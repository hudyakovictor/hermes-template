---
name: conclave
description: Бounded persona-критика, короткий русский Telegram-чат и решение главного агента без автоматической подмены scientific state.
version: 1.0.0
---

# /conclave — комната спорных идей

Показать текущий состав без Qwen-вызова:

```bash
python tools/rg.py conclave cast
```

Conclave — не фиксированный swarm и не театр вместо науки. Главный Hermes-агент
остаётся `Шефом`: он выбирает, нужен ли спор, сколько leaf-workers позволены
сейчас, какие зоны им назначить, проверяет отчёты и принимает решение.

## 1. Когда открывать спор

Спор стоит compute только в особых случаях:

- два отчёта расходятся по confidence хотя бы на `trigger_confidence_gap`;
- источники или claims противоречат друг другу;
- отсутствует control/falsification condition;
- решение достаточно дорогое, чтобы ошибка сожгла существенный GPU-бюджет;
- обсуждение зациклилось и повторяет тезисы без нового источника;
- коммерческий вывод обещает «кнопку бабло», но не имеет counterfactual.

Собери факты в `context.json`, затем выполни:

```bash
python tools/rg.py conclave plan --task-id H-003 --stage critique \\
  --context-file context.json --json
python tools/rg.py conclave open --task-id H-003 --title "H-003: спор о механизме" \\
  --stage critique --context-file context.json --json
```

Если governor сообщает `testing`, `analyze`, `paused` или `capacity=0`, новые
Qwen-вызовы запрещены. Не изображай второе мнение: запиши `parent_self_review` и
отложи Conclave до освобождения ресурса.

## 2. Распределение зон

Сначала прикрепи каждого leaf к зоне и личности:

```bash
python tools/rg.py conclave assign --session D-... --json
python tools/rg.py conclave brief --session D-... --assignment A-... --json
```

Роли фиксированы:

| Ник | Зона | Работа |
|---|---|---|
| `@Архивариус` | `source_audit` | первоисточники, provenance, противоречия, дубли |
| `@Кувалда` | `falsification` | самый дешёвый способ убить claim, missing controls |
| `@Адвокат` | `steelman_and_defense` | сильнейшая версия идеи и честный ответ на objections |
| `@Паяльник` | `mechanism_and_implementation` | минимальный reproducible test, instrumentation, seeds |
| `@Касса` | `value_and_customer_risk` | стоимость, latency, adoption, downside и counterfactual |
| `@Некролог` | `synthesis_and_public_context` | короткое решение и список неизвестного |

Реальных работников всегда `0..available_slots`. При одном слоте главный агент
не подделывает консенсус, а делает parent self-review. Для настоящего спора нужны
два независимых leaf (`@Кувалда` + `@Адвокат`, либо `@Архивариус` + `@Кувалда`
при конфликте источников). Nested delegation запрещён.

## 3. Протокол leaf

`brief` может случайно добавить одному worker короткий context-aware nudge (это
записывается как exposure и не является фактом эффективности). Он содержит две
разные языковые плоскости:

1. **internal reasoning:** English, короткая evidence-backed rationale, без вывода
   chain-of-thought;
2. **public communication:** русский, одна короткая реплика в стиле выбранной
   личности и формат `POSITION / EVIDENCE / OBJECTION / NEXT`.

Child не пишет напрямую в `hypotheses`, `evidence`, queue или verdict. После
работы его JSON обязан пройти:

```bash
python tools/rg.py conclave report --session D-... --assignment A-... \\
  --file reports/worker.json --json
```

`task_id` в отчёте обязан совпадать с task id assignment. Valid report всё ещё
`review_pending`; только parent проверяет source, дедуплицирует claim и вызывает
обычные `/h`, kill-stage и `/v`.

## 4. Шаблоны устойчивости

`brief` выдаёт набор коротких challenge templates. Parent выбирает только нужные
для trigger, чтобы спор не превратился в сериал:

- `source-audit` — точная первичная ссылка, цитата, дата и условие опровержения;
- `falsification` — самый дешёвый decisive test, metric, control, stop condition;
- `steelman` — сильнейшая версия противоположной позиции до ответа;
- `confounder` — скучное альтернативное объяснение и разделяющее наблюдение;
- `replication` — seeds, held-out setting, minimum effect и граница переноса;
- `value-check` — counterfactual, стоимость, latency и downside для заказчика;
- `decision` — strongest evidence, strongest objection, uncertainty и next action.

Так «спор» проверяет прочность claim по нескольким независимым швам, а не
назначает победителем самого язвительного персонажа.

## 5. Раунды спора

Максимум два раунда:

- **R1:** `@Кувалда` формулирует самый сильный удар; `@Адвокат` сначала steelman,
  затем отвечает по каждому objection;
- **R2:** оба называют ровно один новый тест/источник или признают unresolved;
  `Шеф` выносит `continue`, `kill`, `queue` или `self-review`.

Риторическая победа не считается evidence. Заранее зафиксируй, что опровергнет
идею. Если спор дважды повторяет слова без нового факта — `stalled`, отправь
nudge и закрой комнату.

## 6. Публичный Telegram-чат

Это outbound transcript, а не второй long-polling bot:

```bash
python tools/rg.py conclave speak --session D-... --assignment A-... \\
  --kind critique --round 1 \\
  --task "Тезис требует контроля: ..." \\
  --client "Человек снова хочет кнопку бабло; выдаём пока кнопку проверить."
python tools/rg.py conclave transcript --session D-... --send
```

`--task` публикуется в topic `TELEGRAM_CONCLAVE_THREAD_ID`, а `--client` иногда
(по вероятности в конфиге) одновременно — в `TELEGRAM_CLIENT_THREAD_ID`. Дебаты
можно направлять в `TELEGRAM_DEBATE_THREAD_ID`. Каждая реплика хранится в
SQLite, поэтому transcript можно повторить после сетевого сбоя.

Стиль разрешает сарказм, умеренный мат, trolling и чёрный юмор, но удар направлен
на предположение/метод, а не на защищаемый признак или частного человека. Угроза,
призыв к насилию и утечка hidden reasoning удаляются. Шутка может украсить datum,
но не может заменить datum.

## 7. Нуджи и проверка их пользы

Каталог содержит короткие контекстные фразы. Поля `prior_effectiveness=0.95` и
`target_positive_effect=0.90` — запрошенные design priors, не доказанная гарантия.
Выбор случайный и записывается в `conclave_phrase_events`:

```bash
python tools/rg.py conclave nudge --session D-... --assignment A-... --send
python tools/rg.py conclave phrases --json
python tools/rg.py conclave outcome --session D-... \\
  --phrase nudge.fact-before-fight --outcome positive
```

`positive` означает, что parent зафиксировал наблюдаемый эффект: спор перестал
зацикливаться, появился новый проверяемый datum или участники перешли к решению.
Без такой отметки 95/90 остаются красивыми цифрами на плакате.

## 8. Завершение

```bash
python tools/rg.py conclave close --session D-... \\
  --decision "Критика сильнее: сначала L0 control" --announce
```

Закрытие освобождает research leases, но не меняет scientific state. Hypothesis,
evidence и verdict изменяются только штатными инструментами parent-а.
