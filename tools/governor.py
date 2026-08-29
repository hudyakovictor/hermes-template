#!/usr/bin/env python3
"""researchagen — автономный governor для Hermes subagents и GPU.

Hermes ``delegate_task`` умеет ограничивать параллельность только на уровне
одного batch. Он не знает о GPU, о running-эксперименте в researchagen и о
других процессах. Этот модуль добавляет маленький stdlib-only control plane:

* SQLite lease/admission gate вокруг research workers;
* режимы discover → triage → testing → analyze;
* динамическая capacity по VRAM, util, резерву эксперимента, очереди и budget;
* pause/stop/checkpoint/resume semantics;
* валидацию структурированного отчёта без автоматического продвижения
  гипотезы или evidence.

Важно: это cooperative gate для родительского Hermes-агента. Нативный
``delegate_task`` вызывается только после ``reserve``; native Kanban workers
должны соблюдать тот же протокол в своём handoff. Нельзя обещать hard GPU
изоляцию, если обходить этот gate или если inference server запускается вне
этого профиля.

CLI:
  python tools/rg.py governor plan [--mode discover] [--tasks N] [--json]
  python tools/rg.py governor mode testing [--json]
  python tools/rg.py governor reserve --worker-id W --task-id T [--json]
  python tools/rg.py governor pause --lease LEASE
  python tools/rg.py governor checkpoint --lease LEASE --checkpoint FILE
  python tools/rg.py governor resume --lease LEASE
  python tools/rg.py governor stop --lease LEASE
  python tools/rg.py governor stop-confirm --lease LEASE
  python tools/rg.py governor release --lease LEASE
  python tools/rg.py governor report --file report.json --worker-id W
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta
from typing import Any

import core
import gpu
import hypo
import queue as q


MODES = ("discover", "triage", "testing", "analyze", "paused")
RESEARCH_LIVE_STATES = ("active", "pause_requested", "stop_requested")
# Expired means "unresolved and unsafe", not "gone".  A missed heartbeat
# must never free a GPU slot while the child could still be alive.
UNRESOLVED_RESEARCH_STATES = RESEARCH_LIVE_STATES + ("expired",)
UNRESOLVED_EXPERIMENT_STATES = ("active", "pause_requested", "stop_requested", "expired")
LEASE_STATES = UNRESOLVED_RESEARCH_STATES + ("paused", "stopped", "released")
REPORT_STATUSES = ("completed", "no_finding", "blocked", "paused", "failed")
REPORT_PRIVATE_KEYS = {
    "chain_of_thought", "chain-of-thought", "cot", "hidden_reasoning",
    "internal_reasoning", "private_reasoning", "raw_reasoning", "reasoning",
    "analysis_trace", "thoughts", "scratchpad",
}


def _private_report_paths(value: Any, path: str = "report") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            current = f"{path}.{key}"
            if key_text in REPORT_PRIVATE_KEYS:
                paths.append(current)
            else:
                paths.extend(_private_report_paths(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_private_report_paths(item, f"{path}[{index}]"))
    return paths


def _redact_private_report(value: Any):
    if isinstance(value, dict):
        return {
            str(key): _redact_private_report(item)
            for key, item in value.items()
            if str(key).lower() not in REPORT_PRIVATE_KEYS
        }
    if isinstance(value, list):
        return [_redact_private_report(item) for item in value]
    return value


# A report is deliberately stricter than a natural-language child summary.
# It is still not scientific truth: the parent must review it and explicitly
# call the existing hypo/evidence tools.
REPORT_REQUIRED = (
    "task_id", "status", "claims", "evidence_refs", "sources", "confidence",
    "duplicate_of", "recommended_next_action", "changed_files", "resource_usage",
    "failure_reason",
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "null", "none")
    return bool(value)


def enabled(config: dict | None = None) -> bool:
    return _truthy(core.cfg("researchagen.governor.enabled", True, config))


def _cfg(name: str, default: Any, config: dict | None = None) -> Any:
    return core.cfg(f"researchagen.governor.{name}", default, config)


def _int_cfg(name: str, default: int, config: dict | None = None) -> int:
    try:
        return int(_cfg(name, default, config))
    except (TypeError, ValueError):
        return default


def _float_cfg(name: str, default: float, config: dict | None = None) -> float:
    try:
        return float(_cfg(name, default, config))
    except (TypeError, ValueError):
        return default


def _mode_setting(conn, config: dict | None = None) -> str:
    value = core.setting(conn, "governor.mode", None)
    if value not in MODES:
        value = str(_cfg("default_mode", "discover", config) or "discover").lower()
    return value if value in MODES else "discover"


def _active_run_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT run_id, hypo_id, level, pid, started_at FROM runs WHERE state='running' "
        "ORDER BY started_at"
    ).fetchall()
    return [dict(row) for row in rows]


def _active_experiment_lease_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM governor_leases WHERE kind='experiment' AND state IN "
        "('active','pause_requested','stop_requested','expired') ORDER BY acquired_at"
    ).fetchall()
    return [dict(row) for row in rows]


def _active_research_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM governor_leases WHERE kind='research' AND state IN "
        "('active','pause_requested','stop_requested','expired') ORDER BY acquired_at"
    ).fetchall()
    return [dict(row) for row in rows]


def _effective_mode(conn, config: dict | None = None) -> str:
    """A running experiment always overrides a stale discover setting."""
    if _active_run_rows(conn) or _active_experiment_lease_rows(conn):
        return "testing"
    return _mode_setting(conn, config)


def _lease_ttl(config: dict | None = None) -> int:
    return max(30, _int_cfg("lease_ttl_seconds", 300, config))


def _iso_after(seconds: int) -> str:
    return core.iso(core.now() + timedelta(seconds=max(1, seconds)))


def _decode_metadata(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _cron_control_enabled(config: dict | None = None) -> bool:
    return _truthy(_cfg("hermes_cron_control", True, config))


def _cron_job_name(config: dict | None = None) -> str:
    return str(_cfg("research_cron_job", "research-loop", config) or "research-loop")


def _toggle_research_cron(paused: bool, config: dict | None = None) -> dict:
    """Pause/resume the scheduled research job when Hermes is installed.

    This is best effort only when the CLI is absent: an installation without
    Hermes has no scheduled LLM job to protect. A present-but-failing CLI is
    reported as a hard failure so the caller can fail closed.
    """
    if not _cron_control_enabled(config):
        return {"ok": True, "skipped": True, "reason": "hermes cron control disabled"}
    hermes = shutil.which("hermes")
    if not hermes:
        return {"ok": True, "skipped": True, "reason": "hermes CLI not found"}
    action = "pause" if paused else "resume"
    env = dict(os.environ)
    env.setdefault("HERMES_PROFILE", "researchagen")
    try:
        proc = subprocess.run(
            [hermes, "cron", action, _cron_job_name(config)],
            capture_output=True, text=True, timeout=30, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "action": action, "reason": str(exc)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "unknown hermes cron error").strip()
        return {"ok": False, "action": action, "reason": detail[:240]}
    return {"ok": True, "action": action, "output": (proc.stdout or "").strip()[:240]}


def _mark_research_pause_requested(conn, reason: str) -> list[str]:
    rows = _active_research_rows(conn)
    ids = [row["lease_id"] for row in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE governor_leases SET state='pause_requested', reason=?, heartbeat_at=? "
            f"WHERE lease_id IN ({marks})",
            (reason, core.iso(), *ids),
        )
        conn.commit()
    return ids


def sweep(conn, config: dict | None = None) -> list[dict]:
    """Expire abandoned research leases; never silently free a live experiment."""
    now = core.now()
    rows = conn.execute(
        "SELECT * FROM governor_leases WHERE state IN "
        "('active','pause_requested','stop_requested') AND expires_at IS NOT NULL"
    ).fetchall()
    expired = []
    for row in rows:
        expiry = core.parse_iso(row["expires_at"])
        if expiry is None or expiry > now:
            continue
        # A live experiment is also represented in runs. Keep the lease as a
        # safety record until finish/reap; otherwise a stale heartbeat could
        # make the governor launch a second experiment.
        if row["kind"] == "experiment":
            metadata = _decode_metadata(row["metadata"])
            hid = metadata.get("hypo_id")
            live = conn.execute(
                "SELECT 1 FROM runs WHERE state='running' AND hypo_id=? LIMIT 1", (hid,)
            ).fetchone() if hid else None
            if live:
                continue
        conn.execute(
            "UPDATE governor_leases SET state='expired', reason=?, heartbeat_at=? WHERE lease_id=?",
            ("lease TTL expired", core.iso(), row["lease_id"]),
        )
        expired.append({"lease_id": row["lease_id"], "kind": row["kind"],
                        "owner_id": row["owner_id"]})
    if expired:
        conn.commit()
        core.log_event(conn, "governor.lease_expired", None, leases=expired)
    return expired


def _telemetry_capacity(snap: dict, config: dict | None = None) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if not snap.get("available"):
        if snap.get("debug"):
            cap = max(0, _int_cfg("debug_research_capacity", 1, config))
            reasons.append("debug telemetry: real GPU gate is simulated")
            return cap, reasons
        reasons.append("GPU telemetry unavailable: fail-closed")
        return 0, reasons

    best = snap.get("best") or {}
    try:
        free = float(best.get("free_gb", snap.get("free_gb", 0.0)))
        util = float(best.get("util_pct", 100.0))
    except (TypeError, ValueError):
        reasons.append("malformed GPU telemetry: fail-closed")
        return 0, reasons

    critical_free = _float_cfg("critical_free_vram_gb", 2.0, config)
    low_free = _float_cfg("low_free_vram_gb", 6.0, config)
    min_free = _float_cfg("min_free_vram_gb", 4.0, config)
    high_util = _float_cfg("high_utilization_pct", 85.0, config)
    saturated_util = _float_cfg("saturated_utilization_pct", 95.0, config)
    reserve = max(0.0, _float_cfg("experiment_reserve_gb", 8.0, config))
    per_worker = max(0.25, _float_cfg("research_worker_vram_gb", 4.0, config))

    if free < critical_free or util >= saturated_util:
        reasons.append(f"GPU saturated: free={free:.1f} GB, util={util:.0f}%")
        return 0, reasons
    if free < min_free:
        reasons.append(f"free VRAM {free:.1f} GB below safety floor {min_free:.1f} GB")
        return 0, reasons

    headroom = max(0.0, free - reserve)
    by_vram = int(math.floor(headroom / per_worker))
    if by_vram <= 0:
        reasons.append(
            f"experiment reserve {reserve:.1f} GB leaves no {per_worker:.1f} GB worker slot"
        )
        return 0, reasons

    if free < low_free or util >= high_util:
        reasons.append(f"degraded GPU: free={free:.1f} GB, util={util:.0f}% → at most one worker")
        return 1, reasons
    reasons.append(
        f"healthy GPU: free={free:.1f} GB, util={util:.0f}%, "
        f"headroom={headroom:.1f} GB"
    )
    return by_vram, reasons


def _daily_research_budget(conn, config: dict | None = None) -> tuple[int | None, int | None]:
    """Return (limit, remaining) for admission units acquired today.

    Zero means unlimited. This is intentionally a task-admission budget, not a
    fake token counter; actual token/resource usage is accepted in reports.
    """
    limit = _int_cfg("daily_research_task_budget", 12, config)
    if limit <= 0:
        return None, None
    used = conn.execute(
        "SELECT COUNT(*) FROM governor_leases WHERE kind='research' "
        "AND date(acquired_at)=date('now')"
    ).fetchone()[0]
    return limit, max(0, limit - int(used))


def _ready_experiment_candidate(conn, config: dict | None = None) -> dict | None:
    """Return the queue's next launchable hypothesis, if any.

    This is the priority bridge: once a falsifiable candidate is ready, new
    exploratory Qwen work must not occupy the GPU ahead of the sequential
    experiment lane. ``hypo.check`` is deterministic and only refreshes the
    cached kill-check count; it never promotes the candidate.
    """
    candidate = q.pick_next(conn, config)
    if not candidate:
        return None
    gate = hypo.check(candidate["id"], conn)
    return candidate if gate.get("ok") else None


def plan(conn, config: dict | None = None, requested_mode: str | None = None,
         task_count: int | None = None) -> dict:
    """Calculate a deterministic admission plan; never spawns a worker."""
    config = config if config is not None else core.load_config()
    sweep(conn, config)
    if not enabled(config):
        return {
            "enabled": False, "mode": _effective_mode(conn, config),
            "capacity": 0, "available_slots": 0, "active_research": len(_active_research_rows(conn)),
            "can_spawn": False, "reasons": ["governor disabled: fail-closed"],
        }

    requested = (requested_mode or "").lower()
    if requested and requested != "auto" and requested not in MODES:
        return {"enabled": True, "mode": requested, "capacity": 0,
                "available_slots": 0, "active_research": 0, "can_spawn": False,
                "reasons": [f"unknown mode: {requested}"]}
    mode = _effective_mode(conn, config) if requested in ("", "auto") else requested
    # A live experiment is a hard override even if a caller supplied discover.
    if _active_run_rows(conn) or _active_experiment_lease_rows(conn):
        mode = "testing"

    ready_candidate = None
    if mode in ("discover", "triage") and _truthy(
        _cfg("prioritize_ready_experiments", True, config)
    ):
        ready_candidate = _ready_experiment_candidate(conn, config)

    configured_raw = max(0, _int_cfg("max_research_children", 2, config))
    measured_value = core.setting(conn, "governor.measured_max_concurrency", None)
    measured_cap = None
    try:
        if measured_value is not None:
            measured_cap = max(0, int(measured_value))
    except (TypeError, ValueError):
        measured_cap = None
    configured = configured_raw if measured_cap is None else min(configured_raw, measured_cap)
    triage_cap = max(0, _int_cfg("max_triage_children", 1, config))
    mode_cap = {
        "discover": configured,
        "triage": min(configured, triage_cap),
        "testing": 0,
        "analyze": 0,
        "paused": 0,
    }.get(mode, 0)
    reasons: list[str] = []
    if measured_cap is not None:
        reasons.append(f"live benchmark cap: {measured_cap} children")
    if ready_candidate is not None:
        mode_cap = 0
        reasons.append(
            f"testing priority: {ready_candidate['id']} is launchable; research fan-out closed"
        )
    if mode in ("testing", "analyze", "paused"):
        reasons.append(f"mode={mode}: research Qwen admission is closed")
        telemetry_cap = 0
    else:
        snap = gpu.snapshot(config)
        telemetry_cap, telemetry_reasons = _telemetry_capacity(snap, config)
        reasons.extend(telemetry_reasons)

    budget_limit, budget_remaining = _daily_research_budget(conn, config)
    budget_cap = configured if budget_remaining is None else budget_remaining
    if budget_remaining is not None:
        reasons.append(f"research admission budget: {budget_remaining}/{budget_limit} units remaining")

    total_capacity = min(mode_cap, telemetry_cap, budget_cap)
    if task_count is not None:
        total_capacity = min(total_capacity, max(0, int(task_count)))
    active = len(_active_research_rows(conn))
    available = max(0, total_capacity - active)
    if active:
        reasons.append(f"active research leases: {active}")
    if total_capacity == 0 and mode not in ("testing", "analyze", "paused"):
        reasons.append("capacity=0: no new research worker may start")
    return {
        "enabled": True,
        "mode": mode,
        "capacity": total_capacity,
        "available_slots": available,
        "active_research": active,
        "can_spawn": available > 0,
        "configured_cap": configured,
        "configured_cap_raw": configured_raw,
        "measured_cap": measured_cap,
        "mode_cap": mode_cap,
        "telemetry_cap": telemetry_cap,
        "budget_limit": budget_limit,
        "budget_remaining": budget_remaining,
        "task_count": task_count,
        "testing_candidate": None if ready_candidate is None else {
            "id": ready_candidate["id"], "title": ready_candidate["title"],
            "ppi": ready_candidate["ppi"], "bin": ready_candidate["bin"],
        },
        "experiment_active": bool(_active_run_rows(conn) or _active_experiment_lease_rows(conn)),
        "research_leases": [
            {"lease_id": r["lease_id"], "owner_id": r["owner_id"], "task_id": r["task_id"],
             "state": r["state"], "checkpoint": r["checkpoint"]}
            for r in _active_research_rows(conn)
        ],
        "reasons": reasons,
    }


def set_mode(conn, new_mode: str, config: dict | None = None) -> dict:
    """Set a phase and request native pause/resume at the transition boundary."""
    config = config if config is not None else core.load_config()
    new_mode = str(new_mode or "").lower()
    if new_mode not in MODES:
        return {"ok": False, "reason": f"mode должен быть: {', '.join(MODES)}"}
    if not enabled(config):
        return {"ok": False, "reason": "governor disabled: mode transition refused"}
    if new_mode in ("discover", "triage") and (_active_run_rows(conn) or _active_experiment_lease_rows(conn)):
        return {"ok": False, "reason": "нельзя возобновить research: GPU experiment ещё активен"}

    conn.execute(
        "INSERT INTO settings (key,value,updated_at) VALUES ('governor.mode',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (json.dumps(new_mode), core.iso()),
    )
    pause = new_mode in ("testing", "analyze", "paused")
    requested = _mark_research_pause_requested(
        conn, f"phase switched to {new_mode}"
    ) if pause else []
    conn.commit()

    cron = _toggle_research_cron(pause, config)
    if not cron.get("ok"):
        # Fail closed: the DB mode still prevents cooperative workers from
        # reserving, and the caller must not launch testing until the scheduler
        # control path is healthy.
        core.set_setting(conn, "governor.cron_control_blocked", cron.get("reason", "unknown"))
    else:
        core.set_setting(conn, "governor.cron_control_blocked", False)
    core.log_event(conn, "governor.mode", None, mode=new_mode,
                   pause_requested=requested, cron=cron)
    active = _active_research_rows(conn)
    return {
        "ok": bool(cron.get("ok")),
        "mode": new_mode,
        "pause_requested": requested,
        "active_research": [r["lease_id"] for r in active],
        "ready_for_testing": not active if new_mode == "testing" else None,
        "cron": cron,
        "reason": None if cron.get("ok") else "cron control failed: fail-closed",
    }


def _find_lease(conn, identifier: str):
    return conn.execute(
        "SELECT * FROM governor_leases WHERE lease_id=? OR owner_id=? "
        "ORDER BY acquired_at DESC LIMIT 1", (identifier, identifier)
    ).fetchone()


def acquire_research(conn, worker_id: str, task_id: str | None = None,
                     requested_vram_gb: float | None = None,
                     metadata: dict | None = None, config: dict | None = None) -> dict:
    """Atomically reserve one research slot for one native Hermes child."""
    config = config if config is not None else core.load_config()
    worker_id = str(worker_id or "").strip()
    task_id = str(task_id or worker_id).strip()
    if not worker_id:
        return {"ok": False, "reason": "worker_id is required"}
    if not enabled(config):
        return {"ok": False, "reason": "governor disabled: research admission refused"}
    sweep(conn, config)
    existing = conn.execute(
        "SELECT * FROM governor_leases WHERE kind='research' AND owner_id=? "
        "AND state IN ('active','pause_requested','stop_requested','paused') "
        "ORDER BY acquired_at DESC LIMIT 1", (worker_id,)
    ).fetchone()
    if existing is not None:
        return {"ok": True, "idempotent": True, "lease_id": existing["lease_id"],
                "state": existing["state"], "plan": plan(conn, config)}

    # A single reservation consumes one slot, but must not cap the total plan
    # to one; otherwise the second child could never be admitted in a healthy
    # two-slot discover batch.
    admission = plan(conn, config)
    if not admission["can_spawn"]:
        return {"ok": False, "reason": "research admission denied", "plan": admission}

    try:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck the count after taking the SQLite writer lock. Telemetry can
        # change, but two parents cannot consume the same DB slot.
        active = conn.execute(
            "SELECT COUNT(*) FROM governor_leases WHERE kind='research' AND state IN "
            "('active','pause_requested','stop_requested','expired')"
        ).fetchone()[0]
        if active >= admission["capacity"]:
            conn.rollback()
            admission["available_slots"] = 0
            admission["can_spawn"] = False
            return {"ok": False, "reason": "research slot taken by another parent", "plan": admission}
        lease_id = "r-" + uuid.uuid4().hex[:12]
        now = core.iso()
        payload = dict(metadata or {})
        payload.setdefault("worker_id", worker_id)
        row = (
            lease_id, worker_id, "research", "active", admission["mode"], task_id,
            None if requested_vram_gb is None else float(requested_vram_gb),
            now, now, _iso_after(_lease_ttl(config)), None, None,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        conn.execute(
            "INSERT INTO governor_leases "
            "(lease_id,owner_id,kind,state,mode,task_id,requested_vram_gb,"
            " acquired_at,heartbeat_at,expires_at,checkpoint,reason,metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", row,
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    core.log_event(conn, "governor.research_admit", None,
                   lease_id=lease_id, worker_id=worker_id, task_id=task_id,
                   mode=admission["mode"])
    return {"ok": True, "lease_id": lease_id, "worker_id": worker_id,
            "task_id": task_id, "state": "active", "plan": admission}


def acquire_experiment(conn, hypo_id: str, level: str,
                       config: dict | None = None) -> dict:
    """Acquire the exclusive experiment lease; never bypass research pause."""
    config = config if config is not None else core.load_config()
    if not enabled(config):
        return {"ok": False, "reason": "governor disabled: experiment lease refused"}
    current = _effective_mode(conn, config)
    if current == "paused":
        return {"ok": False, "reason": "governor paused"}
    if current == "analyze":
        return {"ok": False, "reason": "analyze phase: verdict required before next experiment"}

    transition = set_mode(conn, "testing", config)
    if not transition.get("ok"):
        return {"ok": False, "reason": transition.get("reason"), "transition": transition}
    if transition.get("active_research"):
        return {
            "ok": False,
            "reason": "research workers still active; stop/steer them at checkpoint first",
            "active_research": transition["active_research"],
            "transition": transition,
        }
    if not transition.get("ready_for_testing", True):
        return {"ok": False, "reason": "research pause not acknowledged", "transition": transition}

    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM runs WHERE state='running' LIMIT 1").fetchone():
            conn.rollback()
            return {"ok": False, "reason": "GPU experiment already running"}
        if conn.execute(
            "SELECT 1 FROM governor_leases WHERE kind='experiment' AND state IN "
            "('active','pause_requested','stop_requested','expired') LIMIT 1"
        ).fetchone():
            conn.rollback()
            return {"ok": False, "reason": "experiment lease already held"}
        if conn.execute(
            "SELECT 1 FROM governor_leases WHERE kind='research' AND state IN "
            "('active','pause_requested','stop_requested','expired') LIMIT 1"
        ).fetchone():
            conn.rollback()
            return {"ok": False, "reason": "research lease still active"}
        lease_id = "e-" + uuid.uuid4().hex[:12]
        now = core.iso()
        metadata = json.dumps({"hypo_id": hypo_id, "level": level}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO governor_leases "
            "(lease_id,owner_id,kind,state,mode,task_id,requested_vram_gb,"
            " acquired_at,heartbeat_at,expires_at,checkpoint,reason,metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease_id, f"experiment:{hypo_id}:{level}", "experiment", "active", "testing",
             hypo_id, None, now, now, _iso_after(_lease_ttl(config)), None, None, metadata),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    core.log_event(conn, "governor.experiment_admit", hypo_id,
                   lease_id=lease_id, level=level)
    return {"ok": True, "lease_id": lease_id, "hypo_id": hypo_id,
            "level": level, "mode": "testing"}


def has_experiment_lease(conn, hypo_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM governor_leases WHERE kind='experiment' AND state NOT IN "
        "('released','stopped') AND task_id=? LIMIT 1", (hypo_id,)
    ).fetchone()
    return row is not None


def finish_experiment(conn, hypo_id: str, config: dict | None = None,
                      analysis: bool = True) -> dict:
    """Release exclusive lease and keep research paused until verdict analysis."""
    config = config if config is not None else core.load_config()
    rows = conn.execute(
        "SELECT lease_id FROM governor_leases WHERE kind='experiment' AND task_id=? "
        "AND state NOT IN ('released','stopped')", (hypo_id,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE governor_leases SET state='released', heartbeat_at=?, reason=? WHERE lease_id=?",
            (core.iso(), "experiment finished; awaiting verdict", row["lease_id"]),
        )
    conn.commit()
    if analysis and (rows or _mode_setting(conn, config) == "testing"):
        # Even if an operator released a lease manually, finishing a run in
        # testing must create the analyze barrier before the next experiment.
        result = set_mode(conn, "analyze", config)
    else:
        result = {"ok": True, "mode": _effective_mode(conn, config), "skipped": True}
    if rows:
        core.log_event(conn, "governor.experiment_release", hypo_id,
                       leases=[r["lease_id"] for r in rows], analysis=analysis)
    return {"ok": bool(result.get("ok", True)), "released": [r["lease_id"] for r in rows],
            "transition": result}


def complete_analysis(conn, config: dict | None = None) -> dict:
    """Return to discovery only after the parent has recorded a verdict."""
    config = config if config is not None else core.load_config()
    if _active_run_rows(conn) or _active_experiment_lease_rows(conn):
        return {"ok": False, "reason": "GPU experiment still active"}
    if _mode_setting(conn, config) not in ("analyze", "testing"):
        return {"ok": True, "mode": _effective_mode(conn, config), "skipped": True}
    return set_mode(conn, "discover", config)


def heartbeat(conn, identifier: str, config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    row = _find_lease(conn, identifier)
    if row is None:
        return {"ok": False, "reason": "lease not found"}
    allowed = RESEARCH_LIVE_STATES if row["kind"] == "research" else ("active",)
    if row["state"] not in allowed:
        return {"ok": False, "reason": f"lease state is {row['state']}"}
    expires = _iso_after(_lease_ttl(config))
    conn.execute(
        "UPDATE governor_leases SET heartbeat_at=?, expires_at=? WHERE lease_id=?",
        (core.iso(), expires, row["lease_id"]),
    )
    conn.commit()
    return {"ok": True, "lease_id": row["lease_id"], "expires_at": expires}


def request_pause(conn, identifier: str | None = None,
                  reason: str = "testing phase requested pause") -> dict:
    if identifier:
        row = _find_lease(conn, identifier)
        rows = [] if row is None or row["kind"] != "research" else [row]
    else:
        rows = _active_research_rows(conn)
    ids = []
    for row in rows:
        if row["state"] in RESEARCH_LIVE_STATES:
            conn.execute(
                "UPDATE governor_leases SET state='pause_requested', reason=?, heartbeat_at=? "
                "WHERE lease_id=?", (reason, core.iso(), row["lease_id"]),
            )
            ids.append(row["lease_id"])
    conn.commit()
    for lease_id in ids:
        core.log_event(conn, "governor.research_pause_requested", None,
                       lease_id=lease_id, reason=reason)
    return {"ok": bool(ids) or identifier is None, "pause_requested": ids,
            "reason": None if ids or identifier is None else "research lease not found"}


def checkpoint(conn, identifier: str, checkpoint_value: str,
               reason: str | None = None) -> dict:
    row = _find_lease(conn, identifier)
    if row is None or row["kind"] != "research":
        return {"ok": False, "reason": "research lease not found"}
    if row["state"] not in ("active", "pause_requested", "stop_requested", "paused"):
        return {"ok": False, "reason": f"cannot checkpoint state {row['state']}"}
    conn.execute(
        "UPDATE governor_leases SET state='paused', checkpoint=?, reason=?, heartbeat_at=? "
        "WHERE lease_id=?",
        (str(checkpoint_value or ""), reason or "paused at checkpoint", core.iso(), row["lease_id"]),
    )
    conn.commit()
    core.log_event(conn, "governor.research_checkpoint", None,
                   lease_id=row["lease_id"], checkpoint=checkpoint_value)
    return {"ok": True, "lease_id": row["lease_id"], "state": "paused",
            "checkpoint": checkpoint_value}


def resume(conn, identifier: str, config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    row = _find_lease(conn, identifier)
    if row is None or row["kind"] != "research":
        return {"ok": False, "reason": "research lease not found"}
    if _effective_mode(conn, config) not in ("discover", "triage"):
        return {"ok": False, "reason": "resume denied outside discover/triage"}
    if row["state"] not in ("paused",):
        return {"ok": row["state"] == "active", "reason": f"lease state is {row['state']}"}
    admission = plan(conn, config)
    if not admission["can_spawn"]:
        return {"ok": False, "reason": "resume denied by current capacity", "plan": admission}
    conn.execute(
        "UPDATE governor_leases SET state='active', mode=?, reason=?, heartbeat_at=?, expires_at=? "
        "WHERE lease_id=?",
        (admission["mode"], "resumed from checkpoint", core.iso(),
         _iso_after(_lease_ttl(config)), row["lease_id"]),
    )
    conn.commit()
    core.log_event(conn, "governor.research_resume", None, lease_id=row["lease_id"])
    return {"ok": True, "lease_id": row["lease_id"], "state": "active",
            "checkpoint": row["checkpoint"], "plan": admission}


def request_stop(conn, identifier: str, reason: str = "stop requested") -> dict:
    row = _find_lease(conn, identifier)
    if row is None:
        return {"ok": False, "reason": "lease not found"}
    if row["kind"] != "research":
        return {"ok": False, "reason": "experiment stop is controlled by dispatch/preempt, not worker stop"}
    if row["state"] not in UNRESOLVED_RESEARCH_STATES:
        return {"ok": False, "reason": f"lease state is {row['state']}"}
    conn.execute(
        "UPDATE governor_leases SET state='stop_requested', reason=?, heartbeat_at=? WHERE lease_id=?",
        (reason, core.iso(), row["lease_id"]),
    )
    conn.commit()
    core.log_event(conn, "governor.stop_requested", None,
                   lease_id=row["lease_id"], lease_kind=row["kind"])
    return {"ok": True, "lease_id": row["lease_id"], "state": "stop_requested"}


def confirm_stop(conn, identifier: str, reason: str = "native worker stopped") -> dict:
    row = _find_lease(conn, identifier)
    if row is None:
        return {"ok": False, "reason": "lease not found"}
    if row["kind"] != "research":
        return {"ok": False, "reason": "experiment stop is controlled by dispatch/preempt, not worker stop"}
    if row["state"] not in ("active", "pause_requested", "stop_requested", "paused", "expired"):
        return {"ok": False, "reason": f"cannot stop state {row['state']}"}
    conn.execute(
        "UPDATE governor_leases SET state='stopped', reason=?, heartbeat_at=? WHERE lease_id=?",
        (reason, core.iso(), row["lease_id"]),
    )
    conn.commit()
    core.log_event(conn, "governor.worker_stopped", None,
                   lease_id=row["lease_id"], lease_kind=row["kind"])
    return {"ok": True, "lease_id": row["lease_id"], "state": "stopped"}


def release(conn, identifier: str, reason: str = "worker completed") -> dict:
    row = _find_lease(conn, identifier)
    if row is None:
        return {"ok": False, "reason": "lease not found"}
    if row["state"] in ("released", "stopped"):
        return {"ok": True, "idempotent": True, "lease_id": row["lease_id"],
                "state": row["state"]}
    conn.execute(
        "UPDATE governor_leases SET state='released', reason=?, heartbeat_at=? WHERE lease_id=?",
        (reason, core.iso(), row["lease_id"]),
    )
    conn.commit()
    core.log_event(conn, "governor.lease_release", None,
                   lease_id=row["lease_id"], lease_kind=row["kind"], reason=reason)
    return {"ok": True, "lease_id": row["lease_id"], "state": "released"}


def leases(conn, include_closed: bool = False) -> list[dict]:
    if include_closed:
        rows = conn.execute("SELECT * FROM governor_leases ORDER BY acquired_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM governor_leases WHERE state NOT IN ('released','stopped') "
            "ORDER BY acquired_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["metadata"] = _decode_metadata(item.get("metadata"))
        result.append(item)
    return result


def validate_report(report: Any) -> dict:
    """Validate shape only; a valid report is still ``review_pending``."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return {"valid": False, "errors": ["report must be a JSON object"],
                "review_pending": True, "scientific_state_changed": False}
    private_paths = _private_report_paths(report)
    if private_paths:
        errors.append(
            "hidden reasoning fields are not accepted or stored: " + ", ".join(private_paths[:8])
        )
    for key in REPORT_REQUIRED:
        if key not in report:
            errors.append(f"missing field: {key}")
    status = report.get("status")
    if status not in REPORT_STATUSES:
        errors.append(f"status must be one of: {', '.join(REPORT_STATUSES)}")
    if not isinstance(report.get("task_id"), str) or not report.get("task_id", "").strip():
        errors.append("task_id must be a non-empty string")
    for key in ("claims", "evidence_refs", "sources", "changed_files"):
        if key in report and not isinstance(report[key], list):
            errors.append(f"{key} must be a list")
    confidence = report.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        errors.append("confidence must be a number in [0,1]")
    elif not 0.0 <= float(confidence) <= 1.0:
        errors.append("confidence must be a number in [0,1]")
    if report.get("duplicate_of") is not None and not isinstance(report.get("duplicate_of"), str):
        errors.append("duplicate_of must be null or a string")
    if report.get("failure_reason") is not None and not isinstance(report.get("failure_reason"), str):
        errors.append("failure_reason must be null or a string")
    if not isinstance(report.get("recommended_next_action"), str):
        errors.append("recommended_next_action must be a string")
    if not isinstance(report.get("resource_usage"), dict):
        errors.append("resource_usage must be an object")
    if isinstance(report.get("changed_files"), list) and any(
        not isinstance(item, str) for item in report["changed_files"]
    ):
        errors.append("changed_files entries must be strings")
    if isinstance(report.get("evidence_refs"), list) and any(
        not isinstance(item, str) or not item.strip() for item in report["evidence_refs"]
    ):
        errors.append("evidence_refs entries must be non-empty strings")
    if status == "failed" and not isinstance(report.get("failure_reason"), str):
        errors.append("failed report requires failure_reason")
    if status == "completed":
        if not report.get("claims"):
            errors.append("completed report requires at least one claim")
        if not report.get("evidence_refs") or not report.get("sources"):
            errors.append("completed report requires evidence_refs and sources")
    # A summary can never become a hypothesis/evidence through this function.
    return {
        "valid": not errors,
        "errors": errors,
        "review_pending": True,
        "scientific_state_changed": False,
        "promotion": "manual_parent_review_only",
    }


def record_report(conn, path: str, worker_id: str | None = None) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "valid": False, "errors": [str(exc)],
                "review_pending": True, "scientific_state_changed": False}
    validation = validate_report(report)
    task_id = report.get("task_id") if isinstance(report, dict) else None
    status = report.get("status") if isinstance(report, dict) else "invalid"
    stored_report = _redact_private_report(report)
    conn.execute(
        "INSERT INTO governor_reports (worker_id,task_id,status,accepted,payload,errors,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (worker_id, task_id, status, 1 if validation["valid"] else 0,
         json.dumps(stored_report, ensure_ascii=False, sort_keys=True),
         json.dumps(validation["errors"], ensure_ascii=False), core.iso()),
    )
    report_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    core.log_event(conn, "governor.report", None, report_id=report_id,
                   task_id=task_id, accepted=validation["valid"])
    return {"ok": True, "report_id": report_id, "worker_id": worker_id,
            "task_id": task_id, **validation}


def status(conn, config: dict | None = None) -> dict:
    return {"plan": plan(conn, config), "mode_setting": _mode_setting(conn, config),
            "leases": leases(conn), "recent_reports": [
                dict(row) for row in conn.execute(
                    "SELECT report_id,worker_id,task_id,status,accepted,created_at "
                    "FROM governor_reports ORDER BY report_id DESC LIMIT 20"
                ).fetchall()
            ]}


def _text_result(data: dict) -> str:
    if "plan" in data and isinstance(data["plan"], dict):
        p = data["plan"]
        return (f"mode={p.get('mode')} capacity={p.get('capacity', 0)} "
                f"available={p.get('available_slots', 0)} "
                f"active_research={p.get('active_research', 0)}\n"
                + "\n".join(f"• {r}" for r in p.get("reasons", [])))
    if "errors" in data and data.get("valid") is False:
        return "Отчёт отклонён схемой:\n" + "\n".join(f"• {e}" for e in data["errors"])
    if data.get("reason"):
        return str(data["reason"])
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def main(argv: list[str]) -> int:
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "status"
    conn = core.db()

    if cmd in ("status", "plan"):
        tasks = core.arg(argv, "tasks")
        task_count = int(tasks) if tasks is not None else None
        data = status(conn, config) if cmd == "status" else {
            "plan": plan(conn, config, core.arg(argv, "mode"), task_count)
        }
        core.emit(data, as_json, _text_result(data))
        return 0
    if cmd == "mode":
        new_mode = argv[2] if len(argv) > 2 else ""
        data = set_mode(conn, new_mode, config)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd in ("reserve", "acquire"):
        worker = core.arg(argv, "worker-id") or core.arg(argv, "owner")
        task = core.arg(argv, "task-id")
        requested = core.arg(argv, "requested-vram-gb")
        data = acquire_research(
            conn, worker or "", task_id=task,
            requested_vram_gb=None if requested is None else float(requested),
            config=config,
        )
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "heartbeat":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        data = heartbeat(conn, identifier, config)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "pause":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else None)
        data = request_pause(conn, identifier)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "checkpoint":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        value = core.arg(argv, "checkpoint") or core.arg(argv, "file") or "checkpoint"
        data = checkpoint(conn, identifier, value)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "resume":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        data = resume(conn, identifier, config)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "stop":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        data = request_stop(conn, identifier)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd in ("stop-confirm", "confirm-stop"):
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        data = confirm_stop(conn, identifier)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd == "release":
        identifier = core.arg(argv, "lease") or (argv[2] if len(argv) > 2 else "")
        data = release(conn, identifier)
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("ok") else 1
    if cmd in ("leases", "workers"):
        data = {"leases": leases(conn, include_closed=core.flag(argv, "all"))}
        core.emit(data, as_json, _text_result(data))
        return 0
    if cmd in ("report", "validate-report"):
        path = core.arg(argv, "file") or (argv[2] if len(argv) > 2 else "")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            data = {"ok": False, "valid": False, "errors": [str(exc)],
                    "review_pending": True, "scientific_state_changed": False}
            core.emit(data, as_json, _text_result(data))
            return 1
        if cmd == "validate-report":
            data = validate_report(report)
        else:
            data = record_report(conn, path, core.arg(argv, "worker-id"))
        core.emit(data, as_json, _text_result(data))
        return 0 if data.get("valid", data.get("ok", False)) else 1

    core.fail(f"неизвестная команда governor: {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
