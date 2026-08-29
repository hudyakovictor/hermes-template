"""Configuration for Bottom Detection.

The package deliberately uses only the standard library.  Hermes remains the
process that owns the LLM and native MCP tools; this configuration describes
how the deterministic research layer consumes those capabilities when an
adapter is provided.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import core


@dataclass
class SkillConfig:
    """Bounded configuration for one mission-scoped search run."""

    mission: str
    domain: str
    mission_path: Optional[str] = None
    llm_model: str = "local"
    mcp_tools: List[str] = field(
        default_factory=lambda: ["arxiv", "pubmed", "google_scholar", "github"]
    )
    mcp_endpoints: Dict[str, str] = field(default_factory=dict)
    mcp_commands: Dict[str, List[str]] = field(default_factory=dict)
    transformation_dictionary: Optional[str] = None
    num_initial_regions: int = 10
    max_parallel_evaluations: int = 5
    max_iterations: int = 200
    target_confirmed_hypotheses: int = 3
    max_cost_usd: float = 10.0
    mcp_rate_limit: int = 100
    mcp_cache_ttl_hours: float = 24.0
    retry_attempts: int = 3
    retry_base_seconds: float = 0.25
    region_no_signal_limit: int = 2
    min_signal_score: float = 0.55
    max_region_depth: int = 3
    max_candidates_per_region: int = 4
    max_transformations_per_hypothesis: int = 8
    enable_metrics: bool = True
    enable_tracing: bool = False
    log_level: str = "INFO"
    log_format: str = "json"

    def __post_init__(self) -> None:
        if not self.mission.strip():
            raise ValueError("mission must not be empty")
        if not self.domain.strip():
            raise ValueError("domain must not be empty")
        self.num_initial_regions = max(1, int(self.num_initial_regions))
        self.max_parallel_evaluations = max(
            1, min(10, int(self.max_parallel_evaluations))
        )
        self.max_iterations = max(1, int(self.max_iterations))
        self.target_confirmed_hypotheses = max(
            1, int(self.target_confirmed_hypotheses)
        )
        self.max_cost_usd = max(0.0, float(self.max_cost_usd))
        self.mcp_rate_limit = max(1, int(self.mcp_rate_limit))
        self.mcp_cache_ttl_hours = max(0.0, float(self.mcp_cache_ttl_hours))
        self.retry_attempts = max(1, int(self.retry_attempts))
        self.retry_base_seconds = max(0.0, float(self.retry_base_seconds))
        self.region_no_signal_limit = max(1, int(self.region_no_signal_limit))
        self.min_signal_score = max(0.0, min(1.0, float(self.min_signal_score)))
        self.max_region_depth = max(0, int(self.max_region_depth))
        self.max_candidates_per_region = max(1, int(self.max_candidates_per_region))
        self.max_transformations_per_hypothesis = max(
            1, int(self.max_transformations_per_hypothesis)
        )
        self.log_level = self.log_level.upper()
        self.log_format = self.log_format.lower()

    @classmethod
    def from_profile(
        cls,
        mission_path: Optional[str] = None,
        domain: Optional[str] = None,
        **overrides: Any,
    ) -> "SkillConfig":
        """Build config from the current profile without duplicating mission text.

        ``MISSION.md`` is the authoritative input.  ``FOCUS.md`` remains a
        human-facing refinement and is intentionally not silently substituted
        for the mission file.
        """

        path = mission_path or os.path.join(core.ROOT, "MISSION.md")
        with open(path, "r", encoding="utf-8") as fh:
            mission = fh.read()
        configured_domain = domain or str(
            core.cfg("researchagen.bottom_detection.domain", "training-dynamics")
        )
        raw_tools = core.cfg(
            "researchagen.bottom_detection.mcp_tools",
            "arxiv,pubmed,google_scholar,github",
        )
        if isinstance(raw_tools, str):
            configured_tools = [
                item.strip() for item in raw_tools.split(",") if item.strip()
            ]
        elif isinstance(raw_tools, (list, tuple)):
            configured_tools = [str(item) for item in raw_tools if str(item)]
        else:
            configured_tools = ["arxiv", "pubmed", "google_scholar", "github"]
        values: Dict[str, Any] = {
            "mission": mission,
            "domain": configured_domain,
            "mission_path": path,
            "mcp_tools": configured_tools,
            "transformation_dictionary": core.cfg(
                "researchagen.bottom_detection.transformation_dictionary", None
            ),
            "llm_model": core.cfg(
                "researchagen.bottom_detection.llm_model", "local"
            ),
            "num_initial_regions": core.cfg(
                "researchagen.bottom_detection.num_initial_regions", 10
            ),
            "max_parallel_evaluations": core.cfg(
                "researchagen.bottom_detection.max_parallel_evaluations", 5
            ),
            "max_iterations": core.cfg(
                "researchagen.bottom_detection.max_iterations", 200
            ),
            "target_confirmed_hypotheses": core.cfg(
                "researchagen.bottom_detection.target_confirmed_hypotheses", 3
            ),
            "max_cost_usd": core.cfg(
                "researchagen.bottom_detection.max_cost_usd", 10.0
            ),
            "mcp_rate_limit": core.cfg(
                "researchagen.bottom_detection.mcp_rate_limit", 100
            ),
            "mcp_cache_ttl_hours": core.cfg(
                "researchagen.bottom_detection.mcp_cache_ttl_hours", 24.0
            ),
            "retry_attempts": core.cfg(
                "researchagen.bottom_detection.retry_attempts", 3
            ),
            "retry_base_seconds": core.cfg(
                "researchagen.bottom_detection.retry_base_seconds", 0.25
            ),
            "region_no_signal_limit": core.cfg(
                "researchagen.bottom_detection.region_no_signal_limit", 2
            ),
            "min_signal_score": core.cfg(
                "researchagen.bottom_detection.min_signal_score", 0.55
            ),
            "max_region_depth": core.cfg(
                "researchagen.bottom_detection.max_region_depth", 3
            ),
            "max_candidates_per_region": core.cfg(
                "researchagen.bottom_detection.max_candidates_per_region", 4
            ),
            "max_transformations_per_hypothesis": core.cfg(
                "researchagen.bottom_detection.max_transformations_per_hypothesis", 8
            ),
            "enable_metrics": core.cfg(
                "researchagen.bottom_detection.enable_metrics", True
            ),
            "log_level": core.cfg(
                "researchagen.bottom_detection.log_level", "INFO"
            ),
            "log_format": core.cfg(
                "researchagen.bottom_detection.log_format", "json"
            ),
        }
        values.update(overrides)
        return cls(**values)
