---
name: governor
description: Показать и управлять автономным admission-контуром research workers и GPU-экспериментов.
version: 1.0.0
---

# /governor — resource governor

Главный Hermes-агент принимает решение сам, но не имеет права обходить
детерминированный SQLite lease gate.

## Проверка

```bash
python tools/rg.py governor status --json
python tools/rg.py governor plan --mode auto --json
python tools/rg.py governor leases --json
```

`capacity` — текущий потолок LLM research workers, `available_slots` — сколько
можно добавить сейчас. Он вычисляется по режиму, active experiment, GPU VRAM и
utilization, reserve эксперимента, дневному admission budget и числу уже взятых
leases. Это не постоянный размер swarm.

## Режимы

```bash
python tools/rg.py governor mode discover
python tools/rg.py governor mode triage
python tools/rg.py governor mode testing
python tools/rg.py governor mode analyze
```

`testing` закрывает новые research/Qwen leases и запрашивает pause активных
workers. Native `delegate_task stop/steer` выполняет parent, после чего он
подтверждает checkpoint. `analyze` удерживается до `/v`; только после вердикта
контур возвращается в `discover`.

## Lease protocol

1. parent создаёт logical task id;
2. `governor reserve --worker-id ... --task-id ...`;
3. native Hermes `delegate_task` получает тот же task id в context;
4. child возвращает структурированный report;
5. parent валидирует report и делает `release`.

Для паузы:

```bash
python tools/rg.py governor pause --lease <id>
# native delegate_task steer/stop + запись checkpoint
python tools/rg.py governor checkpoint --lease <id> --checkpoint <path>
# или
python tools/rg.py governor stop-confirm --lease <id>
```

`resume` разрешён только из `discover/triage` и при положительной новой
capacity. Не возобновляй все leases автоматически: parent выбирает только
валидные незавершённые задачи.

## Report gate

```bash
python tools/rg.py governor report --file reports/worker-T-001.json \
  --worker-id R-001 --json
```

Проверка формы не меняет `hypotheses`, `bd_*` или evidence. Даже valid report
помечен `review_pending`; parent обязан проверить первоисточники, дедуплицировать
claims и пройти обычные `hypo.py`/kill-stage.
