# Windows production (RTX 5090) — запуск deep-research с нуля

> Цель: с пустого клона дойти до живого цикла **SIGNAL → HYPOTHESIS → EXPERIMENT → VERDICT** на Windows-prod, как требует `MISSION.md` + `FOCUS.md` (early bird ticket / lottery ticket / grokking). Покрыто 80 анализами аудита (`tools/audit.py`).

## 0. Что проверяет аудит 80

Аудит разбит на зоны:

- **A-D (a01-a35)**: базовый контур — delegation 2/1, placeholder'ы `<<INSTALLER_PLATFORM>>`, `cron/*.json` `command`, governor, gpu, dispatch, verdict, etc.
- **I. bottom/dr (a36-a50)**: `bottom_detection` домен `training-dynamics`, схемы `bd_regions/hypotheses/evidence/history/cache/runs`, CLI help, `skills/dr/SKILL.md` фазы + governor/discover, `FOCUS.md` термины `early bird ticket/lottery ticket/grokking`, `MISSION.md` существует, `dr` с нуля `live<min_live`, signal mining Phase1, сборка гипотезы ≥3 сигналов, kill-stage 8/8.
- **J. Windows prod (a51-a65)**: `install.ps1` содержит `INSTALLER_PLATFORM/MODE`, `install.sh` — `PLATFORM`, `cron/dispatcher.json` `command: python tools/rg.py tick`, `cron/research-loop.json` `skill: dr` + prompt с governor plan, шаблон `config.yaml` с placeholder'ами, `governor.enabled true`, `gpu_free 20`, `WIN_NVIDIA_SMI` пути, `gpu.snapshot()` мок Windows RTX 5090 22GB free, `dispatch` pause проверяет оба ключа `paused`+`paused_until`, GPU busy, demand L2 требует 3 `demand_signals`, approval `est_hours>limit`, governor plan `capacity≥1` Windows GPU, selfcheck GPU OK Windows.
- **K. logs/hygiene/reports/miniapp/e2e (a66-a80)**: `logs safe_path`, hygiene чистит `>24h` stale runs + архив, `report status json`, panel, verdict list json, queue stats json, crew replay/stats json, inbox sanitize/untrusted, priors cache 7d, miniapp server help + 6 зон, e2e `add→card→gate→launch→finish→verdict`.

Dual-platform правило: отсутствие данных другой ОС не считается ошибкой. На Windows `production` без `nvidia-smi` — FAIL, на macOS — WARN/OK dry-run.

## 1. Установка с нуля (PowerShell) — только токен и API

Теперь быстрый режим по умолчанию: спрашивает **только** `TELEGRAM_BOT_TOKEN` и модель API, остальное авто.

```powershell
# предусловия
python -V        # 3.9+
ollama --version
nvidia-smi       # должен показать RTX 5090
hermes --version

# клон и установка — быстрый режим (только токен и API)
git clone https://github.com/<ваш-логин>/researchagen
cd researchagen
powershell -ExecutionPolicy Bypass -File .\install.ps1
# спросит:
#   TELEGRAM_BOT_TOKEN (обязательно)
#   RESEARCHAGEN_MODEL_BASE_URL [http://localhost:11434/v1] (Enter = Ollama)
#   RESEARCHAGEN_MODEL_NAME [qwen3:27b]
#   RESEARCHAGEN_MODEL_API_KEY [ollama]
# остальное: platform=windows, mode=production, лимиты 6/8/6 авто,
# chat_id/user_id пытается взять из getUpdates (напиши боту /start заранее)

# неинтерактивно (только токен и API):
powershell -ExecutionPolicy Bypass -File .\install.ps1 -BotToken "123:abc" -ModelBase "http://localhost:11434/v1" -ModelKey "ollama" -NonInteractive

# полный режим как раньше (6 шагов):
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Full
```

`install.ps1` спрашивает 6 блоков (ОС, корень Hermes, Telegram token/chat_id/thread_id/2 user_id, модель, лимиты GPU, подтверждение). До подтверждения ничего не пишет.

После установки:

```powershell
cat config.yaml
# ожидаемо:
# researchagen:
#   platform: windows
#   mode: production
#   limits: { gpu_free_gb_required: 20, ... }
#   governor: { enabled: true, ... }
# delegation:
#   max_concurrent_children: 2
#   max_spawn_depth: 1
```

Шаблон содержит `<<INSTALLER_PLATFORM>>` / `<<INSTALLER_MODE>>` — установщик заменяет их. Проверка `a55`, `a51/a52`.

Cron:

```json
// cron/dispatcher.json
{ "command": "python tools/rg.py tick", ... }
// a53 — должен быть command, не script

// cron/research-loop.json
{ "skill": "dr", "prompt": "... governor plan ..." }
// a54
```

## 2. GPU-гейт

```powershell
cd $env:USERPROFILE\.hermes\profiles\researchagen
python tools\gpu.py snapshot
# → RTX 5090: свободно 22 GB (мок в тестах a59)
# на реальном железе: парсит nvidia-smi, пути WIN_NVIDIA_SMI (a58)

python tools\gpu.py check --json
python tools\selfcheck.py all
# Windows prod с GPU: GPU OK (a65)
# без GPU: FAIL (на macOS — WARN)
```

`governor plan` на Windows с GPU должен дать `capacity≥1` (a64).

## 3. Deep-research с нуля (MISSION.md + FOCUS.md)

### 3.1. Фокус и термины

`FOCUS.md` — единственный источник домена (training dynamics). Обязательные термины для поиска (a45):

`early bird ticket`, `lottery ticket`, `iterative magnitude pruning`, `sign stability`, `grokking`, `condensation`, `neural collapse`, `effective rank`, ...

`MISSION.md` — механизм/эксперимент воспроизводимый (a46).

### 3.2. Сигналы

```powershell
# создать каталог сигналов (если пусто — аудит a48 создаст 3 мок-сигнала)
dir signals
# пример сигнала (yaml):
# domain: training-dynamics  ← a36
# anomaly: early loss drop не коррелирует с final
# measurement: ...
```

Сигналы — сырьё для `dr` скилла (фазы в `skills/dr/SKILL.md`, a44):

| Фаза | Что делает |
|------|------------|
| discover | mining signals/ + inbox |
| assemble | ≥3 сигнала → гипотеза (a49) |
| kill-stage | 8/8 проверок (a50) |
| plan | governor capacity, demand, approval |
| launch | L0-L3 |

### 3.3. Гипотезы — сборка ≥3 сигналов

```powershell
python tools\queue.py add "Early sign of useful circuit" --signals 4 --forecast 10 --hours 1 --novelty 0.6 --early 5 --standard 0.5 --money 0.6 --decidability 0.7
python tools\queue.py list
```

`--signals` = количество независимых сигналов (a49 требует ≥3 для сильной гипотезы по MISSION).

### 3.4. Карточка 8/8

```powershell
python tools\hypo.py new H-001 --from-queue
# или вручную: hypotheses/H-001.yaml
```

Карточка должна содержать `REQUIRED_SECTIONS` + `kill_checks` 8 штук (a50, a80):

```yaml
signal_chain: |
  ...
mechanism: |
  ...
...
kill_checks:
  - passed: true  # x8
```

Проверка:

```powershell
python tools\hypo.py check H-001
# → OK 8/8 или список проблем
```

### 3.5. Governor и диспетчер

```powershell
python tools\governor.py plan --json
# Windows prod + GPU → capacity≥1 (a64)
# учитывает gpu_free 20 (a57), governor.enabled true (a56)

python tools\dispatch.py status
# pause проверяет оба ключа: dispatch.paused (bool) + dispatch.paused_until (timed) — a60
```

Дорогой прогон `est_hours>12` требует `/approve` (a63), L2+ требует 3 `demand_signals` (a62), GPU занят — второй launch откажет (a61).

### 3.6. Запуск цикла

```powershell
# тик диспетчера (то же делает cron/dispatcher.json command)
python tools\rg.py tick
python tools\rg.py dr --iterations 1   # research-loop skill dr

# ручной launch
python tools\dispatch.py launch H-001 --level L0
python tools\dispatch.py finish H-001 --gpu-hours 0.1 --state done

# вердикт (не dry-run)
python tools\verdict.py record H-001 --kind confirmed --actual 11 --seeds-pass 3 --seeds-total 3 --sigma 0.1 --gpu-hours 0.1 --changes "L1 разрешён, ранний признак подтверждён"
python tools\verdict.py list --json  # a71
python tools\queue.py stats --json   # a72, live/queued
```

`dispatch.launch` на Windows prod вызывает `nvidia-smi` через `WIN_NVIDIA_SMI` пути (a58) и `tg.send` (мокается в аудите). На macOS — dry-run.

### 3.7. Логи, гигиена, отчёты

```powershell
python tools\report.py status --json  # a69
python tools\report.py panel          # a70
python tools\hygiene.py run --max-run-hours 24  # a67: чистит running>24h, a68 архив
python tools\crew.py replay --n 3     # a73
python tools\crew.py stats --json     # a74
python tools\inbox.py list --json     # a75 sanitize, a76 untrusted
python tools\priors.py search "early bird ticket" --json  # a77 cache 7d
```

`logs/` через `core.safe_path` (a66).

### 3.8. MiniApp — 6 зон

```powershell
python miniapp/server.py --help  # a78
python miniapp/server.py --port 8787
# index.html содержит 6 зон: пульт, конвейер, графики, экипаж, идей, вердикт (a79)
```

## 4. E2E проверка с нуля (a80)

Аудит `a80` повторяет весь путь:

```
tmp_home → queue.add 3 hypo (signals=4) → _full_card 8 kill_checks → hypo.check → launch L0 (darwin mock для CI) → UPDATE dry_run=0 → finish → verdict confirmed
```

На реальном Windows:

```powershell
python tools\audit.py run --no-coverage
# → 80 analyses, 0 fails, 0 warns

python -m unittest discover tests -v
# → 176 OK
```

## 5. Частые ошибки Windows

| Симптом | Решение |
|---------|---------|
| `install.ps1` не запускается | `-ExecutionPolicy Bypass -File` |
| `config.yaml` всё ещё `<<INSTALLER_>>` | Запусти `install.ps1` заново, он заменяет placeholder'ы (a55) |
| `selfcheck FAIL: GPU` | На prod без `nvidia-smi` — FAIL, установи драйвер, проверь `WIN_NVIDIA_SMI` пути (a58) |
| `dispatcher.json` script path must be relative | Используй `command`, не `script` (a53) |
| `queue stats нет live/queued` | Обнови `queue.py stats --json` (a72) |
| `hygiene не чистит` | `reap_stale_runs` требует `started_at` с `datetime('now','-2 days')` и `pid_alive=False` |
| `verdict FAIL dry-run` | Сними `dry_run=0` перед вердиктом (только для e2e теста) |
| `governor unsafe cap 0/0` | `config.yaml` onboarding `{'onboarding':{'seen':...}}` после `rsync --exclude`. Переустанови через `install.ps1` — получишь 2/1 |

## 6. Обновление без потери секретов

```powershell
cd $env:USERPROFILE\.hermes\profiles\researchagen
git pull
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` перезапишет `config.yaml` из шаблона с `2/1` и `windows/production`, секреты из `.env` сохранит.

## 7. Ссылки

- `MISSION.md` — что считается сильной гипотезой (7 пунктов)
- `FOCUS.md` — домен и термины early bird / lottery ticket / grokking
- `skills/dr/SKILL.md` — фазы dr
- `docs/GOVERNOR.md` — bounded delegation
- `docs/BOTTOM-DETECTION.md` — гибридный Bottom Detection (не обходит kill-stage)
- `docs/AUDIT.md` — 80 анализов
