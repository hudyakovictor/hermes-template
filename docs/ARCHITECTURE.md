# Архитектура

## Главное решение: parent governor + два управляемых контура

Самая дорогая ошибка — поручить языковой модели решать, когда тратить GPU, или
запустить безусловный swarm. Модель недетерминирована, а бюджет — жёсткий ресурс.
Поэтому parent Hermes принимает исследовательские решения, а stdlib governor
проверяет admission и lease-инварианты:

| | Контур A — диспетчер | Контур B — исследователь |
|---|---|---|
| Кто решает | чистый Python + SQLite governor | parent-модель в пределах governor |
| Частота | cron каждые 2 мин | cron каждые 25 мин |
| Задача | запуск/вытеснение/завершение прогонов, гейты, бюджет, exclusive experiment lease | искать сигналы, bounded flat fan-out, строить гипотезы, убивать их, писать вердикты |
| GPU policy | один experiment; testing/analyze закрывают research admission | 0..N leaves только в discover, после reserve |
| Цена сбоя | тик идемпотентен и fail-closed | пропущенный тик ничего не ломает, summary не меняет scientific state |

Следствие: если модель упала, зависла или выдала чепуху, эксперименты всё равно идут,
а вердикты не появляются из ниоткуда. Если GPU-телеметрия неизвестна или research
worker не подтвердил checkpoint, новый inference/experiment не допускается.

## Поток работы

```
сигнал (signals/*.md)
   │  ≥3 независимых источника
   ▼
гипотеза (hypotheses/H-XXX.yaml) — 10 обязательных секций
   │  7 kill-проверок  ────▶ убита до GPU = успех (memory/killed.md)
   ▼
очередь (SQLite): PI → PPI = PI / GPU-часы → корзины P1..P4
   │  governor: mode? leases? VRAM/util? reserve? budget? pause?
   ▼
exclusive testing lease → каскад L0 (≤5 мин) → L1 (≤60 мин) → L2 (≤8 ч) → L3 (по решению человека)
   │  results/<H>/<level>/{metrics.jsonl,summary.json}
   ▼
вердикт: факт против ЗАФИКСИРОВАННОГО ранее прогноза
   │
   ├─▶ подтверждено → следующий уровень / заготовка патента
   └─▶ отвергнуто → урок + проверка соседних гипотез на тот же дефект
   ▼
калибровка весов PI по закрытым вердиктам (воскресенье)
```

## Состояние: одна точка истины

`state/researchagen.sqlite3` — таблицы `hypotheses`, `runs`, `verdicts`, `events`,
`settings`, `governor_leases`, `governor_reports` и `conclave_*`. Маркдаун/JSON-файлы (карточки,
сигналы, отчёты) — для человека и git-истории; база — для решений и admission.
Канбан Hermes — durable handoff/человеческое представление, а PI/PPI, kill-stage,
runs и verdict остаются authoritative в researchagen: статус нельзя читать
обратно, иначе появляется вторая правда и гонки.

## Почему PPI, а не PI

`PI = 0.22·S + 0.16·N + 0.12·E + 0.14·Q + 0.14·M + 0.22·D` — качество идеи.
`PPI = PI / оценка GPU-часов` — качество на единицу дефицитного ресурса.

На одной карте сортировка по PI ведёт к тому, что одна красивая 40-часовая гипотеза
съедает неделю и блокирует десять четырёхчасовых. Сортировка по PPI даёт больше
закрытых вопросов на тех же часах. Старение (+0.05/день, потолок +0.30)
не даёт долгим гипотезам висеть вечно.

## Почему каскад уровней

Каждый уровень отвечает ровно на один вопрос и не платит за следующий:

L0 — код вообще работает? L1 — эффект есть хотя бы на трёх seeds?
L2 — эффект выживает на реальном масштабе? L3 — это переносимый метод?

Пропуск уровня запрещён: именно так теряют десятки часов на опечатке в коде.

## Где живёт антисамообман

1. Прогноз пишется в карточку до запуска; `verdict.py` отказывает, если его нет.
2. Вердикт хранит отклонение факта от прогноза — и это главная метрика честности в дайджесте.
3. Калибровка отказывается работать при <8 вердиктах или одном классе исходов.
4. Шаг веса ограничен 20 %: приоритеты не переворачиваются из-за одной удачи.
5. Запрет оценочных слов в вердиктах проверяется тестом, а не просьбой в промпте.

## Изоляция от первого агента

Отдельный `HERMES_HOME/profiles/researchagen`, отдельный `.env`, отдельный токен,
отдельная модель (локальный Qwen вместо OpenRouter), отдельный терминал.
`selfcheck.py` сравнивает токены во всех `profiles/*/.env` и падает при совпадении:
два long-polling процесса на один токен — нерабочая конфигурация.

## Governor и native Hermes delegation

`tools/governor.py` — общий admission controller для всех Qwen-исследователей,
которые parent запускает через native `delegate_task` или Kanban worker. Parent
сначала получает план и берёт lease; governor не создаёт подагентов сам и не
заменяет Hermes. В `discover` dynamic capacity деградирует `2→1→0` по текущим
VRAM/utilization, reserve под эксперимент, budget и числу задач. В `testing`
исследовательские leases закрыты, а `dispatch.py` требует их checkpoint/stop
перед Popen. После `finish` стоит `analyze` до явного verdict.

`governor_reports` хранит только проверенные по форме child reports. Состояние
гипотез и evidence из отчёта не обновляется автоматически: parent обязан
проверить источники и пройти существующие `/h` → kill-stage → queue.

## Conclave: фиксированные зоны, спор по trigger, публичный transcript

Conclave добавляет коммуникационную плоскость, но не вторую очередь и не вторую
научную правду. Parent сначала считает `conclave.detect_triggers`: confidence gap,
source conflict, missing control, high-cost decision, stalled discussion или
commercial claim без counterfactual. Без trigger новый Qwen debate не создаётся.

При trigger `conclave.role_plan` получает текущие `available_slots` у governor и
выбирает `0..N` leaf workers. Персона — это стабильный responsibility contract:
`@Архивариус` проверяет источники, `@Кувалда` фальсифицирует, `@Адвокат`
steelman-ит, `@Паяльник` превращает claim в reproducible test, `@Касса` аудирует
ценность, `@Некролог` сжимает решение. Один слот означает честный parent self-review;
два — реальный falsifier/steelman debate. Максимум два раунда, после чего parent принимает решение. Каждый room получает
набор независимых challenge templates: `source-audit`, `falsification`, `steelman`,
`confounder`, `replication`, `value-check`, `decision`. Ни один child не пишет
hypothesis/evidence/verdict напрямую.

У Conclave две языковые поверхности: English internal protocol для reasoning и
короткие русские public summaries для Telegram. Hidden chain-of-thought не хранится.
`conclave_messages` — только публичный transcript; `conclave_phrase_events` хранит
выбранный nudge, его prior и последующий outcome. 95%/90% — design priors, а не
измеренные гарантии. `conclave speak` может отправить task-комментарий и иногда
одновременный client-комментарий в разные topics. `tg.py` остаётся outbound-only,
поэтому второй Telegram long-polling процесс не появляется.

Когда фаза `testing/analyze/paused`, Conclave не может получить research lease.
Если Telegram недоступен, сообщения всё равно остаются в SQLite и transcript можно
доставить позднее. Закрытие комнаты освобождает lease, но не меняет scientific state:
для этого parent использует штатные `hypo.py`/kill-stage/`verdict.py`.

## Bottom Detection как гибридный поисковый слой

Bottom Detection не заменяет `/dr` и не создаёт второй профиль. Это mission-scoped
слой ширины поиска: он строит дерево регионов, запускает асинхронные evaluators,
делает backtracking и контролируемые трансформации, а затем передаёт кандидата в
тот же `hypotheses/` → kill-stage → SQLite queue → dispatch cascade. Его состояние
(`bd_regions`, `bd_hypotheses`, `bd_evidence`, `bd_history`, `bd_cache`, `bd_runs`)
живет в той же базе, поэтому `/status` и поисковый слой не могут незаметно увидеть
разные очереди.

Вызов: `python tools/rg.py bottom run --iterations N`. Native MCP Hermes остаётся
владельцем сетевого tool-calling; Python transport подключается только явно через
adapter. Нет adapter — это честный режим без literature evidence, а не выдуманное
подтверждение. Трансформированные кандидаты получают новый ID и нулевые scores;
их evidence нужно заработать повторно.

## Почему без внешних зависимостей

Профиль ставится в среду, где уже живёт другой агент. Любой `pip install` — риск
сломать чужое окружение. Поэтому: `sqlite3`, `json`, `urllib`, `argparse`, `subprocess`,
собственный мини-парсер YAML для конфига. PyTorch нужен только внутри самих
экспериментов и импортируется лениво — без него всё остальное работает.
