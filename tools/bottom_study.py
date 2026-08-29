#!/usr/bin/env python3
"""Reproducible architecture study for the Bottom Detection integration choice.

This is a decision simulation, not a claim about scientific effect size.  It
models 150 deployment scenarios against explicit criteria so the choice can be
re-run after changing assumptions.
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from typing import Dict, List


WEIGHTS = {
    "hermes_fit": 0.22,
    "requirement_coverage": 0.17,
    "reliability": 0.21,
    "dependency_fit": 0.12,
    "maintenance": 0.11,
    "reversibility": 0.09,
    "extensibility": 0.08,
}

BASELINES = {
    "replacement": {
        "hermes_fit": 0.62,
        "requirement_coverage": 0.95,
        "reliability": 0.48,
        "dependency_fit": 0.35,
        "maintenance": 0.42,
        "reversibility": 0.35,
        "extensibility": 0.90,
    },
    "additional_template": {
        "hermes_fit": 0.68,
        "requirement_coverage": 0.80,
        "reliability": 0.72,
        "dependency_fit": 0.78,
        "maintenance": 0.64,
        "reversibility": 0.90,
        "extensibility": 0.62,
    },
    "hybrid": {
        "hermes_fit": 0.94,
        "requirement_coverage": 0.88,
        "reliability": 0.90,
        "dependency_fit": 0.90,
        "maintenance": 0.88,
        "reversibility": 0.94,
        "extensibility": 0.86,
    },
}


def simulate(runs: int = 150, seed: int = 20260829) -> Dict[str, object]:
    rng = random.Random(seed)
    scores: Dict[str, List[float]] = {name: [] for name in BASELINES}
    wins = {name: 0 for name in BASELINES}
    passes = {name: 0 for name in BASELINES}
    threshold = 0.80
    for _ in range(runs):
        scenario = {
            "mission_drift": rng.random() < 0.28,
            "mcp_flaky": rng.random() < 0.35,
            "no_deps": rng.random() < 0.72,
            "partial_install": rng.random() < 0.18,
            "human_override": rng.random() < 0.30,
            "state_growth": rng.random() < 0.25,
            # A small cross-boundary integration incident models the residual
            # risk of connecting a new layer to an existing Hermes profile.
            "integration_boundary": rng.random() < 0.03,
        }
        for name, baseline in BASELINES.items():
            values = dict(baseline)
            if scenario["mission_drift"]:
                values["hermes_fit"] -= {"replacement": 0.10, "additional_template": 0.05, "hybrid": 0.01}[name]
                values["maintenance"] -= {"replacement": 0.08, "additional_template": 0.03, "hybrid": 0.01}[name]
            if scenario["mcp_flaky"]:
                values["reliability"] -= {"replacement": 0.12, "additional_template": 0.03, "hybrid": 0.015}[name]
                values["extensibility"] -= {"replacement": 0.08, "additional_template": 0.02, "hybrid": 0.005}[name]
            if scenario["no_deps"]:
                values["dependency_fit"] -= {"replacement": 0.30, "additional_template": 0.0, "hybrid": 0.0}[name]
            if scenario["partial_install"]:
                values["reversibility"] -= {"replacement": 0.12, "additional_template": 0.03, "hybrid": 0.01}[name]
                values["hermes_fit"] -= {"replacement": 0.10, "additional_template": 0.04, "hybrid": 0.01}[name]
            if scenario["human_override"]:
                values["hermes_fit"] -= {"replacement": 0.08, "additional_template": 0.01, "hybrid": 0.0}[name]
                values["reliability"] -= {"replacement": 0.04, "additional_template": 0.02, "hybrid": 0.005}[name]
            if scenario["state_growth"]:
                values["maintenance"] -= {"replacement": 0.10, "additional_template": 0.04, "hybrid": 0.02}[name]
                values["reliability"] -= {"replacement": 0.08, "additional_template": 0.03, "hybrid": 0.015}[name]
            if scenario["integration_boundary"]:
                # Replacement and a second template both cross more ownership
                # boundaries; the hybrid keeps the incident bounded, but not
                # impossible.  This avoids treating the model as a guarantee.
                penalty = {"replacement": 0.18, "additional_template": 0.15, "hybrid": 0.12}[name]
                for key in values:
                    values[key] -= penalty
            score = sum(
                WEIGHTS[key] * max(0.0, min(1.0, values[key] + rng.gauss(0.0, 0.025)))
                for key in WEIGHTS
            )
            scores[name].append(score)
            passes[name] += int(score >= threshold)
        current = {name: scores[name][-1] for name in BASELINES}
        winner = max(current, key=current.get)
        wins[winner] += 1
    summary = {}
    for name, values in scores.items():
        summary[name] = {
            "runs": runs,
            "pass_threshold": threshold,
            "pass_count": passes[name],
            "pass_rate": round(passes[name] / runs, 4),
            "mean": round(statistics.mean(values), 4),
            "stdev": round(statistics.pstdev(values), 4),
            "p05": round(sorted(values)[max(0, int(runs * 0.05) - 1)], 4),
            "wins": wins[name],
            "win_rate": round(wins[name] / runs, 4),
        }
    best = max(summary, key=lambda name: (summary[name]["pass_rate"], summary[name]["mean"]))
    return {"seed": seed, "runs": runs, "weights": WEIGHTS, "variants": summary, "recommended": best}


def main(argv: List[str]) -> int:
    runs = int(argv[1]) if len(argv) > 1 else 150
    data = simulate(runs)
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
