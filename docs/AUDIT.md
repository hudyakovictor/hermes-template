# Аудит функционала: 80 анализов → топ-ошибок → исправления

Дата: 2026-08-30 · инструмент: `python tools/rg.py audit` (или `python tools/audit.py run`)

## Метод

80 детерминированных анализов гоняют **реальный код** — библиотечные вызовы на
временной базе и CLI во временном `RESEARCHAGEN_HOME` (никаких сетей, GPU и
токенов; сеть замокана). Каждый анализ возвращает находки FAIL/WARN; аудит
честный: до исправлений он находил ошибки, после — 80/80 зелёные.

Комбинационное покрытие (метод «90% задач»): анализы перебирают не счастливые
пути, а матрицы — все исходы вердиктов × попадание/промах коридора, карточки
× сигналы × прогноз, гейты диспетчера по одному, все сцены × 8 сидов,
все источники × доступность. Охват кода по строкам (трассировка in-process +
`python -m trace` для сабпроцессов, без тел многострочных шаблонов):
**ядро контура 57%, все модули 53%** — неисполненные остатки это ветки
реального GPU/сети/борды, которые офлайн-аудиту недоступны по определению.

## Зоны и анализы (80)

| Зона | Анализы | Что проверяется |
|---|---|---|
| A. Данные | a01–a06 | свежая схема; миграция старой базы; очередь (авто-коридор, выбор, закрытие); числовые санитайзеры; PPI/MII/корзины; матрица гейтов карточек |
| B. Вердикты | a07–a10 | 4 исхода × коридор × закрытие ставок; идемпотентность ставок; контрольные числа калибровки (bias/MAE/asym/corridor); запрет перекалибровки на малой выборке |
| C. Диспетчер | a11–a14 | гейты (пауза/спрос-чек); аренды governor; report без --file; идемпотентность гигиены |
| D. Чат | a15–a20 | все сцены × 8 сидов без пустых/None/хвостов; договор 1–5 реплик и разгон споров спорностью; арбитраж Boss; пулы 5/2% и лимит 100/день; 9 искателей ревью; история на пустой базе |
| E. Интерфейс/доки | a21–a25 | `--help` во всех инструментах; `--json` всех ключевых команд; команды и флаги из документации существуют; первый запуск 21 команды без трейсбеков |
| F. Изоляция | a26–a29 | safe_path (записи только в ROOT); статический запрет файловых операций с memories//sessions//workspace//auth.json; секреты не утекают в выводы и логи; детект токена на два профиля |
| G. Источники | a30 | prior-art: все 6 источников, планка ≥90%, офлайн — честный запрет вывода |
| H. Кроссплатформенность | a31–a35 | `platform_mode`: macos→debug, windows/linux→production; GPU-гейт: macOS dry-run без карты, Windows требует карту; изоляция токена: корень=FAIL, профили=WARN; модель: debug=WARN, production=FAIL; governor капсы 2/1, onboarding=WARN |
| I. Bottom/dr/FOCUS/MISSION | a36–a50 | bottom_config домен training-dynamics; схемы bd_regions/hypotheses/evidence/history/cache/runs; CLI help; dr skill фазы + governor/discover; FOCUS термины early bird/lottery ticket/grokking; MISSION; dr с нуля live<min_live; signal mining; гипо ≥3 сигналов; kill-stage 8/8 |
| J. Windows prod | a51–a65 | install.ps1 caps, install.sh PLATFORM, cron dispatcher command, research-loop skill dr, config placeholders, governor.enabled true, gpu_free 20, WIN_NVIDIA_SMI пути, gpu snapshot RTX 5090, dispatch pause оба ключа, gpu busy, demand L2 3 сигнала, approval >limit, governor plan capacity≥1 Windows GPU, selfcheck GPU OK Windows |
| K. Логи/гигиена/отчёты/miniapp/e2e | a66–a80 | logs safe_path, hygiene stale >24h + архив, report status json/panel, verdict list json, queue stats json, crew replay/stats json, inbox sanitize/untrusted, priors cache 7d, miniapp server help + 6 зон, e2e add→card→gate→launch→finish→verdict |

Dual-platform: отсутствие данных другой ОС не считается ошибкой. На Windows `production` без `nvidia-smi` — FAIL, на macOS — WARN/OK dry-run.

## Топ-ошибок, найденных и исправленных (расширено до 80)

| # | Ошибка | Исправление |
|---|---|---|
| 1-14 | `--help` падал | help/-h/--help печатают докстринг |
| 15-17 | NaN/inf в queue/hypo | `core.to_number` конечность |
| 18 | `governor report` без --file | понятная ошибка |
| 19 | нет path-изоляции | `safe_path` |
| 20 | inbox без санитайза | `sanitize` + `trusted:false` |
| 21-35 | кроссплатформенные | platform/mode, GPU-гейт, изоляция токена, caps 2/1 |
| 36-43 | bottom_detection схемы | домен training-dynamics, таблицы, CLI help |
| 44-50 | dr skill / FOCUS / MISSION | фазы, governor, discover, термины early bird/lottery/grokking, live<min_live, mining, ≥3 сигналов, 8/8 kill |
| 51-65 | Windows prod | install.ps1/.sh, cron command, config placeholders, gpu_free 20, WIN_NVIDIA_SMI, snapshot RTX 5090, pause оба ключа, busy, demand L2, approval, plan capacity≥1, selfcheck OK |
| 66-80 | логи/гигиена/отчёты/miniapp/e2e | safe_path logs, stale runs, archive, status/panel json, verdict/queue/crew json, inbox sanitize, priors cache 7d, miniapp 6 зон, e2e zero→launch |

## Изоляция среды (a26–a29)

Соседний основной агент живёт на том же устройстве. Защита:
1. Пути через `safe_path`
2. Статический аудит файловых операций
3. Контент — sanitize + trusted:false
4. Секреты — doctor проверяет логи/права/коллизию токенов

## Источники (a30, a77)

`rg.py priors search` — 6 источников, планка ≥90%, кэш 7 дней.

## Windows prod специфика (a51–a65)

- `install.ps1` → `platform: windows / mode: production / 2/1`
- `gpu.snapshot()` → `RTX 5090: свободно XX GB` (мок 22 GB в тестах)
- `WIN_NVIDIA_SMI` пути, `selfcheck GPU OK` только с GPU
- `governor plan` capacity≥1 при GPU
- `dispatch` pause проверяет `paused` + `paused_until`
- demand L2: 3 `demand_signals`, approval: `est_hours>12`

## E2E с нуля (a80)

```
tmp_home → queue.add 3 hypo (signals=4) → _full_card 8 kill_checks → hypo.check → launch L0 (darwin mock) → UPDATE dry_run=0 → finish → verdict
```

На реальном Windows: `audit.py run --no-coverage` → 80/80, `unittest` → 176 OK.

## Как запускать

```bash
python tools/rg.py audit            # 80 анализов + отчёт reports/audit-<дата>.md
python tools/rg.py audit --no-coverage   # быстрее, без замера покрытия
python tools/rg.py doctor           # среда: токен, изоляция, секреты, модель, GPU
python tools/rg.py priors search " early bird ticket"
```

Выход аудита: 0 — FAIL-находок нет; 1 — есть. В CI/кроне: раз в сутки.
