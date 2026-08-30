# Установка на Windows (основная платформа)

Целевая конфигурация: RTX 5090 (32 ГБ), Qwen3-27B Q6 + KV-cache Q8 в Ollama,
Hermes уже установлен и первый агент работает в другом терминале на OpenRouter.

> Полный deep-research путь с нуля → `docs/WINDOWS.md` (80 анализов аудита, FOCUS/MISSION, сигналы → гипотеза ≥3 сигналов → карточка 8/8 → governor → L0-L3 → вердикт).

## 1. Подготовка

```powershell
python -V          # нужен 3.9+
ollama --version
nvidia-smi
hermes --version
```

Обязательно создайте **нового** бота в @BotFather. Переиспользование токена первого
агента приведёт к тому, что оба шлюза будут перебивать друг друга.

## 2. Модель

```powershell
ollama pull qwen3:27b
ollama run qwen3:27b "привет"   # проверка, что веса живые
```

Квантование KV-cache в Q8 задаётся на стороне Ollama (переменные окружения службы),
не в этом профиле. Профиль только потребляет OpenAI-совместимый эндпоинт.

Адрес должен заканчиваться на `/v1`: `http://localhost:11434/v1`.

## 3. Установка — быстрый режим (только токен и API)

По умолчанию установщик теперь в быстром режиме: спрашивает **только** токен и API, остальное авто.

```powershell
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
powershell -ExecutionPolicy Bypass -File .\install.ps1
# спросит:
#   TELEGRAM_BOT_TOKEN (обязательно)
#   RESEARCHAGEN_MODEL_BASE_URL [http://localhost:11434/v1] — Enter = локальная Ollama
#   RESEARCHAGEN_MODEL_NAME [qwen3:27b]
#   RESEARCHAGEN_MODEL_API_KEY [ollama]
# остальное авто: platform windows, mode production, 2/1, лимиты 6/8/6, chat_id/user_id из getUpdates

# полностью неинтерактивно:
powershell -ExecutionPolicy Bypass -File .\install.ps1 -BotToken "123:abc" -ModelBase "http://localhost:11434/v1" -NonInteractive

# полный режим (6 шагов как раньше):
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Full
```

Авто-определение Telegram: установщик зовёт `https://api.telegram.org/bot<token>/getUpdates` и берёт последний `chat.id` и `from.id`. Для этого **заранее** напиши боту `/start` в личку или добавь его в группу и напиши что-нибудь. Если не определилось — ставит `0`, после запуска бот подскажет как обновить `.env`.

Если нужен ручной ввод chat_id/thread_id/user_id — используй `-Full`.

Где взять ID вручную (для полного режима):

- `chat_id` группы: добавь бота в группу, напиши сообщение и открой `getUpdates` в браузере. У супергрупп ID начинается с `-100`.
- `thread_id`: в том же ответе поле `message_thread_id`.
- `user_id`: там же, `from.id`.

После установки `config.yaml`:

```yaml
researchagen:
  platform: windows
  mode: production
  limits: { gpu_free_gb_required: 20 }
  governor: { enabled: true }
delegation:
  max_concurrent_children: 2
  max_spawn_depth: 1
```

Шаблон содержит `<<INSTALLER_PLATFORM>>` / `<<INSTALLER_MODE>>` (a55), `install.ps1` заменяет их (a51). `cron/dispatcher.json` — `command: python tools/rg.py tick` (a53), `research-loop.json` — `skill: dr` + prompt с governor plan (a54).

## 4. Запуск в отдельном терминале

Откройте **второе** окно PowerShell (первое занято основным агентом):

```powershell
researchagen gateway start
```

Шлюз держит и Telegram, и cron. Закрытие окна останавливает автономию — в этом и смысл
отдельного терминала: одно окно — один агент.

Проверка без модели:

```powershell
cd $env:USERPROFILE\.hermes\profiles\researchagen
python tools\gpu.py snapshot
# → RTX 5090: свободно 22 GB (a59), парсит nvidia-smi через WIN_NVIDIA_SMI пути (a58)
python tools\rg.py status
python tools\selfcheck.py all
# Windows prod с GPU → GPU OK (a65), governor plan capacity≥1 (a64)
python tools\audit.py run --no-coverage
# → 80 analyses, 0 fails
python -m unittest discover -s tests -q
# → 176 OK
```

## 5. Deep-research с нуля (кратко, подробно — docs/WINDOWS.md)

```powershell
# 1. Сигналы из FOCUS.md терминов early bird ticket / lottery ticket / grokking
dir signals
python tools\priors.py search "early bird ticket" --json  # a77 cache 7d

# 2. Гипотеза ≥3 сигналов (MISSION.md 7 пунктов)
python tools\queue.py add "Early sign of useful circuit" --signals 4 --forecast 10 --hours 1 --novelty 0.6 --early 5 --standard 0.5 --money 0.6 --decidability 0.7
python tools\queue.py list
python tools\queue.py stats --json  # a72 live/queued

# 3. Карточка 8/8 kill_checks
python tools\hypo.py new H-001 --from-queue
# заполни REQUIRED_SECTIONS + 8 kill_checks
python tools\hypo.py check H-001  # a50

# 4. Governor / dispatch гейты
python tools\governor.py plan --json  # a64 capacity≥1, a56 enabled, a57 gpu_free 20
python tools\dispatch.py status       # a60 pause оба ключа

# 5. Тик и launch
python tools\rg.py tick
python tools\rg.py dr --iterations 1
python tools\dispatch.py launch H-001 --level L0  # a61 GPU busy, a62 demand L2 3 сигнала, a63 approval >12h
python tools\dispatch.py finish H-001 --gpu-hours 0.1 --state done

# 6. Вердикт
python tools\verdict.py record H-001 --kind confirmed --actual 11 --seeds-pass 3 --seeds-total 3 --sigma 0.1 --gpu-hours 0.1 --changes "L1 разрешён"
python tools\verdict.py list --json  # a71
python tools\report.py status --json # a69
python tools\report.py panel         # a70

# 7. Логи/гигиена/crew/inbox/miniapp
python tools\hygiene.py run --max-run-hours 24  # a67 stale >24h, a68 archive
python tools\crew.py replay --n 3               # a73
python tools\crew.py stats --json               # a74
python tools\inbox.py list --json               # a75 sanitize, a76 untrusted
python miniapp/server.py --port 8787            # a78 help, a79 6 зон
```

E2E из нуля покрыт `a80`: `add→card→gate→launch→finish→verdict`.

Bottom detection (опционально):

```powershell
python tools\rg.py bottom run --iterations 1
# схемы: bd_regions/hypotheses/evidence/history/cache/runs (a37-a42), домен training-dynamics (a36)
```

## 6. Автозапуск при входе (опционально)

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument '-NoExit -Command researchagen gateway start'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'researchagen-gateway' -Action $action -Trigger $trigger
```

Не делайте это, пока `selfcheck` не зелёный: автозапуск сломанного контура генерирует
мусор быстрее, чем вы его читаете.

## Типичные проблемы Windows

| Симптом | Причина и решение |
|---|--- |
| `install.ps1` не запускается | Политика исполнения. Запускайте с `-ExecutionPolicy Bypass -File` |
| Кракозябры вместо русских букв | `[Console]::OutputEncoding=[Text.Encoding]::UTF8` перед запуском |
| Модель не отвечает | Ollama слушает только 127.0.0.1 либо адрес без `/v1` |
| VRAM всегда занята | Модель висит в памяти. `ollama stop qwen3:27b` или `/gpu` |
| Шлюз не стартует | Тот же токен, что у первого агента. `selfcheck` пишет об этом прямо |
| `selfcheck` FAIL: GPU | На Windows `production` без `nvidia-smi` — `FAIL`. На macOS — `WARN`/`OK` (dry-run). Проверь драйвер, `gpu.snapshot()` должен показать RTX 5090 (a58/a59) |
| `selfcheck` FAIL: модель | Пустая `RESEARCHAGEN_MODEL_BASE_URL` на Windows — `FAIL` (нужна для L1+). На macOS — `WARN` |
| `selfcheck` FAIL: изоляция токена (корневой) | Токен в `~/.hermes/.env` + профиль — `FAIL` (второй gateway не запустится). Удали `~/.hermes/.env` или смени токен |
| `selfcheck` WARN: изоляция токена (профили) | Токен в `profiles/main` + `profiles/researchagen` — `WARN` для macOS+Windows (допустимо, запускай один gateway) |
| `governor` unsafe cap 0/0 | `config.yaml` — onboarding `{'onboarding':{'seen':...}}` после `rsync --exclude=config.yaml`. Решение: `install.ps1` (перезапишет шаблон с `2/1`) |
| `hygiene не чистит` | `reap_stale_runs` требует `started_at` 2 дня назад и `pid_alive=False` (a67) |
| `verdict FAIL dry-run` | На e2e тесте сними `dry_run=0` перед вердиктом (a80) |

## Обновление без потери секретов

Не используй `rsync --exclude=config.yaml --exclude=.env --exclude=state/` — он
оставляет onboarding-конфиг с капсами `0/0`. Правильно:

```powershell
cd $env:USERPROFILE\.hermes\profiles\researchagen
git pull
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` пишет `platform: windows / mode: production` и `max_concurrent_children: 2`,
`max_spawn_depth: 1`, поэтому `governor` проходит `selfcheck`.
