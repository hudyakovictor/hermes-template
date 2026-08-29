#!/usr/bin/env python3
"""researchagen — еженедельная калибровка весов PI по фактическим вердиктам.

Логика простая и честная: если фактор у подтверждённых гипотез систематически выше,
чем у опровергнутых — его вес растёт; если различия нет — падает. Шаг
ограничен (±20% относительно), а без минимального числа закрытых гипотез
калибровка НЕ делается вообще: на 3 точках подгонка весов — самообман.

Новые веса пишутся в SQLite (settings), а не в config.yaml: конфиг — ручной
источник правды человека, агент его не переписывает. Отчёт показывает дельту
и предлагает готовый блок для вставки в config.yaml.

CLI:
  python tools/calib.py report [--json]
  python tools/calib.py apply [--min-verdicts 8] [--json]
  python tools/calib.py weights [--json]
"""

from __future__ import annotations

import os
import sys

import core
import queue as q
import verdict as v

FACTORS = {
    "signals": "signals",
    "novelty": "novelty",
    "early": "early_pct",
    "standard": "standard",
    "money": "money",
    "decidability": "decidability",
}
MAX_REL_STEP = 0.20
DEFAULT_MIN_VERDICTS = 8


def effective_weights(conn, config: dict | None = None) -> dict:
    """Веса из SQLite если есть, иначе из config.yaml."""
    stored = core.setting(conn, "pi_weights")
    if isinstance(stored, dict) and stored:
        total = sum(float(x) for x in stored.values()) or 1.0
        return {k: float(x) / total for k, x in stored.items()}
    return q.weights(config)


def factor_value(row, factor: str) -> float:
    column = FACTORS[factor]
    raw = row[column]
    if factor == "signals":
        return q.signal_score(int(raw or 0))
    if factor == "early":
        return q.early_score(float(raw or 10.0))
    return q.clamp01(float(raw or 0.0))


def _row_weight(row) -> float:
    """#25: вердикты гипотез с патентной заготовкой весят вдвое — подтверждённое
    и продаваемое должно учить калибровку сильнее, чем просто подтверждённое."""
    path = os.path.join(core.REPORTS_DIR, f"patent-{row['id']}.md")
    return 2.0 if os.path.exists(path) else 1.0


def discrimination(conn) -> dict:
    """Для каждого фактора: среднее у удачных минус среднее у провальных.

    Средние взвешенные: патентные гипотезы (есть reports/patent-*.md) идут
    с весом 2 — калибровка учится на том, что реально монетизируется.
    """
    rows = conn.execute(
        "SELECT h.*, vd.kind FROM verdicts vd "
        "JOIN hypotheses h ON h.id=vd.hypo_id "
        "WHERE vd.kind IN ('confirmed','partial','rejected')"
    ).fetchall()
    good = [r for r in rows if r["kind"] in ("confirmed", "partial")]
    bad = [r for r in rows if r["kind"] == "rejected"]

    def wmean(items, factor):
        if not items:
            return None
        num = sum(factor_value(r, factor) * _row_weight(r) for r in items)
        den = sum(_row_weight(r) for r in items)
        return num / den if den else None

    out = {"n_total": len(rows), "n_good": len(good), "n_bad": len(bad), "factors": {}}
    for factor in FACTORS:
        gm, bm = wmean(good, factor), wmean(bad, factor)
        out["factors"][factor] = {
            "mean_good": None if gm is None else round(gm, 3),
            "mean_bad": None if bm is None else round(bm, 3),
            "delta": None if (gm is None or bm is None) else round(gm - bm, 3),
        }
    return out


def proposed_weights(conn, config: dict | None = None) -> dict:
    current = effective_weights(conn, config)
    disc = discrimination(conn)
    deltas = {f: (disc["factors"][f]["delta"] or 0.0) for f in FACTORS}
    scale = max((abs(d) for d in deltas.values()), default=0.0)
    new = {}
    for factor, weight in current.items():
        if scale < 1e-9:
            new[factor] = weight
            continue
        adj = (deltas.get(factor, 0.0) / scale) * MAX_REL_STEP
        new[factor] = max(0.02, weight * (1 + adj))
    total = sum(new.values()) or 1.0
    return {k: round(x / total, 4) for k, x in new.items()}


def report(conn, config: dict | None = None) -> dict:
    disc = discrimination(conn)
    current = {k: round(x, 4) for k, x in effective_weights(conn, config).items()}
    proposal = proposed_weights(conn, config)
    return {"calibration": v.calibration(conn), "discrimination": disc,
            "weights_current": current, "weights_proposed": proposal,
            "min_verdicts": DEFAULT_MIN_VERDICTS}


def apply(conn, min_verdicts: int = DEFAULT_MIN_VERDICTS, config: dict | None = None) -> dict:
    data = report(conn, config)
    n = data["discrimination"]["n_total"]
    if n < min_verdicts:
        return {"applied": False,
                "reason": f"закрытых гипотез {n} < {min_verdicts} — веса не меняются, "
                          "калибровка на малой выборке — самообман", **data}
    if data["discrimination"]["n_bad"] == 0 or data["discrimination"]["n_good"] == 0:
        return {"applied": False,
                "reason": "есть только один класс исходов — различать нечего", **data}
    core.set_setting(conn, "pi_weights", data["weights_proposed"])
    core.set_setting(conn, "pi_weights.applied_at", core.iso())
    core.log_event(conn, "calib.apply", None, weights=data["weights_proposed"])
    return {"applied": True, **data}


def yaml_block(weights: dict) -> str:
    lines = ["  pi_weights:"]
    for key, value in weights.items():
        lines.append(f"    {key}: {value}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "report"
    conn = core.db()

    if cmd in ("report", "apply"):
        data = apply(conn, int(core.arg(argv, "min-verdicts", DEFAULT_MIN_VERDICTS)), config) \
            if cmd == "apply" else report(conn, config)
        rows = []
        for factor, info in data["discrimination"]["factors"].items():
            rows.append([factor, info["mean_good"], info["mean_bad"], info["delta"],
                         data["weights_current"].get(factor),
                         data["weights_proposed"].get(factor)])
        text = core.table(rows, ["фактор", "удачные", "провальные", "дельта",
                                 "вес сейчас", "вес предложен"])
        cal = data["calibration"]
        text += (f"\n\nЗакрытых гипотез: {data['discrimination']['n_total']} "
                 f"(удачных {data['discrimination']['n_good']}, "
                 f"опровергнутых {data['discrimination']['n_bad']})"
                 f"\nОшибка прогнозов: {cal['mean_abs_deviation_pct']}% | "
                 f"сдвиг {cal['bias_pct']}% | GPU-ч на подтверждение "
                 f"{cal['gpu_hours_per_confirmed']}")
        if cmd == "apply":
            text += ("\nВеса обновлены (действуют сразу)." if data.get("applied")
                     else f"\nВеса НЕ обновлены: {data.get('reason')}")
        else:
            text += "\n\nБлок для config.yaml (по желанию, вручную):\n" \
                    + yaml_block(data["weights_proposed"])
        core.emit(data, as_json, text)
        return 0

    if cmd == "weights":
        w = effective_weights(conn, config)
        core.emit(w, as_json, core.table([[k, round(x, 4)] for k, x in w.items()],
                                         ["фактор", "вес"]))
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
