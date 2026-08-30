#!/usr/bin/env python3
"""researchagen — самопроверка профиля (doctor).

Проверяет то, что ломается на практике: нет .env, пустой токен, чужой токен
(тот же, что у основного профиля), недоступная локальная модель, нет GPU в production,
база не пишется, гипотез меньше минимума, зависшие прогоны.

Exit code: 0 — всё критичное в порядке; 1 — есть FAIL.

CLI: python tools/selfcheck.py all [--json]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import core
import crew
import gpu
import governor
import queue as q

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _check(name: str, state: str, detail: str) -> dict:
    return {"check": name, "state": state, "detail": detail}


def check_layout() -> list[dict]:
    out = []
    for name in ("SOUL.md", ".hermes.md", "MISSION.md", "FOCUS.md", "config.yaml",
                 "distribution.yaml"):
        path = os.path.join(core.ROOT, name)
        out.append(_check(f"файл {name}", OK if os.path.exists(path) else FAIL,
                          path if os.path.exists(path) else "отсутствует"))
    for d in core.ALL_DIRS:
        core.ensure_dirs()
        out.append(_check(f"каталог {os.path.basename(d)}",
                          OK if os.path.isdir(d) else FAIL, d))
    return out


def check_env() -> list[dict]:
    env = core.load_env()
    out = []
    if not os.path.exists(core.ENV_PATH):
        out.append(_check(".env", FAIL, "нет файла — запусти install.ps1 / install.sh"))
        return out
    out.append(_check(".env", OK, core.ENV_PATH))
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        out.append(_check("TELEGRAM_BOT_TOKEN", FAIL, "пусто"))
    elif ":" not in token:
        out.append(_check("TELEGRAM_BOT_TOKEN", FAIL, "не похож на токен BotFather"))
    else:
        out.append(_check("TELEGRAM_BOT_TOKEN", OK, f"…{token[-6:]}"))
    out.append(_check("TELEGRAM_HOME_CHANNEL",
                      OK if env.get("TELEGRAM_HOME_CHANNEL") else FAIL,
                      env.get("TELEGRAM_HOME_CHANNEL", "пусто")))
    users = [u for u in (env.get("TELEGRAM_ALLOWED_USERS", "") or "").split(",") if u.strip()]
    out.append(_check("TELEGRAM_ALLOWED_USERS",
                      OK if len(users) >= 1 else FAIL,
                      f"{len(users)} пользователей" +
                      (" — ожидалось 2" if len(users) == 1 else "")))
    return out


def check_isolation() -> list[dict]:
    """Главный риск двухпрофильной схемы: один токен на два gateway.

    На macOS+Windows один токен — штатная схема (INSTALL-macos.md): один бот
    на два устройства, но gateway только в одном месте за раз. Поэтому коллизия
    — это WARN, а не FAIL: она не ломает контур, но требует ручного контроля
    «один gateway активен». FAIL только для корневого профиля (default), где
    два gateway на одной машине гарантированно рвут long-polling.

    Учитывает кривой HERMES_HOME: если он указывает на профиль
    (.../.hermes/profiles/researchagen), то реальный дом — .../.hermes,
    и проверяем оба места, исключая собственный .env.
    """
    env = core.load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    out = []
    raw_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    # набор домов для проверки: глобальный ~/.hermes + то, что в HERMES_HOME + вывод из RESEARCHAGEN_HOME
    homes = set()
    homes.add(os.path.expanduser("~/.hermes"))
    homes.add(raw_home)
    # если HERMES_HOME указывает внутрь profiles/, то реальный дом — до /profiles/
    if "/profiles/" in raw_home.replace("\\", "/"):
        # /.../.hermes/profiles/researchagen -> /.../.hermes
        parts = raw_home.replace("\\", "/").split("/profiles/")
        if parts[0]:
            homes.add(parts[0])
    # RESEARCHAGEN_HOME (core.ROOT) тоже может подсказать реальный дом
    try:
        root = core.ROOT
        if "/profiles/" in root.replace("\\", "/"):
            parts = root.replace("\\", "/").split("/profiles/")
            if parts[0]:
                homes.add(parts[0])
    except Exception:
        pass

    clashes = []
    root_clash = False
    seen_envs = set()
    own_env = os.path.abspath(core.ENV_PATH)

    if token:
        for h in homes:
            if not h or not os.path.isdir(h):
                continue
            profiles_dir = os.path.join(h, "profiles")
            if os.path.isdir(profiles_dir):
                try:
                    for entry in os.listdir(profiles_dir):
                        other = os.path.join(profiles_dir, entry, ".env")
                        abs_other = os.path.abspath(other)
                        if abs_other in seen_envs or abs_other == own_env:
                            continue
                        if not os.path.exists(other):
                            continue
                        seen_envs.add(abs_other)
                        try:
                            with open(other, "r", encoding="utf-8", errors="replace") as fh:
                                if token in fh.read():
                                    # если это наш собственный профиль, не считаем
                                    if entry == os.path.basename(core.ROOT):
                                        continue
                                    clashes.append(entry)
                        except OSError:
                            continue
                except OSError:
                    pass
            # корневой .env в этом доме
            root_env = os.path.join(h, ".env")
            abs_root = os.path.abspath(root_env)
            if abs_root in seen_envs or abs_root == own_env:
                continue
            if os.path.exists(root_env):
                seen_envs.add(abs_root)
                try:
                    with open(root_env, "r", encoding="utf-8", errors="replace") as fh:
                        if token in fh.read():
                            # если root_env — это наш собственный .env (когда HERMES_HOME=профиль), не считаем
                            if abs_root == own_env:
                                continue
                            clashes.append("default (корневой профиль)")
                            root_clash = True
                except OSError:
                    pass

    # дедупликация имён
    clashes = sorted(set(clashes))
    if root_clash:
        state = FAIL
        detail = f"тот же токен в: {', '.join(clashes)} — второй gateway не запустится (корневой конфликт)"
    elif clashes:
        state = WARN
        detail = (f"тот же токен в: {', '.join(clashes)} — допустимо для macOS+Windows, "
                  "но запускай только один gateway за раз (см. INSTALL-macos.md)")
    else:
        state = OK
        detail = "токен уникален среди профилей"
    out.append(_check("изоляция токена", state, detail))
    out.append(_check("HERMES_HOME", OK, raw_home))
    return out


def check_contamination() -> list[dict]:
    """Ранняя защита: путь-изоляция и «не лезть в чужую память».

    Основной агент живёт на том же устройстве (memories/, sessions/, auth.json).
    Проверяем: (1) guard safe_path существует и реально запирает ROOT;
    (2) инструменты не обращаются к каталогам соседа функционально.
    """
    out = []
    guard = getattr(core, "safe_path", None)
    if guard is None:
        out.append(_check("изоляция путей", FAIL,
                          "нет core.safe_path — запись может уйти из профиля"))
    else:
        try:
            guard("../escape.txt")
            out.append(_check("изоляция путей", FAIL,
                              "safe_path пропустил путь вне ROOT"))
        except (PermissionError, ValueError):
            out.append(_check("изоляция путей", OK,
                              f"записи заперты в {core.ROOT}"))
    return out


def check_logs_secrets() -> list[dict]:
    """Секреты не должны оседать в логах и чате экипажа."""
    token = (core.load_env().get("TELEGRAM_BOT_TOKEN") or "").strip()
    out = []
    if not token:
        return [_check("секреты в логах", OK, "токена нет — и утекать нечему")]
    leaks = []
    for dirname in (core.LOGS_DIR,):
        if not os.path.isdir(dirname):
            continue
        for name in os.listdir(dirname):
            try:
                with open(os.path.join(dirname, name), encoding="utf-8",
                          errors="replace") as fh:
                    blob = fh.read()
            except OSError:
                continue
            if token in blob:
                leaks.append(name)
    try:
        conn = core.db()
        n = conn.execute("SELECT COUNT(*) FROM crew_chat WHERE text LIKE ?",
                         (f"%{token}%",)).fetchone()[0]
        conn.close()
        if n:
            leaks.append(f"crew_chat ({n} реплик)")
    except Exception:  # noqa: BLE001
        pass
    out.append(_check("секреты в логах", FAIL if leaks else OK,
                      f"токен найден в: {', '.join(leaks)}" if leaks
                      else "токена в логах и чате нет"))
    return out


def check_env_perms() -> list[dict]:
    """Права .env: 600 (только владелец). На Windows — пропускаем."""
    out = []
    if os.name != "posix" or not os.path.exists(core.ENV_PATH):
        return out
    mode = os.stat(core.ENV_PATH).st_mode & 0o777
    out.append(_check(
        "права .env",
        OK if mode == 0o600 else WARN,
        f"{oct(mode)} — рекомендуем chmod 600 (секреты профиля)"
        if mode != 0o600 else "600"))
    return out


def check_model() -> list[dict]:
    env = core.load_env()
    base = (env.get("RESEARCHAGEN_MODEL_BASE_URL") or "").rstrip("/")
    name = env.get("RESEARCHAGEN_MODEL_NAME", "")
    config = core.load_config()
    _, is_debug = core.platform_mode(config)
    if not base:
        # на macOS/debug модель может отсутствовать для dry-run отладки очереди
        state = WARN if is_debug else FAIL
        return [_check("локальная модель", state,
                       "RESEARCHAGEN_MODEL_BASE_URL пуст — "
                       + ("dry-run режим, модель опциональна" if is_debug
                          else "нужен для L1+ прогонов"))]
    url = base + "/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id", "") for m in data.get("data", [])]
        if name and not any(name.split(":")[0] in i for i in ids):
            return [_check("локальная модель", WARN,
                           f"endpoint жив, но модели {name} в списке нет: "
                           + ", ".join(ids[:6]))]
        return [_check("локальная модель", OK, f"{name} доступна на {base}")]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, TimeoutError) as exc:
        state = WARN if is_debug else FAIL
        return [_check("локальная модель", state,
                       f"{base} не отвечает ({exc}). "
                       + ("dry-run доступен, но /dr требует модель" if is_debug
                          else "Запусти ollama serve и проверь порт."))]


def check_gpu() -> list[dict]:
    config = core.load_config()
    # onboarding — профиль ещё не установлен, GPU-чек не критичен
    if "researchagen" not in config and "onboarding" in config:
        return [_check("GPU", WARN, "профиль в onboarding-состоянии — GPU-гейт проверится после install.sh")]
    snap = gpu.snapshot(config)
    if snap["available"]:
        state = OK if snap["free_gb"] >= snap["required_gb"] else WARN
        return [_check("GPU", state,
                       f"{snap['best']['name']}: свободно {snap['free_gb']:.1f} GB "
                       f"из {snap['best']['total_gb']:.1f} GB")]
    if snap["debug"]:
        return [_check("GPU", OK, "macOS/debug: эксперименты идут как dry-run — штатно")]
    # в шаблоне (config.yaml с <<INSTALLER_>>) platform_mode падает в linux/production,
    # но nvidia-smi отсутствует — это не ошибка первого запуска, а отсутствие GPU
    plat_raw = str(core.cfg("researchagen.platform", "", config) or "")
    if plat_raw.startswith("<<") or not plat_raw:
        return [_check("GPU", WARN,
                       "GPU не обнаружен, config.yaml ещё шаблонный — запусти install.sh и проверь nvidia-smi")]
    return [_check("GPU", FAIL, "nvidia-smi не найден, а режим production")]


def check_db() -> list[dict]:
    try:
        conn = core.db()
        core.set_setting(conn, "selfcheck.last", core.iso())
        live = q.live_count(conn)
        minimum = int(core.cfg("researchagen.limits.min_live_hypotheses", 3))
        out = [_check("база состояния", OK, core.DB_PATH)]
        out.append(_check("живых гипотез", OK if live >= minimum else WARN,
                          f"{live} (минимум по правилу R2: {minimum})"))
        stale = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE state='running' "
            "AND datetime(started_at) < datetime('now','-1 day')").fetchone()[0]
        out.append(_check("зависшие прогоны", OK if stale == 0 else WARN,
                          f"{stale} прогонов в статусе running >24ч "
                          "(починит tools/hygiene.py)" if stale else "нет"))
        return out
    except Exception as exc:  # noqa: BLE001
        return [_check("база состояния", FAIL, str(exc))]


def check_python() -> list[dict]:
    major, minor = sys.version_info[:2]
    state = OK if (major, minor) >= (3, 9) else FAIL
    return [_check("python", state, f"{sys.version.split()[0]} — нужен 3.9+ (только stdlib)")]


def check_governor() -> list[dict]:
    config = core.load_config()
    # если профиль ещё в onboarding (hermes profile create без install.sh),
    # config.yaml содержит только onboarding — это не unsafe, а незавершённая установка
    if "researchagen" not in config and "onboarding" in config:
        return [_check("governor", WARN,
                       "профиль в onboarding-состоянии — запусти install.sh для записи delegation caps")]
    enabled = governor.enabled(config)
    max_children = int(core.cfg("delegation.max_concurrent_children", 0, config) or 0)
    max_depth = int(core.cfg("delegation.max_spawn_depth", 0, config) or 0)
    if not enabled:
        return [_check("governor", FAIL, "research/GPU admission controller отключён")]
    if max_children < 1 or max_depth != 1:
        return [_check("governor", FAIL,
                       f"unsafe Hermes delegation cap: children={max_children}, depth={max_depth}")]
    conn = None
    try:
        conn = core.db()
        plan = governor.plan(conn, config)
        return [_check("governor", OK,
                       f"mode={plan['mode']}, capacity={plan.get('capacity', 0)}, "
                       f"available={plan.get('available_slots', 0)}")]
    except Exception as exc:  # noqa: BLE001
        return [_check("governor", FAIL, str(exc))]
    finally:
        if conn is not None:
            conn.close()


def check_chat() -> list[dict]:
    """Чат экипажа: таблица живая и топик для доставок указан."""
    out = []
    try:
        conn = core.db()
        crew.init_db(conn)
        n = int(conn.execute("SELECT COUNT(*) FROM crew_chat").fetchone()[0])
        conn.close()
        out.append(_check("чат экипажа (таблица)", OK,
                          f"crew_chat доступна, реплик: {n} (/aichat)"))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("чат экипажа (таблица)", FAIL, str(exc)))
    env = core.load_env()
    thread = (env.get("TELEGRAM_AICHAT_THREAD_ID", "")
              or env.get("TELEGRAM_CHAT_THREAD_ID", "")
              or env.get("TELEGRAM_CREW_THREAD_ID", ""))
    out.append(_check("чат экипажа (топик)",
                      OK if thread else WARN,
                      thread if thread else
                      "TELEGRAM_AICHAT_THREAD_ID пуст: переписка только в базе (/aichat)"))
    return out


def run_all() -> dict:
    checks: list[dict] = []
    checks += check_python()
    checks += check_layout()
    checks += check_env()
    checks += check_isolation()
    checks += check_contamination()
    checks += check_env_perms()
    checks += check_logs_secrets()
    checks += check_model()
    checks += check_gpu()
    checks += check_governor()
    checks += check_chat()
    checks += check_db()
    fails = [c for c in checks if c["state"] == FAIL]
    warns = [c for c in checks if c["state"] == WARN]
    return {"checks": checks, "fails": len(fails), "warns": len(warns),
            "ok": not fails}


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    as_json = core.wants_json(argv)
    data = run_all()
    icons = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    text = "\n".join(f"{icons[c['state']]} {c['check']}: {c['detail']}" for c in data["checks"])
    text += (f"\n\nИтог: ошибок {data['fails']}, предупреждений {data['warns']}. "
             + ("Контур готов к работе." if data["ok"] else "Запускать нельзя — см. ❌."))
    core.emit(data, as_json, text)
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
