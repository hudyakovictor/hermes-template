# Какие возможности Hermes использованы и зачем

Смысл таблицы — не «галочки», а объяснение, какую задачу закрывает каждый механизм.

| Механизм Hermes | Где используется | Зачем именно он |
|---|---|---|
| **Профили** | отдельный `profiles/researchagen` | Изоляция от первого агента: свой токен, своя модель, своя база |
| **Алиас профиля** | `researchagen gateway start` | Второй терминал без путаницы с `HERMES_HOME` |
| **Распространение профилей** | `distribution.yaml` | Установка прямо из GitHub одной командой, без ручного копирования |
| **Скиллы** | `skills/<name>/SKILL.md`, включая `/conclave` и `/debate` | Каждый скилл — слеш-команда и сжатая инструкция: контекст грузится только когда нужен; `/bottom` подключает hybrid search layer |
| **Комплекты скиллов** | `skill-bundles/research-os.yaml` | Одна сущность вместо 20 разрозненных файлов при установке и обновлении |
| **Cron** | dispatcher/research/conclave-watch/digest/recalib/hygiene | Основа автономии: короткие тики вместо одной бесконечной сессии, которая теряет контекст; Conclave-watch CPU-only |
| **`--workdir` у cron** | все задачи | Прогоны сериализуются по рабочему каталогу — два тика не лезут в одну базу одновременно |
| **`--skill` у cron** | `research-loop` | Тик стартует уже в нужной роли, без длинного промпта-напоминания |
| **Адреса доставки** | `telegram:<chat>:<thread>` | Сводки падают в нужную тему, а не в общую кашу |
| **Циклы (`/loop`)** | ручной спринт, скилл `/auto` | Когда нужно довести одну тему до конца за одну сессию; завершение по `LOOP_COMPLETE` |
| **Цели (`/goal`)** | ручной спринт | Контракт завершения (outcome / verification / stop_when) вместо «поработай над этим» |
| **Гейты целей (`/goal gate add`)** | `selfcheck.py all`, `tests/` | Цель не считается выполненной, пока проверочная команда красная |
| **Канбан** | native Kanban + `tools/board.py sync` | Durable handoff и визуальная картина; scientific SQLite остаётся единственным источником PI/PPI/verdict |
| **Subagent delegation** | нативный `delegate_task` через parent | Короткий flat research fan-out; dynamic count только после `tools/rg.py governor plan/reserve` |
| **Governor** | `tools/governor.py`, `governor_leases` | Общий admission ledger: GPU telemetry, experiment lock, pause/resume/checkpoint, budget и report validation |
| **Conclave** | `tools/conclave.py`, `conclave_*` tables | Fixed role zones, trigger-based two-round critique, public Russian transcript, measured nudges и task/client commentary |
| **Шлюз Telegram** | `researchagen gateway start` + outbound `tools/tg.py` | Управление/updates читает Hermes; Conclave transcript и telemetry только отправляются через Bot API, второго polling нет |
| **Память** | `memory.memory_enabled` | Уроки из убитых гипотез переживают перезапуск сессии |
| **Компрессия** | `compression.threshold` | 25-минутные тики не упираются в лимит контекста локальной модели |
| **Вспомогательные слоты** | `title_generation`, `compression`, `goal_judge` | Дешёвые служебные вызовы не занимают главную модель на GPU |
| **Хук BOOT** | `hooks/BOOT.md` | Любая сессия начинается с фактов из базы, а не с домыслов по памяти |

## Что сознательно НЕ использовано

| Механизм | Причина отказа |
|---|---|
| Собственный Telegram-бот на long-polling | Второй процесс на тот же токен ломает оба. Телеметрия — только отправка |
| Параллельные эксперименты | Одна карта. Два прогона — два шумных результата вместо одного чистого |
| Рекурсивный swarm / nested delegation | `max_spawn_depth=1` и `orchestrator_enabled=false`: на одной GPU дерево умножает Qwen calls и ломает бюджет |
| Планирование cron самим агентом | Агент, который может плодить свои задачи, размножает их тихо |
| Запись калибровки в `config.yaml` | Конфиг принадлежит человеку. Веса живут в `settings` базы |
| MCP-серверы для поиска литературы | Цель — ноль внешних зависимостей и платных сервисов; `urllib` достаточно |
