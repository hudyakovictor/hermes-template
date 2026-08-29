"""Optional hybrid Bottom Detection layer for the researchagen profile."""

from .config import SkillConfig
from .evaluators import (
    CommercialEvaluator,
    ExperimentEvaluator,
    Evaluation,
    EvaluationContext,
    LiteratureMCPEvaluator,
    MechanismEvaluator,
    NoveltyEvaluator,
)
from .exceptions import ExperimentError, MCPError, SkillError
from .finalizer import format_verdict
from .mcp import (
    CallableMCPTransport,
    HTTPMCPTransport,
    JsonCommandMCPTransport,
    MCPClient,
    RateLimiter,
    TTLCache,
    retry_async,
)
from .metrics import Metrics
from .skill import BottomDetectionSkill
from .state import Evidence, Hypothesis, Region, SearchState
from .transformation import TransformationResult, TransformationSkill

__version__ = "0.1.0"

__all__ = [
    "BottomDetectionSkill",
    "Region",
    "Hypothesis",
    "Evidence",
    "SearchState",
    "SkillConfig",
    "Evaluation",
    "EvaluationContext",
    "LiteratureMCPEvaluator",
    "MechanismEvaluator",
    "ExperimentEvaluator",
    "NoveltyEvaluator",
    "CommercialEvaluator",
    "TransformationResult",
    "TransformationSkill",
    "format_verdict",
    "Metrics",
    "MCPClient",
    "CallableMCPTransport",
    "HTTPMCPTransport",
    "JsonCommandMCPTransport",
    "RateLimiter",
    "TTLCache",
    "retry_async",
    "SkillError",
    "MCPError",
    "ExperimentError",
]
