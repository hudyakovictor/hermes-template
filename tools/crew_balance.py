#!/usr/bin/env python3
"""Симуляция баланса чата экипажа: 15 прогонов реалистичного потока событий.

Каждый прогон — свежая база и ~40 событий с типовым миксом (deep research
не идёт: hypo/idea/gate/verdict/kill/review-события + редкие idle). Считаем
долю реплик каждого агента и молчунов. Запуск:

    python3 tools/crew_balance.py            # 15 симуляций, сводка
    python3 tools/crew_balance.py --json
"""
from __future__ import annotations

import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
import crew  # noqa: E402

# микс событий «деп-ресерч не идёт»: конвейер живёт идеями, гейтами и вердиктами
EVENT_MIX: list[tuple[str, dict]] = [
    ("hypo_new", {"hid": "H-0{n}", "forecast": 12, "signals": 4, "hours": 2, "ppi": 0.4}),
    ("idea_intake", {"iid": "IN-0{n}", "title": "ранняя остановка по норме весов"}),
    ("idea_review", {"hid": "H-0{n}"}),
    ("idea_rejected", {"iid": "IN-0{n}", "reason": "сигналов меньше трёх"}),
    ("idea_dup", {"iid": "IN-0{n}"}),
    ("gate_pass", {"hid": "H-0{n}", "passed": 8, "total": 8}),
    ("gate_fail", {"hid": "H-0{n}", "passed": 5, "total": 8, "hours": 3}),
    ("launch", {"hid": "H-0{n}", "level": "L0", "hours": 1}),
    ("finish_ok", {"hid": "H-0{n}", "gpu_hours": 0.8, "seeds": 3}),
    ("verdict_confirmed", {"hid": "H-0{n}", "actual": 11, "forecast": 12, "dev": 8, "bets_result": "ставки: 2 из 3 угадали"}),
    ("verdict_rejected", {"hid": "H-0{n}", "actual": 2, "forecast": 25, "dev": -92, "bets_result": "ставки: 1 из 4"}),
    ("verdict_partial", {"hid": "H-0{n}", "actual": 17, "forecast": 12, "dev": 41}),
    ("kill", {"hid": "H-0{n}"}),
    ("queue_empty", {"min": 3}),
    ("digest", {"open_findings": 2, "spent": 4.2}),
    ("budget_burn", {"burn": 18.0, "budget": 20}),
    ("review_weak_signals", {"hid": "H-0{n}", "signals": 2}),
    ("review_stale_run", {"hid": "H-0{n}"}),
    ("review_forecast_drift", {"bias": 14}),
]

# веса: как в живом контуре без dr — гипотезы и вердикты чаще, idle реже
WEIGHTS = [10, 6, 6, 3, 2, 6, 5, 7, 6, 7, 6, 4, 4, 3, 4, 2, 4, 3, 3]


def one_sim(seed: int, events: int = 40) -> dict[str, int]:
    """Один прогон: свежая база, поток событий, счёт реплик по агентам."""
    tmp = tempfile.TemporaryDirectory()
    core.allow_root(tmp.name)
    conn = core.db(os.path.join(tmp.name, "sim.sqlite3"))
    rng = random.Random(seed)
    config = core.load_config()
    config.setdefault("researchagen", {})["crew"] = dict(
        config.get("researchagen", {}).get("crew", {}),
        dispute_probability=0.35, joke_probability=0.3)
    counts = {a: 0 for a in crew.AGENTS}
    for i in range(events):
        event, ctx = rng.choices(EVENT_MIX, weights=WEIGHTS, k=1)[0]
        ctx = {k: v.replace("{n}", str(100 + i)) if isinstance(v, str) else v
               for k, v in ctx.items()}
        res = crew.emit(event, ctx, conn=conn, config=config,
                        rng=random.Random(rng.random()), send=False, force=True)
        for line in res.get("lines") or []:
            if line.get("agent") in counts and line.get("event") != "nudge":
                counts[line["agent"]] += 1
    conn.close()
    tmp.cleanup()
    return counts


def main() -> int:
    as_json = "--json" in sys.argv
    sims = [one_sim(seed) for seed in range(15)]
    agents = sorted(crew.AGENTS)
    if as_json:
        import json
        core.emit({"sims": sims}, True)
        return 0
    total_all = sum(sum(s.values()) for s in sims)
    print(f"15 симуляций × 40 событий ≈ {total_all} реплик (без «умных фраз»)\n")
    header = "агент      " + "".join(f"s{i+1:<3} ".rjust(6) for i in range(15)) + "  доля"
    print(header)
    for a in agents:
        row = "".join(f"{s[a]:<6}" for s in sims)
        share = sum(s[a] for s in sims) / total_all * 100
        print(f"{crew.AGENTS[a]['name']:<10} {row} {share:5.1f}%")
    worst = min(sum(s[a] for s in sims) for a in agents)
    silent = [crew.AGENTS[a]["name"] for a in agents
              if sum(s[a] for s in sims) / total_all < 0.06]
    print(f"\nминимум у агента: {worst} реплик; почти молчат: {silent or 'никто'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
