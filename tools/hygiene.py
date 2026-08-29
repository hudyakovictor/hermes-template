#!/usr/bin/env python3
"""researchagen — гигиена состояния (ночной cron).

Задачи:
  1. зависшие прогоны (running > N часов, процесса нет) → failed + гипотеза в blocked;
  2. ротация логов и metrics.jsonl больше лимита;
  3. уборка протухших stop-флагов;
  4. компактизация журнала событий (старше 90 дней — в архивный JSONL);
  5. VACUUM.

CLI: python tools/hygiene.py run [--max-run-hours 24] [--json]
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import core
import governor

MAX_LOG_MB = 20
MAX_METRICS_MB = 50
EVENT_KEEP_DAYS = 90


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            import subprocess
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                                 capture_output=True, text=True, timeout=15, check=False)
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def reap_stale_runs(conn, max_hours: float) -> list[dict]:
    rows = conn.execute("SELECT * FROM runs WHERE state='running'").fetchall()
    reaped = []
    for row in rows:
        started = core.parse_iso(row["started_at"])
        hours = 0.0 if started is None else (core.now() - started).total_seconds() / 3600
        if hours < max_hours and pid_alive(row["pid"]):
            continue
        conn.execute("UPDATE runs SET state='failed', finished_at=? WHERE run_id=?",
                     (core.iso(), row["run_id"]))
        conn.execute("UPDATE hypotheses SET status='blocked', updated_at=? WHERE id=?",
                     (core.iso(), row["hypo_id"]))
        reaped.append({"run_id": row["run_id"], "hypo_id": row["hypo_id"],
                       "hours": round(hours, 2), "pid": row["pid"]})
    conn.commit()
    if reaped:
        for item in reaped:
            governor.finish_experiment(conn, item["hypo_id"], analysis=True)
        core.log_event(conn, "hygiene.reap", None, runs=reaped)
    return reaped


def rotate(path: str, max_mb: int) -> bool:
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < max_mb * 1024 * 1024:
        return False
    backup = f"{path}.1"
    if os.path.exists(backup):
        os.remove(backup)
    shutil.move(path, backup)
    return True


def rotate_logs() -> list[str]:
    rotated = []
    if os.path.isdir(core.LOGS_DIR):
        for name in os.listdir(core.LOGS_DIR):
            path = os.path.join(core.LOGS_DIR, name)
            if os.path.isfile(path) and rotate(path, MAX_LOG_MB):
                rotated.append(os.path.relpath(path, core.ROOT))
    results = os.path.join(core.ROOT, "results")
    for dirpath, _dirs, files in os.walk(results):
        for name in files:
            if name == "metrics.jsonl" and rotate(os.path.join(dirpath, name), MAX_METRICS_MB):
                rotated.append(os.path.relpath(os.path.join(dirpath, name), core.ROOT))
    return rotated


def clear_flags(conn) -> list[str]:
    cleared = []
    if not os.path.isdir(core.STATE_DIR):
        return cleared
    active = {r["hypo_id"] for r in
              conn.execute("SELECT hypo_id FROM runs WHERE state='running'").fetchall()}
    for name in os.listdir(core.STATE_DIR):
        if not (name.startswith("stop-") and name.endswith(".flag")):
            continue
        hypo_id = name[len("stop-"):-len(".flag")]
        if hypo_id not in active:
            os.remove(os.path.join(core.STATE_DIR, name))
            cleared.append(name)
    return cleared


def archive_events(conn) -> int:
    rows = conn.execute(
        "SELECT * FROM events WHERE datetime(created_at) < datetime('now', ?)",
        (f"-{EVENT_KEEP_DAYS} day",)).fetchall()
    if not rows:
        return 0
    path = os.path.join(core.STATE_DIR, "events-archive.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    conn.execute("DELETE FROM events WHERE datetime(created_at) < datetime('now', ?)",
                 (f"-{EVENT_KEEP_DAYS} day",))
    conn.commit()
    return len(rows)


def run(max_hours: float = 24.0) -> dict:
    conn = core.db()
    reaped = reap_stale_runs(conn, max_hours)
    rotated = rotate_logs()
    flags = clear_flags(conn)
    archived = archive_events(conn)
    conn.commit()
    conn.execute("VACUUM")
    return {"reaped_runs": reaped, "rotated_files": rotated,
            "cleared_flags": flags, "archived_events": archived}


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    data = run(float(core.arg(argv, "max-run-hours", 24.0)))
    text = "\n".join([
        f"Зависшие прогоны закрыты: {len(data['reaped_runs'])}",
        f"Файлов ротировано: {len(data['rotated_files'])}",
        f"Флагов убрано: {len(data['cleared_flags'])}",
        f"Событий сдано в архив: {data['archived_events']}",
    ])
    core.emit(data, as_json, text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
