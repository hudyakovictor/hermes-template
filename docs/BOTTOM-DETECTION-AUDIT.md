# Bottom Detection: дополнительный аудит

Дата аудита: `2026-08-29`.

Проведены 30 независимых проверок текущего гибридного слоя. В локальном
stdlib-only окружении результат: **30/30 PASS**.

## Матрица проверок

| # | Проверка | Результат |
|---:|---|---|
| 01 | фиксированная ветка Arena и отсутствие смены branch | PASS |
| 02 | импорт всех модулей `tools/bottom_detection` | PASS |
| 03 | AST/compile validation | PASS |
| 04 | полный regression unittest suite | PASS, 79 тестов |
| 05 | executable-line coverage нового пакета | PASS, 87.95% |
| 06 | повторяемость 150-сценарного study-runner | PASS, hybrid 96% threshold pass |
| 07 | CLI JSON smoke и два последовательных cron-тика | PASS, iteration 0 → 1 → 2 |
| 08 | отключение через quoted `false` | PASS |
| 09 | clamp конфигурации и cap evaluators | PASS |
| 10 | idempotent SQLite schema с `bd_*` таблицами | PASS |
| 11 | изоляция namespace для разных mission/domain | PASS |
| 12 | fallback при malformed persisted JSON | PASS |
| 13 | append-only history и транзакция событий | PASS |
| 14 | validation и deduplication region/candidate | PASS |
| 15 | async evaluator concurrency | PASS, peak 2 в тесте |
| 16 | изоляция падения одного evaluator | PASS |
| 17 | cancellation не оставляет run в `running` | PASS |
| 18 | cost limit останавливает следующий iteration | PASS |
| 19 | fan-out по arxiv/pubmed/scholar/github с provenance | PASS |
| 20 | retry/backoff и terminal adapter error | PASS |
| 21 | SQLite TTL cache и expiry | PASS |
| 22 | shared MCP rate limiter | PASS |
| 23 | безопасный JSON-command adapter | PASS |
| 24 | HTTP adapter round trip через stdlib | PASS |
| 25 | malformed MCP rows не создают false evidence | PASS |
| 26 | три transformation families и нейтральность child | PASS |
| 27 | backtracking parent/sibling frontier invariant | PASS |
| 28 | четыре секции финального формата | PASS |
| 29 | promotion gate, queue handoff и idempotence | PASS |
| 30 | installer markers, bundle и documentation integration | PASS |

## Исправления, сделанные по результатам аудита

1. `run --iterations 1` после первого тика больше не становится no-op: лимит
   трактуется как лимит текущего вызова, а cumulative counter сохраняется в state.
2. Cancellation теперь записывает `run.cancelled`, а не оставляет `bd_runs` в
   состоянии `running`.
3. Promotion стал идемпотентным: повторный вызов возвращает уже созданную обычную
   карточку очереди и не создаёт дубликат.
4. Transformation child больше не наследует `signal_sources`, mechanism и forecast;
   сохраняется только provenance через `origin_id` и metadata.
5. Transformation output распределяется round-robin, чтобы при лимите кандидатов
   не вытеснялись synonym, related-concept или cross-domain families.
6. `tools/queue.py` экспортирует совместимый `SimpleQueue`, поэтому имя legacy
   queue-модуля не ломает `concurrent.futures` и stdlib HTTP transport.
7. Numeric/null/object MCP rows отбрасываются вместо превращения в псевдо-claims.
8. Malformed cache payload удаляется при чтении, а quoted `false` корректно отключает
   Bottom Detection CLI.
9. Удалена устаревшая installer-подстановка `INSTALLER_DEBUG_MODE`, для которой уже
   нет маркера в `config.yaml`; оставлена актуальная пара platform/mode.

## Границы вывода

30/30 означает, что проверенные локальные контракты и failure paths проходят в
текущем окружении. Это не означает 99% полноты и не заменяет проверку живых
Hermes native MCP, Telegram gateway, Windows PowerShell и реального GPU-контура.
Для этих границ Bottom Detection по-прежнему не выдумывает evidence и не получает
права запускать GPU без существующих queue/kill/verdict gates.
