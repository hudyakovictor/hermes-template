"""Built-in asynchronous evaluators for exploratory candidates."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, Sequence

from .config import SkillConfig
from .mcp import MCPClient
from .metrics import Metrics
from .state import Evidence, Hypothesis, SearchState


@dataclass
class Evaluation:
    """One evaluator's bounded result."""

    evaluator: str
    score: float
    evidence: List[Evidence] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)
    cost_usd: float = 0.0


@dataclass
class EvaluationContext:
    """Shared services passed to every evaluator."""

    config: SkillConfig
    mcp: MCPClient
    metrics: Metrics


class Evaluator(Protocol):
    name: str

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        ...


def _clip(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _evidence_id(candidate_id: str, evaluator: str, source: str, index: int) -> str:
    digest = hashlib.sha1(
        f"{candidate_id}|{evaluator}|{source}|{index}".encode("utf-8")
    ).hexdigest()[:14]
    return f"E-{digest}"


class LiteratureMCPEvaluator:
    """Search configured MCP adapters and turn results into evidence."""

    name = "literature"

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        results = await context.mcp.search_all(hypothesis.text)
        evidence: List[Evidence] = []
        direct_flags: List[bool] = []
        for index, result in enumerate(results):
            source = str(
                result.get("source")
                or result.get("url")
                or result.get("doi")
                or f"{result.get('tool', 'mcp')}:{index}"
            )
            claim = str(result.get("claim") or result.get("title") or "")
            if not claim:
                continue
            direct = bool(result.get("direct", False))
            direct_flags.append(direct)
            independent = str(
                result.get("independent_key")
                or result.get("authors")
                or result.get("tool", "mcp")
            )
            evidence.append(
                Evidence(
                    id=_evidence_id(hypothesis.id, self.name, source, index),
                    candidate_id=hypothesis.id,
                    source=source,
                    claim=claim,
                    kind="literature",
                    independent_key=independent,
                    strength=_clip(float(result.get("strength", 0.5) or 0.5)),
                    metadata={"tool": result.get("tool", "mcp"), "direct": direct},
                )
            )
        if not results:
            return Evaluation(
                self.name,
                0.0,
                detail={"available": False, "reason": "MCP не настроен или результатов нет"},
            )
        direct_rate = sum(direct_flags) / len(direct_flags) if direct_flags else 0.0
        score = _clip(1.0 - direct_rate)
        return Evaluation(
            self.name,
            score,
            evidence=evidence,
            detail={"available": True, "results": len(results), "direct_rate": direct_rate},
        )


class MechanismEvaluator:
    """Cheap lexical sanity check for a causal, falsifiable mechanism."""

    name = "mechanism"
    _causal = re.compile(
        r"\b(because|therefore|caus|mechanism|причин|механизм|подав|усил|извлеч)",
        re.IGNORECASE,
    )
    _control = re.compile(
        r"\b(control|ablation|falsif|контрол|абляц|опроверг|критер)",
        re.IGNORECASE,
    )

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        text = f"{hypothesis.text} {hypothesis.mechanism}".strip()
        score = 0.10
        if len(hypothesis.mechanism.strip()) >= 40:
            score += 0.30
        if self._causal.search(text):
            score += 0.30
        if self._control.search(text):
            score += 0.20
        if any(char.isdigit() for char in text):
            score += 0.10
        return Evaluation(
            self.name,
            _clip(score),
            detail={
                "mechanism_chars": len(hypothesis.mechanism.strip()),
                "has_causal_language": bool(self._causal.search(text)),
                "has_control_language": bool(self._control.search(text)),
            },
        )


class ExperimentEvaluator:
    """Estimate whether a candidate has a cheap, numeric next experiment."""

    name = "experiment"

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        hours = max(0.25, float(hypothesis.estimated_hours or 0.25))
        cheapness = _clip(1.0 - (hours - 0.25) / 8.0)
        metadata = hypothesis.metadata
        has_pass_fail = bool(metadata.get("pass_fail") or metadata.get("metric"))
        score = 0.75 * cheapness + 0.25 * (1.0 if has_pass_fail else 0.25)
        return Evaluation(
            self.name,
            _clip(score),
            detail={
                "estimated_hours": hours,
                "has_numeric_gate": has_pass_fail,
                "level": metadata.get("level", "L0"),
            },
        )


class NoveltyEvaluator:
    """Measure independent provenance, not the popularity of a paper."""

    name = "novelty"

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        keys = {
            state.evidence[eid].independent_key
            for eid in hypothesis.evidence_ids
            if eid in state.evidence and state.evidence[eid].independent_key
        }
        keys.update(source for source in hypothesis.signal_sources if source)
        score = _clip(len(keys) / 3.0)
        return Evaluation(
            self.name,
            score,
            detail={"independent_sources": len(keys)},
        )


class CommercialEvaluator:
    """Lightweight market/technology impact prior; final claims need evidence."""

    name = "commercial"
    _terms = (
        "compute",
        "cost",
        "latency",
        "memory",
        "energy",
        "production",
        "компьют",
        "стоим",
        "задерж",
        "энерг",
        "масштаб",
        "патент",
    )

    async def evaluate(
        self,
        hypothesis: Hypothesis,
        state: SearchState,
        context: EvaluationContext,
    ) -> Evaluation:
        text = f"{hypothesis.text} {hypothesis.mechanism}".lower()
        hits = sum(1 for term in self._terms if term in text)
        explicit = hypothesis.metadata.get("commercial_score")
        score = float(explicit) if explicit is not None else min(1.0, hits / 3.0)
        return Evaluation(
            self.name,
            _clip(score),
            detail={"keyword_hits": hits, "explicit_score": explicit is not None},
        )


DEFAULT_EVALUATORS: Sequence[Evaluator] = (
    LiteratureMCPEvaluator(),
    MechanismEvaluator(),
    ExperimentEvaluator(),
    NoveltyEvaluator(),
    CommercialEvaluator(),
)


__all__ = [
    "DEFAULT_EVALUATORS",
    "Evaluation",
    "EvaluationContext",
    "Evaluator",
    "LiteratureMCPEvaluator",
    "MechanismEvaluator",
    "ExperimentEvaluator",
    "NoveltyEvaluator",
    "CommercialEvaluator",
]
