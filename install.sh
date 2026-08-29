#!/bin/sh
# researchagen — установщик для macOS / Linux.
# Только POSIX sh + Python stdlib. Никаких внешних библиотек.
# Запуск: sh install.sh

set -u

BOLD="$(printf '\033[1m')"
DIM="$(printf '\033[2m')"
GREEN="$(printf '\033[32m')"
YELLOW="$(printf '\033[33m')"
RED="$(printf '\033[31m')"
CYAN="$(printf '\033[36m')"
OFF="$(printf '\033[0m')"

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_NAME="researchagen"

say() { printf '%s\n' "$1"; }
hr() { say "${DIM}------------------------------------------------------------${OFF}"; }
ok() { say "  ${GREEN}OK${OFF}   $1"; }
warn() { say "  ${YELLOW}WARN${OFF} $1"; }
bad() { say "  ${RED}FAIL${OFF} $1"; }

ask() {
	# ask <prompt> <default>
	_p="$1"; _d="$2"
	if [ -n "$_d" ]; then
		printf '%s %s[%s]%s: ' "$_p" "$DIM" "$_d" "$OFF"
	else
		printf '%s: ' "$_p"
	fi
	IFS= read -r _a || _a=""
	if [ -z "$_a" ]; then _a="$_d"; fi
	printf '%s' "$_a"
}

ask_req() {
	# ask_req <prompt>
	while :; do
		_v="$(ask "$1" "")"
		if [ -n "$_v" ]; then printf '%s' "$_v"; return 0; fi
		say "  ${RED}Поле обязательное.${OFF}"
	done
}

header() {
	clear 2>/dev/null || true
	say ""
	say "${BOLD}  researchagen${OFF} ${DIM}— автономный исследователь training dynamics${OFF}"
	say "${DIM}  дополнительный профиль для agent-hermes — не трогает ваш основной агент${OFF}"
	hr
}

# ------------------------------------------------------------------ 0. Python
PY=""
for c in python3 python; do
	if command -v "$c" >/dev/null 2>&1; then
		if "$c" -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then
			PY="$c"; break
		fi
	fi
done

header
say "${BOLD}Шаг 0/6 — предпроверка${OFF}"
if [ -z "$PY" ]; then
	bad "Не найден Python 3.9+. Установите python3 и повторите."
	exit 1
fi
ok "Python: $($PY -V 2>&1)"

if command -v hermes >/dev/null 2>&1; then
	ok "hermes найден в PATH"
	HAS_HERMES=1
else
	warn "hermes не найден в PATH — профиль будет разложен, но cron и скиллы надо будет подключить вручную"
	HAS_HERMES=0
fi

# ------------------------------------------------------------------ 1. OS
say ""
say "${BOLD}Шаг 1/6 — операционная система${OFF}"
say "  1) macOS  ${DIM}— режим отладки: контур и бот работают, GPU-прогоны идут dry-run${OFF}"
say "  2) Linux  ${DIM}— полный режим, если есть NVIDIA GPU${OFF}"
say "  ${DIM}Для Windows используйте install.ps1${OFF}"
OS_CHOICE="$(ask "Выбор" "1")"
case "$OS_CHOICE" in
	2) PLATFORM="linux"; DEBUG_MODE="false" ;;
	*) PLATFORM="macos"; DEBUG_MODE="true" ;;
esac
ok "Платформа: $PLATFORM (debug_mode=$DEBUG_MODE)"

# ------------------------------------------------------------------ 2. HERMES_HOME
say ""
say "${BOLD}Шаг 2/6 — куда ставим${OFF}"
DEFAULT_ROOT="${HERMES_HOME:-$HOME/.hermes}"
HERMES_ROOT="$(ask "Корень Hermes" "$DEFAULT_ROOT")"
TARGET="$HERMES_ROOT/profiles/$PROFILE_NAME"
say "  Профиль: ${CYAN}$TARGET${OFF}"
if [ -d "$TARGET" ]; then
	warn "Каталог уже существует — код будет обновлён, .env и база сохранены"
fi

# ------------------------------------------------------------------ 3. Telegram
say ""
say "${BOLD}Шаг 3/6 — Telegram (ОТДЕЛЬНЫЙ бот, не тот, что у первого агента)${OFF}"
say "${DIM}  Два процесса с одним токеном не работают: long-polling будет рваться у обоих.${OFF}"
TG_TOKEN="$(ask_req "  Токен бота (BotFather)")"
case "$TG_TOKEN" in
	*:*) : ;;
	*) warn "Токен без двоеточия выглядит неверно — проверьте" ;;
esac
TG_CHAT="$(ask_req "  chat_id рабочего чата/группы")"
TG_THREAD="$(ask "  thread_id темы 'Штаб' (Enter = общий чат)" "")"
say "${DIM}  Оба пользователя должны быть в списке — тогда они видят одну и ту же картину.${OFF}"
TG_USER1="$(ask_req "  user_id пользователя 1")"
TG_USER2="$(ask "  user_id пользователя 2 (Enter = пропустить)" "")"
if [ -n "$TG_USER2" ]; then
	TG_USERS="$TG_USER1,$TG_USER2"
else
	TG_USERS="$TG_USER1"
	warn "Второй пользователь не указан — его можно добавить позже в .env"
fi

# ------------------------------------------------------------------ 4. Модель
say ""
say "${BOLD}Шаг 4/6 — локальная модель (Ollama, OpenAI-совместимый эндпоинт)${OFF}"
MODEL_BASE="$(ask "  base_url" "http://localhost:11434/v1")"
MODEL_NAME="$(ask "  имя модели" "qwen3:27b")"
MODEL_KEY="$(ask "  api_key (Ollama игнорирует)" "ollama")"

# ------------------------------------------------------------------ 5. Лимиты
say ""
say "${BOLD}Шаг 5/6 — границы автономии${OFF}"
GPU_FREE="$(ask "  Минимум свободной VRAM для запуска, ГБ" "6")"
DAILY_BUDGET="$(ask "  Суточный бюджет GPU-часов" "8")"
APPROVAL="$(ask "  Прогон дороже N часов — спрашивать в Telegram" "6")"
AUTOLAUNCH="$(ask "  Автозапуск экспериментов без спроса? (y/n)" "y")"
case "$AUTOLAUNCH" in
	n|N|no|NO) AUTOLAUNCH_VAL="false" ;;
	*) AUTOLAUNCH_VAL="true" ;;
esac

# ------------------------------------------------------------------ Подтверждение
say ""
hr
say "${BOLD}Проверьте перед записью${OFF}"
say "  Профиль      : $TARGET"
say "  Платформа    : $PLATFORM (debug=$DEBUG_MODE)"
say "  Токен бота   : $(printf '%s' "$TG_TOKEN" | cut -c1-8)..."
say "  Чат / тема    : $TG_CHAT / ${TG_THREAD:-—}"
say "  Пользователи: $TG_USERS"
say "  Модель       : $MODEL_NAME @ $MODEL_BASE"
say "  Лимиты      : VRAM ≥ ${GPU_FREE} ГБ, бюджет ${DAILY_BUDGET} ч/сут, подтверждение > ${APPROVAL} ч"
say "  Автозапуск  : $AUTOLAUNCH_VAL"
hr
GO="$(ask "Продолжить? (y/n)" "y")"
case "$GO" in
	n|N|no|NO) say "Отменено. Ничего не записано."; exit 0 ;;
esac

# ------------------------------------------------------------------ 6. Установка
say ""
say "${BOLD}Шаг 6/6 — установка${OFF}"
mkdir -p "$TARGET" || { bad "Не удалось создать $TARGET"; exit 1; }

for d in tools skills skill-bundles cron hooks docs tests hypotheses signals experiments inbox memory reports results logs state; do
	mkdir -p "$TARGET/$d"
done

for f in MISSION.md SOUL.md .hermes.md FOCUS.md distribution.yaml .env.EXAMPLE .gitignore README.md LICENSE; do
	[ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$TARGET/$f"
done
for d in tools skills skill-bundles cron hooks docs tests; do
	[ -d "$SRC_DIR/$d" ] && cp -R "$SRC_DIR/$d/." "$TARGET/$d/" 2>/dev/null
done
ok "Файлы разложены"

# config.yaml с подстановкой значений
"$PY" - "$SRC_DIR/config.yaml" "$TARGET/config.yaml" <<'PYEOF' "$PLATFORM" "$DEBUG_MODE" "$MODEL_NAME" "$MODEL_BASE" "$GPU_FREE" "$DAILY_BUDGET" "$APPROVAL" "$AUTOLAUNCH_VAL"
import sys
src, dst, platform, debug, model, base, gpu_free, budget, approval, autolaunch = sys.argv[1:11]
text = open(src, encoding="utf-8").read()
repl = {
    "<<INSTALLER_PLATFORM>>": platform,
    "<<INSTALLER_MODE>>": "debug" if debug.lower() == "true" else "production",
    "<<INSTALLER_MODEL_NAME>>": model,
    "<<INSTALLER_MODEL_BASE_URL>>": base,
    "<<INSTALLER_GPU_FREE_GB>>": gpu_free,
    "<<INSTALLER_DAILY_GPU_HOURS>>": budget,
    "<<INSTALLER_APPROVAL_GPU_HOURS>>": approval,
    "<<INSTALLER_AUTOLAUNCH>>": autolaunch,
}
for k, v in repl.items():
    text = text.replace(k, v)
open(dst, "w", encoding="utf-8").write(text)
print("config.yaml записан")
PYEOF
ok "config.yaml настроен"

# .env — только если нет, иначе бэкап
if [ -f "$TARGET/.env" ]; then
	cp "$TARGET/.env" "$TARGET/.env.bak"
	warn "Старый .env сохранён как .env.bak"
fi
umask 077
{
	printf '%s\n' "# researchagen — секреты профиля. Не коммитить."
	printf 'TELEGRAM_BOT_TOKEN=%s\n' "$TG_TOKEN"
	printf 'TELEGRAM_HOME_CHANNEL=%s\n' "$TG_CHAT"
	printf 'TELEGRAM_CRON_THREAD_ID=%s\n' "$TG_THREAD"
	printf 'TELEGRAM_ALLOWED_USERS=%s\n' "$TG_USERS"
	printf 'RESEARCHAGEN_MODEL_BASE_URL=%s\n' "$MODEL_BASE"
	printf 'RESEARCHAGEN_MODEL_NAME=%s\n' "$MODEL_NAME"
	printf 'RESEARCHAGEN_MODEL_API_KEY=%s\n' "$MODEL_KEY"
	printf 'RESEARCHAGEN_HOME=%s\n' "$TARGET"
} > "$TARGET/.env"
chmod 600 "$TARGET/.env" 2>/dev/null
ok ".env записан (права 600)"

# Скиллы и бандл в общие каталоги Hermes
if [ -d "$SRC_DIR/skills" ]; then
	mkdir -p "$HERMES_ROOT/skills" "$HERMES_ROOT/skill-bundles"
	cp -R "$SRC_DIR/skills/." "$HERMES_ROOT/skills/" 2>/dev/null
	[ -f "$SRC_DIR/skill-bundles/research-os.yaml" ] && cp "$SRC_DIR/skill-bundles/research-os.yaml" "$HERMES_ROOT/skill-bundles/"
	ok "Скиллы и комплект research-os установлены"
fi

# Cron
if [ "$HAS_HERMES" = "1" ]; then
	"$PY" - "$SRC_DIR/cron" "$TG_CHAT" "$TG_THREAD" "$TARGET" <<'PYEOF'
import json, os, subprocess, sys
cron_dir, chat, thread, workdir = sys.argv[1:5]
if not os.path.isdir(cron_dir):
    raise SystemExit(0)
for name in sorted(os.listdir(cron_dir)):
    if not name.endswith(".json"):
        continue
    job = json.load(open(os.path.join(cron_dir, name), encoding="utf-8"))
    delivery = str(job.get("delivery", "none"))
    delivery = delivery.replace("<<INSTALLER_CHAT_ID>>", chat).replace("<<INSTALLER_THREAD_ID>>", thread)
    cmd = ["hermes", "cron", "add", job["name"], job["schedule"]]
    if job.get("command"):
        cmd += ["--command", job["command"]]
    else:
        cmd += ["--prompt", job.get("prompt", "")]
    if job.get("skill"):
        cmd += ["--skill", job["skill"]]
    if delivery and delivery != "none":
        cmd += ["--delivery", delivery]
    cmd += ["--workdir", workdir]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, HERMES_PROFILE="researchagen"))
        state = "OK" if r.returncode == 0 else "SKIP"
        print("  %s   cron %s" % (state, job["name"]))
    except Exception as exc:
        print("  SKIP cron %s (%s)" % (job["name"], exc))
PYEOF
else
	warn "cron не зарегистрирован: hermes не найден. См. docs/OPERATIONS.md"
fi

# Самопроверка
say ""
hr
say "${BOLD}Самопроверка${OFF}"
( cd "$TARGET" && "$PY" tools/selfcheck.py all )
RC=$?

say ""
hr
if [ "$RC" = "0" ]; then
	say "${GREEN}Готово.${OFF} Профиль установлен и прошёл проверку."
else
	say "${YELLOW}Установлено, но есть ошибки выше.${OFF} Исправьте их до включения автономии."
fi
say ""
say "Дальше:"
say "  ${CYAN}researchagen gateway start${OFF}   ${DIM}# бот и cron в отдельном терминале${OFF}"
say "  ${CYAN}researchagen chat${OFF}            ${DIM}# ручная сессия${OFF}"
say "  ${CYAN}cd \"$TARGET\" && $PY tools/rg.py status${OFF}"
say ""
say "${DIM}Основной агент не затронут: другой профиль, другой токен, другой терминал.${OFF}"
say ""
exit "$RC"
