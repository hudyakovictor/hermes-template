# Bottom Detection: решение по интеграции

## Решение

Выбран **гибрид в текущем профиле**, а не второй шаблон и не замена
researchagen целиком.

- `MISSION.md` остаётся единственным источником предметного контекста.
- `tools/bottom_detection/` — опциональный stdlib-only orchestration layer:
  регионы, кандидаты, evidence, async evaluators, backtracking, transformations,
  TTL-cache, rate limit, retries, JSON logging и Prometheus text metrics.
- `state/researchagen.sqlite3` остаётся единой точкой истины. Для слоя добавлены
  mission-scoped таблицы `bd_*`; они не смешиваются между разными текстами миссии.
- `queue.py`, `hypo.py`, `dispatch.py` и `verdict.py` остаются владельцами
  promotion, GPU-лимитов, kill-stage и научного вердикта.
- `skills/bottom/SKILL.md` — тонкий Hermes-интерфейс, а не второй runtime.

Второй шаблон создал бы две очереди, две истории и два набора правил. Полная замена
лишила бы текущий профиль проверенных GPU- и verdict-гейтов. Поэтому Bottom Detection
добавляет исследовательскую ширину, но не получает права тратить GPU или закрывать
гипотезу.

## Результат 150 симуляций

Запуск воспроизводится без внешних зависимостей:

```bash
python tools/bottom_study.py 150
```

Seed: `20260829`. В каждой симуляции сравнивались три варианта по взвешенной шкале
совместимости с Hermes, покрытия требований, надёжности, отсутствия внешних
зависимостей, сопровождения, обратимости и расширяемости. Порог прохождения — `0.80`.

| Вариант | Средний score | P05 | Прошёл порог | Победил |
|---|---:|---:|---:|---:|
| Полная замена профиля | 0.5215 | 0.4151 | 0/150 | 0/150 |
| Дополнительный автономный шаблон | 0.7126 | 0.6812 | 0/150 | 0/150 |
| Гибридный слой | **0.8948** | **0.8778** | **144/150 = 96%** | **150/150 = 100%** |

Модель также содержит 3% сценариев residual integration-boundary incident с
ограниченным штрафом для каждого варианта; поэтому 96% — это результат модели,
а не обещание production SLO.

Это **архитектурная Monte Carlo-модель**, а не эмпирическая вероятность качества
научных открытий. Она подтверждает выбор при зафиксированных предположениях; живой
MCP, Hermes gateway, Telegram и GPU-прогоны должны быть проверены отдельно.

## Как закрыты требования

| Требование | Реализация |
|---|---|
| MISSION.md на входе | `SkillConfig.from_profile()` читает `MISSION.md`; namespace зависит от mission+domain |
| Регионы и кандидаты | seed-регионы из миссии, `add_region`, deterministic fallback candidates |
| Async/параллельность | `asyncio.gather`; глобальный semaphore ограничен 1–10 evaluators |
| Backtracking | region tree, `backtracked` state, возврат parent и siblings на frontier |
| Полная история | `bd_history` append-only + materialized `bd_*` state |
| Приоритет | `P=.25E+.20N+.20M+.15X+.10C+.10D`; bounded `[0,1]` scores |
| Формат вывода | `format_verdict`: SIGNAL → HYPOTHESIS → EXPERIMENT PLAN → VERDICT |
| MCP | optional HTTP или JSON-command transport; native Hermes MCP остаётся у Hermes |
| L0–L3 | candidate metadata и promotion в существующий cascade `dispatch.py` |
| Custom evaluators | `BottomDetectionSkill.register_evaluator()` |
| Structured logging | stdlib `JsonFormatter` |
| Metrics | stdlib Prometheus text exporter |
| Rate limiting/cache/retry | sliding-window limiter, SQLite TTL 24h, exponential backoff |
| Limits | `max_iterations`, `max_cost_usd`, evaluator cap, region depth/candidate caps |
| Тесты/coverage | 62 теста; stdlib AST/trace harness даёт 87.56% executable-line estimate |

Проверка тестов и покрытия:

```bash
python -m unittest discover -s tests -q
python tools/bottom_coverage.py
```

Docker, `aiohttp`, `structlog`, `pytest` и другие внешние зависимости не добавлены:
это сознательное соответствие инварианту профиля «чистая Windows/macOS + stdlib».
Изоляция экспериментов остаётся обязанностью существующего Hermes terminal backend
и `dispatch.py`; Bottom Detection сам GPU-процессы не запускает.

## Команды

```bash
python tools/rg.py bottom init
python tools/rg.py bottom run --iterations 1
python tools/rg.py bottom regions --json
python tools/rg.py bottom candidates --json
python tools/rg.py bottom stats --json
python tools/rg.py bottom evidence <candidate-id> \
  --source "https://..." --independent "paper-or-lab" \
  --claim "точная аномалия" --strength 0.8
python tools/rg.py bottom promote <candidate-id>
```

В формуле `E` — независимое evidence, `N` — novelty, `M` — mechanism, `X` —
experiment, `C` — commercial value, `D` — decidability. Все входы
ограничены `[0,1]`; absence of evidence остаётся нулём, а не скрытым prior.

Promotion требует минимум три независимых evidence и заранее зафиксированный
forecast. После promotion обычный `tools/hypo.py check` всё равно блокирует запуск,
если карточка не заполнена и kill-stage не пройден.
