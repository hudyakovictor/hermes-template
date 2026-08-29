---
name: bottom
description: Bottom Detection — mission-scoped поиск по регионам с backtracking, трансформациями и асинхронными evaluators; результат не обходит очередь и GPU-гейты researchagen.
version: 0.1.0
---

# /bottom — Bottom Detection

Это **гибридный исследовательский слой**, а не второй профиль и не второй источник
правды. `MISSION.md` задаёт неизменный контекст; SQLite researchagen хранит
регионы, кандидатов, evidence и историю; `queue.py`, `hypo.py`, `dispatch.py` и
`verdict.py` по-прежнему принимают окончательные решения о GPU и исходе.

## Быстрый цикл

```bash
python tools/bottom_detection_cli.py init
python tools/bottom_detection_cli.py run --iterations 1
python tools/bottom_detection_cli.py regions
python tools/bottom_detection_cli.py candidates
python tools/bottom_detection_cli.py stats
```

По умолчанию слой создаёт регионы из `MISSION.md`, оценивает кандидатов
параллельными stdlib-evaluators и сохраняет каждое действие в `bd_history`.
Остановка ограничена `max_iterations` и `max_cost_usd` в `config.yaml`.

## Как работать с MCP

Hermes остаётся владельцем native MCP-инструментов. Найденные факты надо переносить
в exploratory state с provenance:

```bash
python tools/bottom_detection_cli.py evidence HYPOTHESIS_ID \
  --source "https://arxiv.org/abs/..." \
  --independent "paper-or-lab-id" \
  --claim "точная аномалия или контрольный результат" \
  --strength 0.8
```

Если настроен Python adapter, он использует `mcp_endpoints` или `mcp_commands`,
rate limit, TTL-кэш 24 часа и retry с exponential backoff. Отсутствие MCP не
маскируется за подтверждённое evidence: evaluator возвращает нейтральный результат.

## Регионы, расширение и backtracking

- `refine` углубляет регион в причинный механизм и контроль;
- `expand` создаёт дочерние ветви early predictor и negative control;
- `transform` применяет синонимы, смежные концепции и cross-domain analogies;
- `backtrack` закрывает слабую ветвь и возвращает родителя/соседей на frontier;
- повторный запуск продолжает тот же mission namespace, а не начинает заново.

Трансформированный кандидат получает новый ID, `origin_id` и нейтральные оценки.
Наследование красивого score запрещено: evidence и проход evaluators нужно получить
заново.

## Promotion в основной контур

```bash
python tools/bottom_detection_cli.py promote HYPOTHESIS_ID
python tools/hypo.py check H-XXX
python tools/rg.py next
```

Promotion разрешён только после трёх независимых evidence и зафиксированного
forecast. Он создаёт обычную карточку `hypotheses/H-XXX.yaml`; пустые секции и
kill-stage всё ещё блокируют запуск. `/bottom` никогда не запускает GPU напрямую.

## Формат результата

Каждый top-кандидат форматируется строго в четыре секции:

`SIGNAL` → `HYPOTHESIS` → `EXPERIMENT PLAN` → `VERDICT`

`VERDICT` exploratory-кандидата — это статус и следующее действие, а не научное
подтверждение. Научный вердикт появляется только через `tools/verdict.py` после
реального артефакта и прогноза.
