# Autonomous Hermes governor

## Decision

Для одной локальной GPU оптимален не безусловный swarm, а **один parent
governor + bounded flat research leaves + эксклюзивная durable experiment
queue**.

Parent Hermes остаётся единственным владельцем решений:

- создавать ли child вообще;
- сколько независимых research-задач допустить на текущем тике;
- какой task/context/output contract дать каждому child;
- когда остановить, поставить на checkpoint или возобновить worker;
- какие отчёты принять после проверки и что отправить в scientific queue.

`delegate_task` используется для коротких reasoning-heavy задач. `execute_code`
— для циклов, фильтрации, дедупликации и агрегации telemetry. Native Kanban
используется для durable handoff, который должен пережить restart и сменить
worker. Ни один из этих механизмов сам по себе не знает, сколько VRAM оставляет
локальной Qwen под эксперимент, поэтому вокруг них нужен этот lease gate.

## Почему не recursive swarm

Нативный Hermes запускает children с изолированным контекстом и возвращает
parent только финальный summary. Batch-конкурентность конфигурируется, но это
верхняя граница batch, а не общий GPU lock. Nested delegation — отдельный
opt-in режим; дерево быстро умножает количество inference calls. Поэтому в
профиле зафиксированы:

```yaml
delegation:
  max_concurrent_children: 2
  max_spawn_depth: 1
  orchestrator_enabled: false
```

Два — только консервативный ceiling. Parent на каждом тике может выбрать 0, 1
или 2 workers, но реальный ответ даёт governor.

## State machine

```text
DISCOVER ──candidate/kill-stage──> TRIAGE ──admit experiment──> TESTING
    ▲                                  │                          │
    │                                  └───────────────┐          │
    │                                                  ▼          │
    └──────────── verdict (/v) <── ANALYZE <── finish/checkpoint
```

| Mode | New research Qwen | GPU experiment | Meaning |
|---|---:|---:|---|
| `discover` | dynamic 0..N | not started by this phase | independent evidence collection |
| `triage` | dynamic 0..1 | not started until candidate is ready | kill-stage/synthesis |
| `testing` | 0 | exactly one | experiment owns the GPU lane |
| `analyze` | 0 | no next run | result is waiting for explicit verdict |
| `paused` | 0 | no new run | operator/safety pause |

A running `runs.state='running'` always overrides a stale mode setting and makes
capacity zero for research. `dispatch.py` obtains an exclusive experiment lease
and refuses to start while a research lease is `active`, `pause_requested` or
`stop_requested`. `--force` can bypass a scientific checklist, never this
resource lock.

## Admission calculation

`python tools/rg.py governor plan --json` returns both the decision and reasons.
The total capacity is:

```text
capacity = min(
    configured mode cap,
    telemetry capacity,
    remaining research admission budget,
    number of independent tasks offered by parent
)

If `queue.pick_next` has a candidate that already passes `hypo.py check`,
`testing priority` forces research capacity to `0` until the dispatcher gets
the sequential experiment started. This prevents a last research batch from
racing the experiment queue.

available_slots = max(0, capacity - active research leases)
```

Telemetry capacity is calculated from the best visible GPU:

- production without valid `nvidia-smi` telemetry: `0` (fail closed);
- utilization at/above `saturated_utilization_pct` or VRAM below the critical
  floor: `0`;
- high utilization or low free VRAM: at most `1`;
- otherwise `floor((free_vram - experiment_reserve) /
  research_worker_vram)` capped by configured max.

The initial values in `config.yaml` are deliberately conservative estimates.
`research_worker_vram_gb` is incremental KV/cache headroom, not model weights;
measure it on the real Qwen endpoint before increasing it or the child cap.
`daily_research_task_budget` is an admission-unit budget, not a fabricated token
meter. Actual token/GPU usage belongs in `resource_usage` in the child report.
A completed live benchmark can tighten (never raise) the cap through
`settings.governor.measured_max_concurrency`.

## Pause protocol

When the parent or dispatcher enters `testing`:

1. new research claims are closed in SQLite;
2. the research cron job is paused when the Hermes CLI is available;
3. active leases become `pause_requested`;
4. the parent uses native `delegate_task stop` or `steer` to finish at a safe
   checkpoint; it must not kill the running experiment;
5. the parent records `checkpoint`, or confirms `stop`;
6. only then can `dispatch.py` acquire the experiment lease.

`finish` releases the experiment lease but enters `analyze`. The next research
cron run and the next experiment stay blocked until `/v` records the result;
`verdict.py` returns the governor to `discover`.

A lease has a TTL and heartbeat. Expiry is an audit event; an `expired` research
lease remains an unresolved blocker until the parent confirms stop (it is never
silently treated as free). Expiry is not permission to assume a live experiment
stopped. Experiment rows in `runs` remain the final safety check.

## Child report contract

A child must return a JSON report with:

```json
{
  "task_id": "R-2026-001-1",
  "status": "completed",
  "claims": [
    {"claim": "...", "evidence_refs": ["E-1"], "confidence": 0.62}
  ],
  "evidence_refs": ["E-1"],
  "sources": ["https://example.org/primary-source"],
  "confidence": 0.62,
  "duplicate_of": null,
  "recommended_next_action": "parent verifies source and compares with H-014",
  "changed_files": ["signals/2026-08-29-example.md"],
  "resource_usage": {
    "duration_seconds": 42,
    "qwen_requests": 1,
    "gpu_seconds": 35,
    "tokens": null
  },
  "failure_reason": null
}
```

`python tools/rg.py governor report --file ...` validates the shape and stores
an audit row. It always returns `review_pending=true` and
`scientific_state_changed=false`. A valid report is not a hypothesis, evidence,
or verdict. The parent must inspect the primary source, remove duplicates, then
use the existing signal/hypothesis/kill-stage pipeline.

## Native Hermes mapping

| Need | Hermes primitive | Governor rule |
|---|---|---|
| Short independent search/critique | `delegate_task` | flat leaf, reserve first, 0..capacity workers |
| Mechanical bulk work | `execute_code` | no LLM child and no research lease |
| Restart-safe handoff | Kanban | named worker calls/obeys lease before Qwen |
| Scheduled research | cron | `hermes cron pause/resume` follows testing/analyze |
| GPU experiment | existing SQLite queue + `dispatch.py` | exclusive experiment lease, max one |
| Scientific truth | existing `hypotheses`, `bd_*`, verdict tables | governor never promotes state |

The Hermes Kanban board remains an optional view/worker transport. The
researchagen SQLite queue remains authoritative for PI/PPI, kill-stage, runs and
verdicts; `tools/board.py sync` is one-way and cannot overwrite scientific
status.

## Commands

```bash
python tools/rg.py governor status --json
python tools/rg.py governor mode discover
python tools/rg.py governor mode testing
python tools/rg.py governor reserve --worker-id R-001 --task-id R-001-1 --json
python tools/rg.py governor pause --lease r-abc123
python tools/rg.py governor checkpoint --lease r-abc123 --checkpoint reports/R-001.json
python tools/rg.py governor stop-confirm --lease r-abc123
python tools/rg.py governor resume --lease r-abc123
python tools/rg.py governor release --lease r-abc123
```

Live calibration against the configured OpenAI-compatible Qwen endpoint:

```bash
python tools/governor_benchmark.py run --concurrencies 1,2 \
  --requests-per-level 3 --output reports/qwen-governor.json
```

The benchmark takes the same exclusive experiment lease, records p50/p95,
throughput, errors and GPU snapshots, and proposes a ceiling only when the
measured p95 stays within 1.25x of sequential p95 with zero errors. It does not
produce a scientific verdict.

## What this does not prove

The governor is a deterministic admission layer, not a live performance result.
It does not prove the best child count for a particular Qwen quantization,
Ollama/vLLM/SGLang configuration, context length, GPU, or experiment. Before
raising the cap, run a live benchmark and record:

- accepted hypotheses per hour;
- experiment start latency;
- Qwen throughput and p95 latency;
- GPU utilization and free-VRAM reserve;
- duplicate rate and cost/tokens per promoted hypothesis;
- research tasks killed before GPU.

## Official Hermes references

- [Subagent Delegation](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- [Delegation Patterns](https://hermes-agent.nousresearch.com/docs/guides/delegation-patterns)
- [Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
- [Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
- [Cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)
