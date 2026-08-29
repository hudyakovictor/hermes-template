#!/usr/bin/env python3
"""researchagen — вердикты и калибровка прогнозов.

Формат вердикта жёсткий (SOUL.md): что проверяли → что получили числами →
отклонение от прогноза в % → что из этого меняется → следующее действие.
Никаких «перспективно» и «многообещающе»: такие слова запрещены и отлавливаются тестом.

CLI:
  python tools/verdict.py record H-003 --kind confirmed --actual 11.4 \
      --seeds-pass 3 --seeds-total 3 --sigma 2.8 --gpu-hours 0.8 \
      --changes "L2 разрешён, абляция по знакам градиента"
  python tools/verdict.py list [--limit 10] [--json]
  python tools/verdict.py calibration [--json]
"""

from __future__ import annotations

import sys

import core
import queue as q

BANNED = ("перспективно", "многообещающе", "возможно улучшение",
          "выглядит интересно", "promising")

KIND_STATUS = {"confirmed": "confirmed", "partial": "partial",
               "rejected": "rejected", "killed": "killed"}

KIND_WORD = {
    "confirmed": "ПОДТВЕРЖДЕНО",
    "partial": "ЧАСТИЧНО (эффект есть, но не во всех условиях прогноза)",
    "rejected": "ОПРОВЕРГНУТО",
    "killed": "СНЯТО ДО ЭКСПЕРИМЕНТА",
}


def deviation(forecast, actual) -> float | None:
    """Отклонение факта от прогноза в % от прогноза."""
    if forecast in (None, "") or actual in (None, ""):
        return None
    forecast = float(forecast)
    actual = float(actual)
    if abs(forecast) < 1e-9:
        return None
    return round((actual - forecast) / abs(forecast) * 100.0, 1)


def check_language(text: str) -> list[str]:
    low = (text or "").lower()
    return [w for w in BANNED if w in low]


def render(hid: str, title: str, kind: str, forecast, actual, dev,
           seeds_pass: int, seeds_total: int, sigma, gpu_hours: float,
           changes: str) -> str:
    lines = [f"*⚖️ ВЕРДИКТ {hid} — {KIND_WORD.get(kind, kind)}*", f"{title}", ""]
    lines.append(f"• результат: {actual if actual is not None else '—'}% "
                 f"(прогноз {forecast if forecast is not None else '—'}%)")
    if dev is not None:
        sign = "+" if dev > 0 else ""
        lines.append(f"• отклонение от прогноза: {sign}{dev}%")
    lines.append(f"• seeds: {seeds_pass}/{seeds_total} воспроизвелись")
    if sigma not in (None, ""):
        lines.append(f"• размер эффекта: {sigma}σ от шума seeds")
    lines.append(f"• цена: {gpu_hours:.2f} GPU-ч")
    lines.append("")
    lines.append(f"Что меняется: {changes}")
    return "\n".join(lines)


def record(conn, hid: str, kind: str, actual=None, seeds_pass: int = 0,
           seeds_total: int = 0, sigma=None, gpu_hours: float = 0.0,
           changes: str = "") -> dict:
    row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if row is None:
        core.fail(f"{hid} не найдена")
    if kind not in KIND_STATUS:
        core.fail(f"kind должен быть одним из: {', '.join(KIND_STATUS)}")
    if kind != "killed" and row["forecast"] is None:
        core.fail("вердикт невозможен: прогноз должен быть зафиксирован до запуска")
    if kind != "killed" and actual in (None, ""):
        core.fail("для вердикта нужен фактический результат --actual")
    latest_run = conn.execute(
        "SELECT dry_run FROM runs WHERE hypo_id=? ORDER BY run_id DESC LIMIT 1",
        (hid,),
    ).fetchone()
    if kind != "killed" and latest_run is not None and latest_run["dry_run"]:
        core.fail("dry-run не является научным результатом и не закрывает гипотезу")
    banned = check_language(changes)
    if banned:
        core.fail("в поле --changes запрещённые формулировки: "
                  + ", ".join(banned) + ". Нужно конкретное действие или число.")
    dev = deviation(row["forecast"], actual)
    conn.execute(
        "INSERT INTO verdicts (hypo_id, level, kind, forecast, actual, deviation,"
        " seeds_pass, seeds_total, sigma, gpu_hours, what_changes, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, row["level"], kind, row["forecast"],
         None if actual in (None, "") else float(actual), dev,
         int(seeds_pass), int(seeds_total),
         None if sigma in (None, "") else float(sigma),
         float(gpu_hours), changes, core.iso()),
    )
    conn.commit()
    q.set_status(conn, hid, KIND_STATUS[kind])
    core.log_event(conn, "verdict", hid, verdict_kind=kind, actual=actual,
                   deviation=dev, gpu_hours=gpu_hours)
    text = render(hid, row["title"], kind, row["forecast"], actual, dev,
                  int(seeds_pass), int(seeds_total), sigma, float(gpu_hours), changes)
    path = f"{core.REPORTS_DIR}/verdict-{hid}.md"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n\n---\n\n")
    return {"ok": True, "id": hid, "kind": kind, "deviation": dev,
            "text": text, "report": path}


def calibration(conn) -> dict:
    rows = conn.execute(
        "SELECT kind, forecast, actual, deviation, gpu_hours FROM verdicts "
        "WHERE deviation IS NOT NULL"
    ).fetchall()
    counts = {k: 0 for k in KIND_STATUS}
    for r in conn.execute("SELECT kind, COUNT(*) n FROM verdicts GROUP BY kind").fetchall():
        counts[r["kind"]] = r["n"]
    if rows:
        devs = [abs(float(r["deviation"])) for r in rows]
        bias = sum(float(r["deviation"]) for r in rows) / len(rows)
        mae = sum(devs) / len(devs)
    else:
        bias, mae = None, None
    total = sum(counts.values())
    spent = conn.execute("SELECT COALESCE(SUM(gpu_hours),0) FROM verdicts").fetchone()[0]
    hit = counts["confirmed"] + counts["partial"]
    return {
        "verdicts": total,
        "by_kind": counts,
        "mean_abs_deviation_pct": None if mae is None else round(mae, 1),
        "bias_pct": None if bias is None else round(bias, 1),
        "hit_rate": None if total == 0 else round(hit / total, 3),
        "gpu_hours_spent": round(float(spent), 2),
        "gpu_hours_per_confirmed": None if counts["confirmed"] == 0
        else round(float(spent) / counts["confirmed"], 2),
    }


def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "list"
    conn = core.db()

    if cmd == "record":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        res = record(
            conn, hid,
            kind=core.arg(argv, "kind") or core.fail("нужен --kind"),
            actual=core.arg(argv, "actual"),
            seeds_pass=core.arg(argv, "seeds-pass", 0),
            seeds_total=core.arg(argv, "seeds-total", 0),
            sigma=core.arg(argv, "sigma"),
            gpu_hours=float(core.arg(argv, "gpu-hours", 0.0)),
            changes=core.arg(argv, "changes", ""),
        )
        core.emit(res, as_json, res["text"])
        return 0

    if cmd == "list":
        limit = int(core.arg(argv, "limit", 10))
        rows = conn.execute(
            "SELECT v.*, h.title FROM verdicts v JOIN hypotheses h ON h.id=v.hypo_id "
            "ORDER BY v.verdict_id DESC LIMIT ?", (limit,)).fetchall()
        items = [dict(r) for r in rows]
        text = core.table(
            [[r["hypo_id"], r["kind"], r["level"],
              "—" if r["actual"] is None else f"{r['actual']:.1f}",
              "—" if r["deviation"] is None else f"{r['deviation']:+.0f}%",
              f"{r['seeds_pass']}/{r['seeds_total']}",
              f"{r['gpu_hours']:.2f}", str(r["title"])[:34]] for r in rows],
            ["id", "вердикт", "ур", "факт%", "откл", "seeds", "GPUч", "гипотеза"])
        core.emit(items, as_json, text)
        return 0

    if cmd == "calibration":
        data = calibration(conn)
        text = "\n".join([
            f"Вердиктов всего: {data['verdicts']}",
            f"Подтверждено/частично/опровергнуто/снято: "
            f"{data['by_kind']['confirmed']}/{data['by_kind']['partial']}/"
            f"{data['by_kind']['rejected']}/{data['by_kind']['killed']}",
            f"Средняя ошибка прогноза: {data['mean_abs_deviation_pct']}%",
            f"Систематический сдвиг: {data['bias_pct']}% "
            "(>0 — прогнозы занижены, <0 — завышены)",
            f"Доля удач: {data['hit_rate']}",
            f"GPU-часов на одно подтверждение: {data['gpu_hours_per_confirmed']}",
        ])
        core.emit(data, as_json, text)
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
