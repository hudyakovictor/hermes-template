#!/usr/bin/env python3
"""CLI bridge for the hybrid Bottom Detection layer.

Examples:
  python tools/bottom_detection_cli.py init
  python tools/bottom_detection_cli.py run --iterations 1
  python tools/bottom_detection_cli.py regions --json
  python tools/bottom_detection_cli.py candidate R-001 "..." --mechanism "..."
  python tools/bottom_detection_cli.py evidence H-... --source URL --claim "..."
  python tools/bottom_detection_cli.py promote H-...
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Dict, List

import core

from bottom_detection import BottomDetectionSkill, SkillConfig, format_verdict


def _skill() -> BottomDetectionSkill:
    core.load_env()
    if not bool(core.cfg("researchagen.bottom_detection.enabled", True)):
        core.fail("Bottom Detection отключён в researchagen.bottom_detection.enabled")
    return BottomDetectionSkill(SkillConfig.from_profile())


def _json_flag(argv: List[str]) -> bool:
    return core.wants_json(argv)


def _print(payload: Any, as_json: bool, text: str = "") -> None:
    core.emit(payload, as_json, text or payload)


def main(argv: List[str]) -> int:
    command = argv[1] if len(argv) > 1 else "stats"
    as_json = _json_flag(argv)
    skill = _skill()
    try:
        if command == "init":
            _print(
                skill.stats(),
                as_json,
                f"Инициализировано регионов: {len(skill.state.regions)}; "
                f"namespace {skill.state.namespace}",
            )
            return 0
        if command == "run":
            raw_iterations = core.arg(argv, "iterations")
            iterations = int(raw_iterations) if raw_iterations else None
            result = asyncio.run(skill.run(iterations))
            _print(
                result,
                as_json,
                "\n\n".join(result["verdicts"].values())
                if result["verdicts"]
                else "Bottom Detection: новых кандидатов нет",
            )
            return 0
        if command == "stats":
            data = skill.stats()
            _print(data, as_json, json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if command == "regions":
            data = [asdict(item) for item in skill.state.regions.values()]
            _print(data, as_json, _rows_text(data, "id", "name", "status", "signal_score"))
            return 0
        if command in ("candidates", "hypotheses"):
            data = [asdict(item) for item in skill.state.hypotheses.values()]
            data.sort(key=lambda item: (-item["priority"], item["id"]))
            _print(data, as_json, _rows_text(data, "id", "region_id", "status", "priority"))
            return 0
        if command == "candidate":
            region_id = argv[2] if len(argv) > 2 else core.fail("нужен region id")
            text = argv[3] if len(argv) > 3 else core.fail("нужен текст кандидата")
            item = skill.add_candidate(
                region_id,
                text,
                mechanism=core.arg(argv, "mechanism", ""),
                signal_sources=_csv(core.arg(argv, "sources", "")),
                estimated_hours=float(core.arg(argv, "hours", 0.25)),
                forecast=_float_or_none(core.arg(argv, "forecast")),
                metadata={
                    "level": core.arg(argv, "level", "L0"),
                    "metric": core.arg(argv, "metric", ""),
                    "pass_fail": core.arg(argv, "pass-fail", ""),
                },
            )
            _print(asdict(item), as_json, f"Кандидат {item.id} добавлен в {region_id}")
            return 0
        if command == "evidence":
            hypothesis_id = argv[2] if len(argv) > 2 else core.fail("нужен hypothesis id")
            item = skill.add_evidence(
                hypothesis_id,
                core.arg(argv, "source", ""),
                core.arg(argv, "claim", ""),
                kind=core.arg(argv, "kind", "literature"),
                independent_key=core.arg(argv, "independent", ""),
                strength=float(core.arg(argv, "strength", 0.5)),
            )
            _print(asdict(item), as_json, f"Evidence {item.id} добавлено к {hypothesis_id}")
            return 0
        if command == "transform":
            hypothesis_id = argv[2] if len(argv) > 2 else core.fail("нужен hypothesis id")
            hypothesis = skill._get_hypothesis(hypothesis_id)
            results = skill.transformer.transform(hypothesis)
            data = [asdict(result) for result in results]
            _print(data, as_json, f"Трансформаций: {len(data)}")
            return 0
        if command == "promote":
            hypothesis_id = argv[2] if len(argv) > 2 else core.fail("нужен hypothesis id")
            data = skill.promote(hypothesis_id)
            _print(data, as_json, f"{hypothesis_id} передан в основную очередь")
            return 0
        if command == "verdict":
            hypothesis_id = argv[2] if len(argv) > 2 else core.fail("нужен hypothesis id")
            hypothesis = skill._get_hypothesis(hypothesis_id)
            evidence = [
                skill.state.evidence[eid]
                for eid in hypothesis.evidence_ids
                if eid in skill.state.evidence
            ]
            _print({"id": hypothesis_id, "text": format_verdict(hypothesis, evidence)}, as_json,
                   format_verdict(hypothesis, evidence))
            return 0
        core.fail(f"неизвестная команда {command!r}")
    finally:
        skill.close()
    return 2


def _csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _float_or_none(value: Any) -> Any:
    return None if value in (None, "") else float(value)


def _rows_text(items: List[Dict[str, Any]], *keys: str) -> str:
    if not items:
        return "_пусто_"
    header = list(keys)
    rows = [[item.get(key, "") for key in keys] for item in items]
    return core.table(rows, header)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
