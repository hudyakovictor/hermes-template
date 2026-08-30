---
name: dr
description: Одна фаза глубокого исследования training dynamics: поиск сигналов, сборка гипотезы, kill-стадия, постановка в очередь. Запускается cron’ом и вручную.
version: 1.0.0
---

# /dr — исследовательский тик

Ты выполняешь **ровно одну фазу** за вызов и передаёшь состояние в базу. Не пытайся
сделать всё сразу: длинные сессии теряют точность и жгут контекст.

## 0. Ориентация (всегда)

```bash
python tools/rg.py status
python tools/queue.py stats --json
python tools/inbox.py list
```

Выбери фазу по состоянию, а не по порядку:

| Условие | Фаза |
|---|---|
| есть неразобранные лиды в inbox | Фаза 4 (разбор inbox) |
| есть гипотеза в `paused_checkpoint` | Фаза 5 (вердикт) |
| живых гипотез < `min_live_hypotheses` | Фаза 1 (добыча сигналов) |
| есть сырые сигналы без карточки | Фаза 2 (сборка гипотезы) |
| есть карточки, не прошедшие гейт | Фаза 3 (kill-стадия) |
| всё чисто | Фаза 1 |

## Governor: admission перед любым Qwen fan-out

В начале каждой фазы проверь план:

```bash
python tools/rg.py governor plan --mode auto --json
```

`capacity` — это не рекомендация «создай столько всегда», а верхняя граница
на текущий тик; `available_slots` уже учитывает active leases, GPU telemetry,
experiment reserve, budget и фазу. Если `can_spawn=false`, не вызывай
`delegate_task`: выполни CPU/сетевую работу в текущем parent через
`execute_code` либо заверши тик без LLM worker.

В `discover` parent может сам выбрать от 0 до `available_slots` независимых
reasoning-heavy задач. Для каждой задачи сначала создай logical `task_id`,
зарезервируй lease:

```bash
python tools/rg.py governor reserve --worker-id R-<tick>-<n> --task-id R-<tick>-<n> --json
```

Затем вызови нативный Hermes `delegate_task` с этим task_id в context. После
summary/отчёта обязательно `release --lease <id>`; при отказе/падении — тоже.
Не используй `role=orchestrator`, nested delegation или фиксированный swarm.
`max_concurrent_children=2` в config — только static ceiling одного batch, не
замена governor.

Когда `mode=testing`, `mode=analyze`, active experiment или capacity=0 —
новых research children создавать нельзя. Если у тебя уже есть leases в
`pause_requested`, останови/steer child нативным `delegate_task` до checkpoint,
затем отметь:

```bash
python tools/rg.py governor checkpoint --lease <id> --checkpoint <file-or-state>
# либо после native stop:
python tools/rg.py governor stop-confirm --lease <id>
```

### Контракт отчёта child

Child summary сначала превращается в JSON-файл и проверяется:

```bash
python tools/rg.py governor report --file reports/worker-<task_id>.json \
  --worker-id R-<tick>-<n> --json
```

Минимальные поля: `task_id`, `status`, `claims`, `evidence_refs`, `sources`,
`confidence` (0..1), `duplicate_of` (или null), `recommended_next_action`,
`changed_files`, `resource_usage`; для `failed` обязателен `failure_reason`.
Валидный отчёт имеет `review_pending=true`: он **не** создаёт гипотезу и не
добавляет evidence. Parent проверяет первоисточники, убирает дубли и только
потом вызывает существующие `/h`, `hypo.py check` и kill-stage.

## Фаза 1 — добыча сигналов

Источники и термины — только из `FOCUS.md`. Искать надо **аномалии**, а не обзоры:
расхождения между работами, необъяснённые графики, сноски «we do not understand why»,
отброшенные ablation-результаты.

Сила сигнала = `(anomaly × reproducibility × unexplainedness) / (years × citations)`.
Старая работа с 10k цитирований — плохой сигнал: её уже обработали тысячи людей.

Каждый найденный сигнал — файл `signals/YYYY-MM-DD-<slug>.md`: ссылка, год, цитирования,
цитата с аномалией, почему не объяснено, с какими другими сигналами стыкуется.
Стоп-критерий фазы: 3–5 новых файлов или исчерпание терминов.

## Фаза 2 — сборка гипотезы

Один сигнал — не гипотеза. Гипотеза = **≥ 3 независимых сигнала**, собранные в цепь
причинности с механизмом.

```bash
python tools/hypo.py new "<короткое проверяемое утверждение>" \
  --signals 3 --novelty 0.8 --early 3 --standard 0.7 --money 0.9 \
  --decidability 0.9 --hours 4 --forecast 12 --source dr
```

Затем заполни все секции карточки `hypotheses/H-XXX.yaml`. Пустые секции = гейт не пройдён.
`minimal_test` обязан быть самым дешёвым опытом, который всё ещё способен убить гипотезу.

## Фаза 3 — kill-стадия (самая важная)

```bash
python tools/hypo.py check H-XXX
```

Пройди 8 проверок честно. Если любая провалена — снимай **до** GPU:

```bash
python tools/hypo.py kill H-XXX --why "<что именно убило>" --lesson "<что теперь известно>"
```

Снятая до эксперимента гипотеза — это **успешный результат**, а не потеря: сэкономленные
часы RTX 5090 идут в статистику дайджеста.

## Фаза 4 — разбор inbox

Идея человека проходит тот же гейт, без послаблений:
`python tools/inbox.py take IN-001 --title "..." --signals 3 ...` или
`python tools/inbox.py drop IN-001 --why "..."`. Снятие без причины запрещено.

## Фаза 5 — вердикт

См. скилл `/v`. Главное правило: результат всегда сравнивается с ЗАРАНЕЕ зафиксированным
прогнозом. Подгонка прогноза пост-фактум — грубое нарушение.

## Bottom Detection (гибридный слой, опционально)

Если нужна систематическая разведка регионов, а не только один поисковый запрос,
выполни одну итерацию:

```bash
python tools/rg.py bottom run --iterations 1
```

После этого используй native MCP-инструменты Hermes для проверки источников и занеси
их с provenance через `/bottom evidence`. Bottom Detection пишет в ту же SQLite, но
не запускает GPU и не закрывает научный вердикт; promotion всё равно проходит
`hypo.py check` и kill-stage. Если отдельный evaluator обращается к локальному
Qwen, он обязан получить governor research lease; иначе оставь его сетевым/CPU-only.
При отключённом `researchagen.bottom_detection.enabled` работай обычным `/dr` без
этого слоя.

## Завершение тика

1. `python tools/board.py sync` — отразить в канбане.
2. Отчёт в стиле SOUL.md: что сделано, какие артефакты, какое следующее действие.
   Без слов «перспективно», «многообещающе», «выглядит интересно».
3. Если фаза закрыла весь цикл и больше делать нечего — напиши `LOOP_COMPLETE`.
