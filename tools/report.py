#!/usr/bin/env python3
"""researchagen — сводки: статус, дайджест, недельный отчёт, заявка на патент.

Всё, что видят оба пользователя в боте, собирается здесь из ОДНОГО источника —
базы state/researchagen.sqlite3. Поэтому что бы кто из них ни спросил — цифры совпадают.

CLI:
  python tools/report.py status [--json]
  python tools/report.py digest [--send] [--json]
  python tools/report.py weekly [--send] [--json]
  python tools/report.py patent H-003 [--json]
"""

from __future__ import annotations

import os
import sys

import core
import crew
import dispatch
import gpu
import queue as q
import tg
import verdict as v


def status(conn, config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    items = q.scored(conn, core.LIVE_STATUSES, config)
    running = dispatch.running_runs(conn)
    snap = gpu.snapshot(config)
    cal = v.calibration(conn)
    plan = [i for i in items if i["status"] == "queued"]
    paused = [i for i in items if i["status"] == "paused_checkpoint"]
    done_rows = conn.execute(
        "SELECT hypo_id, kind, actual, deviation, created_at FROM verdicts "
        "ORDER BY verdict_id DESC LIMIT 5").fetchall()
    return {
        "paused": bool(core.setting(conn, "dispatch.paused", False)),
        "platform": snap["platform"],
        "debug": snap["debug"],
        "gpu": {"available": snap["available"], "free_gb": snap["free_gb"],
                "required_gb": snap["required_gb"]},
        "gpu_hours_today": round(dispatch.gpu_hours_today(conn), 2),
        "gpu_hours_budget": float(core.cfg("researchagen.limits.daily_gpu_hours_budget", 20, config)),
        "running": running,
        "planned": plan,
        "at_checkpoint": paused,
        "live_total": len(items),
        "calibration": cal,
        "recent_verdicts": [dict(r) for r in done_rows],
        "next": q.pick_next(conn, config),
    }


def status_text(data: dict) -> str:
    lines = ["*📊 researchagen — штаб*"]
    if data["paused"]:
        lines.append("⏸ Диспетчер на паузе (/resume снимает)")
    lines.append(f"Платформа: {data['platform']}" + (" (debug, dry-run)" if data["debug"] else ""))
    if data["gpu"]["available"]:
        lines.append(f"GPU: свободно {data['gpu']['free_gb']:.1f} GB "
                     f"(нужно {data['gpu']['required_gb']:.0f} GB)")
    else:
        lines.append("GPU: недоступен")
    lines.append(f"GPU-часов за сутки: {data['gpu_hours_today']} / "
                 f"{data['gpu_hours_budget']:.0f}")
    lines.append("")

    if data["running"]:
        lines.append("*В работе сейчас*")
        for r in data["running"]:
            elapsed = core.human_delta(
                (core.now() - (core.parse_iso(r["started_at"]) or core.now())).total_seconds())
            lines.append(f"• {r['hypo_id']} {r['level']} — {elapsed} "
                         + ("(dry-run)" if r["dry_run"] else ""))
    else:
        lines.append("*В работе сейчас*: ничего (GPU свободен)")

    if data["at_checkpoint"]:
        lines.append("")
        lines.append("*Ждёт вердикта (checkpoint)*")
        for i in data["at_checkpoint"][:5]:
            lines.append(f"• {i['id']} {i['level']} — {i['title'][:44]}")

    lines.append("")
    lines.append(f"*Запланировано*: {len(data['planned'])} гипотез")
    for i in data["planned"][:5]:
        lines.append(f"• {i['bin']} PPI {i['ppi']:.3f} — {i['id']} {i['title'][:40]}")
    if data["next"]:
        lines.append(f"NEXT → {data['next']['id']} ({data['next']['reason']})")

    if data["recent_verdicts"]:
        lines.append("")
        lines.append("*Последние вердикты*")
        for r in data["recent_verdicts"]:
            dev = "" if r["deviation"] is None else f", отклонение {r['deviation']:+.0f}%"
            lines.append(f"• {r['hypo_id']}: {v.KIND_WORD.get(r['kind'], r['kind'])}{dev}")

    cal = data["calibration"]
    lines.append("")
    lines.append(f"Калибровка: ошибка прогноза {cal['mean_abs_deviation_pct']}%, "
                 f"доля удач {cal['hit_rate']}, "
                 f"GPU-ч на подтверждение {cal['gpu_hours_per_confirmed']}")
    return "\n".join(lines)


def digest(conn, config: dict | None = None) -> dict:
    """Суточный дайджест: только события за 24 ч и только цифры."""
    runs = conn.execute(
        "SELECT r.*, h.title FROM runs r JOIN hypotheses h ON h.id=r.hypo_id "
        "WHERE datetime(r.started_at) >= datetime('now','-1 day') ORDER BY r.run_id"
    ).fetchall()
    verdicts = conn.execute(
        "SELECT * FROM verdicts WHERE datetime(created_at) >= datetime('now','-1 day')"
    ).fetchall()
    added = conn.execute(
        "SELECT id, title, source FROM hypotheses "
        "WHERE datetime(created_at) >= datetime('now','-1 day')"
    ).fetchall()
    killed = conn.execute(
        "SELECT hypo_id, payload FROM events WHERE kind='hypo.killed' "
        "AND datetime(created_at) >= datetime('now','-1 day')"
    ).fetchall()
    gpu_hours = sum(float(r["gpu_hours"] or 0) for r in runs)

    lines = [f"*🗓 Дайджест за сутки — {core.iso()[:10]}*", ""]
    lines.append(f"Новых гипотез: {len(added)} | прогонов: {len(runs)} | "
                 f"вердиктов: {len(verdicts)} | снято до теста: {len(killed)}")
    lines.append(f"Потрачено GPU: {gpu_hours:.2f} ч")
    if verdicts:
        lines.append("")
        lines.append("*Вердикты*")
        for r in verdicts:
            dev = "" if r["deviation"] is None else f" | отклонение {r['deviation']:+.0f}%"
            lines.append(f"• {r['hypo_id']} {r['level']}: "
                         f"{v.KIND_WORD.get(r['kind'], r['kind'])}{dev} | "
                         f"seeds {r['seeds_pass']}/{r['seeds_total']} | "
                         f"{r['gpu_hours']:.2f} GPU-ч")
    if killed:
        lines.append("")
        lines.append("*Снято до эксперимента (сэкономленные часы)*")
        for r in killed:
            lines.append(f"• {r['hypo_id']}")
    if not (added or runs or verdicts or killed):
        lines.append("")
        lines.append("За сутки не случилось ничего. Причина — в штабе (/st): либо пауза, "
                     "либо пустая очередь, либо занята VRAM.")
    text = "\n".join(lines)
    return {"text": text, "runs": len(runs), "verdicts": len(verdicts),
            "added": len(added), "killed": len(killed), "gpu_hours": round(gpu_hours, 2)}


def weekly(conn, config: dict | None = None) -> dict:
    cal = v.calibration(conn)
    by_level = conn.execute(
        "SELECT level, COUNT(*) n, ROUND(COALESCE(SUM(gpu_hours),0),2) h FROM runs "
        "WHERE datetime(started_at) >= datetime('now','-7 day') GROUP BY level"
    ).fetchall()
    survivors = conn.execute(
        "SELECT id, title, level FROM hypotheses WHERE status IN ('confirmed','partial')"
    ).fetchall()
    lines = [f"*📈 Недельный отчёт — {core.iso()[:10]}*", ""]
    lines.append(core.table([[r["level"], r["n"], r["h"]] for r in by_level],
                            ["уровень", "прогонов", "GPU-ч"]))
    lines.append("")
    lines.append(f"Выживших гипотез (подтверждено/частично): {len(survivors)}")
    for r in survivors[:10]:
        lines.append(f"• {r['id']} {r['level']} — {r['title'][:50]}")
    lines.append("")
    lines.append(f"Ошибка прогноза: {cal['mean_abs_deviation_pct']}% | "
                 f"сдвиг {cal['bias_pct']}% | всего GPU-ч {cal['gpu_hours_spent']}")
    text = "\n".join(lines)
    path = os.path.join(core.REPORTS_DIR, f"weekly-{core.iso()[:10]}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"text": text, "path": path, "survivors": len(survivors)}


PATENT_TEMPLATE = """# Заявочная заготовка — {hid}

**Название метода:** {title}

## 1. Техническая проблема
Полное обучение требуется для того, чтобы узнать, будет ли конфигурация полезной.
Затраты compute несутся до получения информации о ценности результата.

## 2. Суть метода (независимый пункт)
Способ раннего отбора, включающий шаги:
1. измерение набора внутренних наблюдаемых величин на шагах обучения ≤ {early}% бюджета;
2. вычисление агрегатного критерия по этим величинам;
3. прекращение или продолжение обучения по порогу критерия;
4. перераспределение высвобождённого compute на выжившие конфигурации.

## 3. Зависимые пункты (варианты)
- выбор конкретных наблюдаемых (согласование знаков градиента, ранги норм, кривизна loss);
- адаптивный порог вместо фиксированного;
- применение к поиску архитектуры / гиперпараметров / данных;
- распределённая реализация (отбор веток на кластере).

## 4. Доказательства работоспособности (из базы, а не из текста)
{evidence}

## 5. Обходы и границы
Указать условия, при которых метод не работает (из раздела falsification карточки).

> Это техническая заготовка, а не юридический документ. Патентная пригодность
> определяется патентным поверенным, а не агентом.
"""


def patent(conn, hid: str) -> dict:
    row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if row is None:
        core.fail(f"{hid} не найдена")
    if row["status"] not in ("confirmed", "partial"):
        core.fail(f"{hid}: статус {row['status']}. Заготовка делается только после "
                  "подтверждения на L2/L3.")
    vs = conn.execute("SELECT * FROM verdicts WHERE hypo_id=? ORDER BY verdict_id", (hid,)).fetchall()
    evidence = "\n".join(
        f"- {r['level']}: факт {r['actual']}% при прогнозе {r['forecast']}% "
        f"(отклонение {r['deviation']}%), seeds {r['seeds_pass']}/{r['seeds_total']}, "
        f"{r['gpu_hours']:.2f} GPU-ч" for r in vs) or "- нет записанных вердиктов"
    text = PATENT_TEMPLATE.format(hid=hid, title=row["title"],
                                 early=row["early_pct"], evidence=evidence)
    path = os.path.join(core.REPORTS_DIR, f"patent-{hid}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"path": path, "text": text}


def main(argv: list[str]) -> int:
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "status"
    conn = core.db()

    if cmd == "status":
        data = status(conn, config)
        core.emit(data, as_json, status_text(data))
        return 0
    if cmd == "digest":
        data = digest(conn, config)
        if core.flag(argv, "send"):
            tg.send(data["text"])
            crew.safe_emit("digest", conn=conn, config=config)
        core.emit(data, as_json, data["text"])
        return 0
    if cmd == "weekly":
        data = weekly(conn, config)
        if core.flag(argv, "send"):
            tg.send(data["text"])
            crew.safe_emit("digest", conn=conn, config=config)
        core.emit(data, as_json, data["text"])
        return 0
    if cmd == "patent":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        data = patent(conn, hid)
        core.emit(data, as_json, data["text"])
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
