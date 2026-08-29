#!/usr/bin/env python3
"""researchagen — диспетчер ресурса GPU (ядро автономии).

Смысл: агент не спрашивает человека «что запустить». Решает очередь (PPI) +
гейты: kill-stage пройден, прогноз зафиксирован, VRAM свободна, суточный бюджет
не исчерпан, цена в лимите без подтверждения. Governor дополнительно удерживает
exclusive experiment lease и не запускает его, пока research/Qwen workers не
остановлены на checkpoint. Один GPU = один прогон.

CLI:
  python tools/dispatch.py tick [--json]        # главный вход для cron
  python tools/dispatch.py launch H-003 --level L1 [--force]
  python tools/dispatch.py finish H-003 --gpu-hours 0.8 [--state done]
  python tools/dispatch.py preempt [--json]
  python tools/dispatch.py running [--json]
  python tools/dispatch.py approve H-007        # разрешить дорогой прогон (>лимита часов)
  python tools/dispatch.py pause / resume        # рубильник из Telegram
"""

from __future__ import annotations

import os
import subprocess
import sys

import core
import crew
import gpu
import governor
import hypo
import queue as q
import tg


def running_runs(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, h.title, h.forecast FROM runs r JOIN hypotheses h ON h.id=r.hypo_id "
        "WHERE r.state='running' ORDER BY r.started_at"
    ).fetchall()
    return [dict(r) for r in rows]


def gpu_hours_today(conn) -> float:
    return float(conn.execute(
        "SELECT COALESCE(SUM(gpu_hours),0) FROM runs WHERE date(started_at)=date('now')"
    ).fetchone()[0])


def is_paused(conn) -> bool:
    return bool(core.setting(conn, "dispatch.paused", False))


def approved(conn, hid: str) -> bool:
    return hid in (core.setting(conn, "dispatch.approved", []) or [])


def approve(conn, hid: str) -> None:
    current = core.setting(conn, "dispatch.approved", []) or []
    if hid not in current:
        current.append(hid)
    core.set_setting(conn, "dispatch.approved", current)
    core.log_event(conn, "dispatch.approved", hid)


def runner_command(hid: str, level: str, dry_run: bool) -> list[str]:
    script = os.path.join(core.EXP_DIR, f"{hid}.py")
    entry = script if os.path.exists(script) else os.path.join(core.TOOLS_DIR, "exp_runner.py")
    cmd = [sys.executable, entry, "--hypo", hid, "--level", level]
    if entry.endswith("exp_runner.py") and not os.path.exists(script):
        cmd += ["--smoke"]      # нет своего скрипта — идёт встроенный короткий прогон
    if dry_run:
        cmd += ["--dry-run"]
    return cmd


def launch(conn, hid: str, level: str = "L0", force: bool = False,
           config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"{hid} не найдена"}

    if is_paused(conn) and not force:
        return {"ok": False, "reason": "диспетчер на паузе (/resume снимает)"}

    if running_runs(conn):
        limit = int(core.cfg("researchagen.limits.max_parallel_experiments", 1, config))
        if len(running_runs(conn)) >= limit:
            return {"ok": False, "reason": "GPU занят другим прогоном"}

    gate = hypo.check(hid, conn)
    if not gate["ok"] and not force:
        return {"ok": False, "reason": "гейт не пройден", "problems": gate["problems"]}

    est = float(row["est_hours"] or 0)
    limit_hours = float(core.cfg("researchagen.limits.approval_gpu_hours", 12, config))
    if est > limit_hours and not approved(conn, hid) and not force:
        text = (f"*⚠️ Нужно подтверждение*\n{hid} «{row['title']}»\n"
                f"Оценка {est:.1f} GPU-ч > лимита {limit_hours:.0f} ч.\n"
                f"Разрешить: /approve {hid}")
        tg.send(text)
        core.log_event(conn, "dispatch.approval_requested", hid, est_hours=est)
        return {"ok": False, "reason": f"требуется /approve {hid} ({est:.1f} ч > {limit_hours:.0f} ч)"}

    budget = float(core.cfg("researchagen.limits.daily_gpu_hours_budget", 20, config))
    spent = gpu_hours_today(conn)
    if spent >= budget and not force:
        return {"ok": False,
                "reason": f"суточный бюджет GPU исчерпан: {spent:.1f}/{budget:.0f} ч"}

    # Acquire the phase transition before sampling the final VRAM gate.  This
    # closes the research cron and requests worker checkpoints early enough to
    # free their memory for the experiment.  A failed VRAM check leaves the
    # system in testing (research remains paused) and the next dispatcher tick
    # retries; it never silently starts new Qwen work in the meantime.
    # ``--force`` may bypass a scientific checklist, never this resource lock.
    experiment_lease = governor.acquire_experiment(conn, hid, level, config=config)
    if not experiment_lease.get("ok"):
        return {
            "ok": False,
            "reason": f"governor: {experiment_lease.get('reason')}",
            "problems": experiment_lease.get("active_research", []),
        }

    ok_gpu, why, snap = gpu.can_launch(config=config)
    if not ok_gpu and not force:
        governor.release(conn, experiment_lease["lease_id"], "GPU gate not ready; retry in testing")
        return {"ok": False, "reason": f"GPU-гейт: {why}",
                "governor": {"mode": "testing", "research_paused": True}}

    dry_run = bool(snap["debug"] and not snap["available"])
    log_path = os.path.join(core.LOGS_DIR, f"{hid}-{level}.log")
    core.ensure_dirs()
    cmd = runner_command(hid, level, dry_run)
    try:
        with open(log_path, "a", encoding="utf-8") as logfh:
            logfh.write(f"\n===== {core.iso()} launch {hid} {level} dry_run={dry_run} =====\n")
            proc = subprocess.Popen(cmd, cwd=core.ROOT, stdout=logfh,
                                    stderr=subprocess.STDOUT)
    except OSError as exc:
        governor.release(conn, experiment_lease["lease_id"], "process launch failed")
        governor.set_mode(conn, "discover", config)
        return {"ok": False, "reason": f"не удалось запустить процесс: {exc}"}

    try:
        cur = conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, dry_run, pid, log_path)"
            " VALUES (?,?,'running',?,?,?,?)",
            (hid, level, core.iso(), 1 if dry_run else 0, proc.pid, log_path))
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        try:
            proc.terminate()
        except OSError:
            pass
        governor.release(conn, experiment_lease["lease_id"], "state insert failed")
        governor.set_mode(conn, "discover", config)
        return {"ok": False, "reason": f"не удалось записать состояние прогона: {exc}"}
    q.set_status(conn, hid, "running", level)
    core.log_event(conn, "dispatch.launch", hid, level=level, pid=proc.pid,
                   dry_run=dry_run, gpu=snap.get("free_gb"),
                   governor_lease=experiment_lease.get("lease_id"))
    crew.safe_emit("launch", conn=conn, ctx={
        "hid": hid, "level": level, "budget": float(
            core.cfg("researchagen.limits.daily_gpu_hours_budget", 20, config)),
        "burn": round(gpu_hours_today(conn), 1)})
    tg.send(tg.progress_card(hid, level, 0.0,
                             "Запущен" + (" (dry-run, отладка)" if dry_run else ""),
                             {"прогноз": f"{row['forecast']}%",
                              "оценка": f"{est:.1f} GPU-ч",
                              "VRAM": f"{snap.get('free_gb', 0):.1f} GB свободно"}),
             silent=True)
    return {"ok": True, "id": hid, "level": level, "run_id": cur.lastrowid,
            "pid": proc.pid, "dry_run": dry_run, "log": log_path, "gpu": why,
            "governor_lease": experiment_lease.get("lease_id")}


def finish(conn, hid: str, gpu_hours: float = 0.0, state: str = "done",
           config: dict | None = None) -> dict:
    row = conn.execute(
        "SELECT * FROM runs WHERE hypo_id=? AND state='running' ORDER BY run_id DESC LIMIT 1",
        (hid,)).fetchone()
    if row is None:
        return {"ok": False, "reason": f"активного прогона {hid} нет"}
    conn.execute("UPDATE runs SET state=?, finished_at=?, gpu_hours=? WHERE run_id=?",
                 (state, core.iso(), float(gpu_hours), row["run_id"]))
    conn.commit()
    q.set_status(conn, hid, "paused_checkpoint" if state == "done" else "blocked")
    governor_result = governor.finish_experiment(
        conn, hid, config=config if config is not None else core.load_config()
    )
    core.log_event(conn, "dispatch.finish", hid, state=state, gpu_hours=gpu_hours,
                   governor=governor_result)
    crew.safe_emit("finish_ok" if state == "done" else "finish_fail", conn=conn, ctx={
        "hid": hid, "gpu_hours": gpu_hours, "state": state})
    return {"ok": True, "id": hid, "state": state, "gpu_hours": gpu_hours,
            "governor": governor_result,
            "note": "Статус — checkpoint. Дальше обязателен вердикт: /v " + hid}


def preempt(conn, config: dict | None = None) -> dict:
    """R6: если в очереди появилась гипотеза с PPI в N раз выше — прервать текущее
    на ближайшем checkpoint (процесс получает флаг-файл, а не SIGKILL)."""
    config = config if config is not None else core.load_config()
    ratio = float(core.cfg("researchagen.limits.preempt_ratio", 2.0, config))
    active = running_runs(conn)
    if not active:
        return {"ok": False, "reason": "нет активных прогонов"}
    current = active[0]
    cur_row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (current["hypo_id"],)).fetchone()
    cur_ppi = q.ppi(cur_row, config)
    challenger = q.pick_next(conn, config)
    if not challenger:
        return {"ok": False, "reason": "очередь пуста — прерывать не ради чего"}
    if challenger["ppi"] < cur_ppi * ratio:
        return {"ok": False,
                "reason": f"{challenger['id']} PPI {challenger['ppi']:.3f} < "
                          f"{ratio:g}×{cur_ppi:.3f} — прерывание не оправдано"}
    flag_path = os.path.join(core.STATE_DIR, f"stop-{current['hypo_id']}.flag")
    with open(flag_path, "w", encoding="utf-8") as fh:
        fh.write(f"preempted_by={challenger['id']} at={core.iso()}\n")
    conn.execute("UPDATE runs SET state='preempted', finished_at=? WHERE run_id=?",
                 (core.iso(), current["run_id"]))
    conn.commit()
    q.set_status(conn, current["hypo_id"], "paused_checkpoint")
    governor_result = governor.finish_experiment(
        conn, current["hypo_id"], config=config, analysis=False
    )
    core.log_event(conn, "dispatch.preempt", current["hypo_id"],
                   by=challenger["id"], governor=governor_result)
    tg.send(f"*⏸ Прервано на checkpoint*\n{current['hypo_id']} → в очередь\n"
            f"Причина: {challenger['id']} даёт PPI {challenger['ppi']:.3f} против {cur_ppi:.3f}")
    crew.safe_emit("preempt", conn=conn, ctx={
        "hid": current["hypo_id"], "ratio": f"{ratio:g}",
        "challenger": challenger["id"]})
    return {"ok": True, "paused": current["hypo_id"], "in_favor_of": challenger["id"],
            "flag": flag_path}


def tick(conn, config: dict | None = None) -> dict:
    """Один шаг диспетчера. Идемпотентен: безопасно звать каждые 2 минуты."""
    config = config if config is not None else core.load_config()
    if is_paused(conn):
        return {"action": "paused", "reason": "диспетчер остановлен из Telegram"}

    active = running_runs(conn)
    if active:
        pre = preempt(conn, config)
        if pre.get("ok"):
            return {"action": "preempted", **pre}
        run = active[0]
        elapsed = (core.now() - (core.parse_iso(run["started_at"]) or core.now())).total_seconds()
        return {"action": "busy", "hypo_id": run["hypo_id"], "level": run["level"],
                "elapsed": core.human_delta(elapsed), "reason": pre.get("reason")}

    if not bool(core.cfg("researchagen.autolaunch", True, config)):
        return {"action": "idle", "reason": "autolaunch=false — запуск только вручную"}

    nxt = q.pick_next(conn, config)
    if not nxt:
        crew.safe_emit("queue_empty", conn=conn, ctx={
            "min": int(core.cfg("researchagen.limits.min_live_hypotheses", 3, config))})
        return {"action": "idle", "reason": "очередь пуста — нужны новые гипотезы (/dr)"}

    level = "L0" if nxt["level"] in (None, "", "L0") else nxt["level"]
    result = launch(conn, nxt["id"], level, config=config)
    if result.get("ok"):
        return {"action": "launched", **result, "why": nxt["reason"]}
    if "бюджет" in str(result.get("reason") or ""):
        crew.safe_emit("budget_burn", conn=conn, ctx={
            "burn": round(gpu_hours_today(conn), 1),
            "budget": float(core.cfg("researchagen.limits.daily_gpu_hours_budget", 20, config))})
    return {"action": "blocked", "hypo_id": nxt["id"], **result}


def main(argv: list[str]) -> int:
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "tick"
    conn = core.db()

    if cmd == "tick":
        res = tick(conn, config)
        core.emit(res, as_json, f"[{res['action']}] " + str(
            res.get("reason") or res.get("why") or res.get("hypo_id") or ""))
        return 0

    if cmd == "launch":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        res = launch(conn, hid, core.arg(argv, "level", "L0"), core.flag(argv, "force"), config)
        core.emit(res, as_json,
                  (f"Запущено {hid} ({res.get('level')}), pid {res.get('pid')}, лог {res.get('log')}"
                   if res.get("ok") else
                   "Не запущено: " + str(res.get("reason")) +
                   ("\n  • " + "\n  • ".join(res.get("problems", [])) if res.get("problems") else "")))
        return 0 if res.get("ok") else 1

    if cmd == "finish":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        res = finish(conn, hid, float(core.arg(argv, "gpu-hours", 0.0)),
                     core.arg(argv, "state", "done"), config)
        core.emit(res, as_json, str(res.get("note") or res.get("reason")))
        return 0 if res.get("ok") else 1

    if cmd == "preempt":
        res = preempt(conn, config)
        core.emit(res, as_json, str(res.get("reason") or f"Прервано {res.get('paused')}"))
        return 0

    if cmd == "running":
        rows = running_runs(conn)
        text = core.table(
            [[r["hypo_id"], r["level"], r["started_at"],
              "dry-run" if r["dry_run"] else "GPU", str(r["title"])[:34]] for r in rows],
            ["id", "уровень", "старт", "режим", "гипотеза"]) if rows else "Активных прогонов нет."
        core.emit(rows, as_json, text)
        return 0

    if cmd == "approve":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        approve(conn, hid)
        core.emit({"ok": True, "id": hid}, as_json,
                  f"{hid}: дорогой прогон разрешён, диспетчер возьмёт его на ближайшем тике")
        return 0

    if cmd in ("pause", "resume"):
        core.set_setting(conn, "dispatch.paused", cmd == "pause")
        core.log_event(conn, f"dispatch.{cmd}")
        core.emit({"paused": cmd == "pause"}, as_json,
                  "Диспетчер остановлен: новые прогоны не стартуют, research-контур работает"
                  if cmd == "pause" else "Диспетчер снова активен")
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
