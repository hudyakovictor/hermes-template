# Аудит стартового пути — итерация 5 (20 анализов)

Фокус захода: Python 3.9-совместимость (Windows), создание каталогов при
старте, BOM/CRLF в конфигурации, запуск субпроцессов, идемпотентность,
жизнь без git (ZIP), нестандартные пути.

## Найденные и исправленные дефекты

| # | Анализ | Дефект | Статус |
|---|--------|--------|--------|
| 1 | `core.load_config` + BOM в config.yaml | **КРИТИЧНО**: если редактор Windows (Notepad) сохранил config.yaml с BOM, `researchagen.platform` → None → платформа становится строкой `"none"`, режим тихо ломается (не production/debug, а мусор) | 🔧 чтение через `encoding="utf-8-sig"`; проверено: BOM+CRLF → windows/production |
| 2 | `install.ps1` / `setup.ps1` без BOM | PowerShell 5.1 читает .ps1 без BOM как ANSI (cp1251): русские сообщения — кракозябры, но синтаксис цел. Для неопытного друга выглядит как поломка | 🔧 добавлен UTF-8 BOM в оба файла |
| 3 | `core.load_env` + `export `-префикс / пробелы вокруг `=` | `.env` в bash-стиле (`export KEY=...`) или с пробелами (`KEY = val`) не читался | 🔧 поддержан префикс `export ` и strip пробелов |
| 4 | (самонанесённый) правка load_env → IndentationError | моя же правка сломала отступ — поймано юнит-тестами (8 errors), исправлено, 176/176 OK | 🔧 |

## Проверено — ошибок нет

| # | Анализ | Результат |
|---|--------|-----------|
| 5 | Синтаксис Python 3.9 (`X | None`, `list[...]`, `match`) | все файлы с pipe-аннотациями имеют `from __future__ import annotations`; `match`/`removeprefix` нет |
| 6 | `isinstance` с subscript-типами (3.9 краш) | нет таких вызовов |
| 7 | Субпроцессы: hardcode `python` vs `sys.executable` | везде `sys.executable` (Windows-safe) |
| 8 | `core.db()` при отсутствии `state/` | вызывает `ensure_dirs()` — каталоги создаются |
| 9 | `core.append_log` при отсутствии `logs/` | вызывает `ensure_dirs()` — лог недоставки пишется |
| 10 | `config.yaml` с CRLF | парсер корректен |
| 11 | `dispatch.runner_command` | путь к exp_runner через `sys.executable`, fallback на `--smoke` |
| 12 | `gpu.py` поиск nvidia-smi на Windows | известные пути `C:\Windows\System32\...` + `shutil.which` |
| 13 | Параллельные `tick` (2 процесса) | оба `[blocked]`, очередь/карточка не раздвоились — идемпотентно |
| 14 | `--json` на status/queue/tick/boot | валидный JSON, exit 0 |
| 15 | `audit run` полный (с coverage) | 80/80, 0 FAIL; охват ядра 63%, всех модулей 59% |
| 16 | Работа без `.git` (из ZIP) | git нигде не вызывается |
| 17 | `HERMES_HOME` с пробелом | изоляция путей корректна |
| 18 | Каталог проекта с кириллицей | `rg.py status` работает |
| 19 | `exp_runner --smoke --dry-run` | exit 0, JSON-результат (dry-run) |
| 20 | miniapp POST `submit_idea` / `idea_check` | идея в IN-001, проверка качества, exit-ответы |
| + | `inbox take/drop`, `verdict record` без аргументов, `rg.py help` | понятные ошибки/usage |
| + | Юнит-тесты (176) + `audit` после всех правок | OK; 80/80 |

## Кумулятивный итог пяти итераций

Исправлено: 7 + 5 + 3 + 2 + 4 = **21 дефект**.

Блокеры старта с нуля, найденные и устранённые:
1. `install.sh`: ручной ввод токена писал промпт в значение (итер. 2);
2. `install.sh --in-place`: установка в клон не работала (итер. 3);
3. `core.load_env`: `.env` в cp1251/UTF-16 ронял все инструменты (итер. 4);
4. `core.load_config`: BOM в config.yaml ломал платформу/режим (итер. 5).

Остаточные риски (только на машине владельца): CLI `hermes` (gateway/cron),
реальный Telegram, GPU + Ollama, исполнение `install.ps1` (в песочнице нет
PowerShell — глубокий статический анализ + BOM-фикс).
