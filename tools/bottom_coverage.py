#!/usr/bin/env python3
"""Measure Bottom Detection test line coverage without third-party packages.

The profile is intentionally stdlib-only, so this small harness uses
``sys.settrace`` and the AST to report executable source lines.  It is a
conservative line estimate suitable for the package's acceptance check; it is
not a replacement for coverage.py in environments where that dependency is
available.
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Set


ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "tools" / "bottom_detection"


def _possible_lines(path: Path) -> Set[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        int(node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.stmt, ast.ExceptHandler))
        and getattr(node, "lineno", None) is not None
    }


def measure() -> Dict[str, object]:
    files = {str(path.resolve()): path for path in PACKAGE.glob("*.py")}
    executed: Dict[str, Set[int]] = {path: set() for path in files}

    def trace(frame: object, event: str, _arg: object) -> object:
        if event == "line":
            code = getattr(frame, "f_code")
            filename = os.path.abspath(str(code.co_filename))
            if filename in executed:
                executed[filename].add(int(getattr(frame, "f_lineno")))
        return trace

    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "tools"))
    stream = io.StringIO()
    sys.settrace(trace)
    try:
        suite = unittest.TestLoader().discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    finally:
        sys.settrace(None)

    by_file: Dict[str, Dict[str, object]] = {}
    total_possible = 0
    total_executed = 0
    for filename, path in sorted(files.items()):
        possible = _possible_lines(path)
        hit = possible & executed[filename]
        by_file[path.name] = {
            "executed": len(hit),
            "possible": len(possible),
            "coverage": round(len(hit) / len(possible), 4) if possible else 1.0,
        }
        total_possible += len(possible)
        total_executed += len(hit)
    return {
        "tests_ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "files": by_file,
        "executed": total_executed,
        "possible": total_possible,
        "coverage": round(total_executed / total_possible, 4) if total_possible else 1.0,
        "test_output": stream.getvalue(),
    }


def main() -> int:
    data = measure()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["tests_ok"] and data["coverage"] >= 0.80 else 1


if __name__ == "__main__":
    raise SystemExit(main())
