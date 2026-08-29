#!/usr/bin/env python3
"""researchagen — зеркало очереди в Hermes Kanban.

Зачем два списка:
  * SQLite-очередь — источник правды для решений (PI/PPI, гейты, GPU);
  * Hermes Kanban — человекочитаемое представление в терминале и боте.
Синхрон односторонний (база → канбан), чтобы не было двух конфликтующих истин.
Если CLI hermes недоступен — команда молча деградирует (контур от неё не зависит).

Соответствие статусов:
  queued            → ready
  running           → running
  paused_checkpoint → review
  blocked           → blocked
  confirmed/partial → done
  rejected/killed   → archived

CLI:
  python tools/board.py sync [--json]
  python tools/board.py show [--json]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import core

STATUS_MAP = {
    "queued": "ready",
    "running": "running",
    "paused_checkpoint": "review",
    "blocked": "blocked",
    "confirmed": "done",
    "partial": "done",
    "rejected": "archived",
    "killed": "archived",
    "archived": "archived",
}


def hermes_bin() -> str | None:
    return shutil.which("hermes")


def run_hermes(args: list[str]) -> tuple[bool, str]:
    binary = hermes_bin()
    if not binary:
        return False, "CLI hermes не найден в PATH"
    try:
        proc = subprocess.run([binary] + args, capture_output=True, text=True,
                              timeout=60, check=False, cwd=core.ROOT)
        return proc.returncode == 0, (proc.stdout or proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def sync(conn) -> dict:
    rows = conn.execute("SELECT * FROM hypotheses").fetchall()
    mirror = core.setting(conn, "kanban.mirror", {}) or {}
    created, moved, skipped = [], [], []
    if not hermes_bin():
        return {"available": False,
                "reason": "CLI hermes не найден — канбан пропущен, очередь работает",
                "items": len(rows)}
    for row in rows:
        target = STATUS_MAP.get(row["status"], "triage")
        title = f"[{row['id']}] {row['title']}"
        if row["id"] not in mirror:
            ok, out = run_hermes(["kanban", "create", title,
                                  "--description",
                                  f"researchagen {row['id']}, уровень {row['level']}, "
                                  f"прогноз {row['forecast']}%, оценка {row['est_hours']} GPU-ч"])
            if ok:
                mirror[row["id"]] = {"title": title, "status": target}
                created.append(row["id"])
            else:
                skipped.append({"id": row["id"], "error": out[:180]})
                continue
        if mirror.get(row["id"], {}).get("status") != target:
            ok, out = run_hermes(["kanban", "promote", title, "--status", target])
            if ok:
                mirror[row["id"]]["status"] = target
                moved.append({"id": row["id"], "to": target})
            else:
                skipped.append({"id": row["id"], "error": out[:180]})
    core.set_setting(conn, "kanban.mirror", mirror)
    core.log_event(conn, "kanban.sync", None, created=len(created), moved=len(moved))
    return {"available": True, "created": created, "moved": moved, "problems": skipped}


def show(conn) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM hypotheses GROUP BY status").fetchall()
    columns = {}
    for row in rows:
        columns.setdefault(STATUS_MAP.get(row["status"], "triage"), 0)
        columns[STATUS_MAP.get(row["status"], "triage")] += row["n"]
    return {"columns": columns}


def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "show"
    conn = core.db()

    if cmd == "sync":
        data = sync(conn)
        text = (data.get("reason") or
                f"Создано {len(data.get('created', []))}, перемещено "
                f"{len(data.get('moved', []))}, проблем {len(data.get('problems', []))}")
        core.emit(data, as_json, text)
        return 0

    if cmd == "show":
        data = show(conn)
        core.emit(data, as_json,
                  core.table([[k, v] for k, v in sorted(data["columns"].items())],
                             ["колонка", "задач"]) or "Пусто")
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
