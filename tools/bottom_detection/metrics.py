"""Minimal Prometheus text exporter for local observability."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict


_SAFE = re.compile(r"[^a-zA-Z0-9_:]")


class Metrics:
    """In-process counters and gauges; export is optional and dependency-free."""

    def __init__(self) -> None:
        self.counters: Dict[str, float] = defaultdict(float)
        self.gauges: Dict[str, float] = defaultdict(float)

    def inc(self, name: str, value: float = 1.0) -> None:
        self.counters[name] += float(value)

    def set(self, name: str, value: float) -> None:
        self.gauges[name] = float(value)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
        }

    def prometheus(self, prefix: str = "researchagen_bottom_detection") -> str:
        lines = []
        for kind, values in (("counter", self.counters), ("gauge", self.gauges)):
            for name, value in sorted(values.items()):
                metric = _SAFE.sub("_", f"{prefix}_{name}")
                lines.append(f"# TYPE {metric} {kind}")
                lines.append(f"{metric} {value:g}")
        return "\n".join(lines) + ("\n" if lines else "")
