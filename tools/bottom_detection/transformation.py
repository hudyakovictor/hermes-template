"""Controlled hypothesis transformations for signal-preserving backtracking."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .config import SkillConfig
from .state import Hypothesis


DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "gradient": ["weight", "activation", "loss", "parameter", "feature"],
    "anti-correlation": [
        "sparsity",
        "low-rank",
        "dynamics",
        "competition",
        "suppression",
    ],
    "pruning": [
        "channel pruning",
        "filter pruning",
        "attention head pruning",
        "embedding pruning",
        "policy pruning",
    ],
}

DEFAULT_RELATED_CONCEPTS: Dict[str, List[str]] = {
    "activation sparsity": [
        "neuron pruning",
        "dropout",
        "weight decay",
        "batch normalization",
        "layer normalization",
    ],
    "neuron pruning": [
        "channel pruning",
        "filter pruning",
        "attention head pruning",
        "embedding pruning",
    ],
}

DEFAULT_CROSS_DOMAIN: Dict[str, Dict[str, List[str]]] = {
    "neuron pruning": {
        "computer-vision": ["channel pruning", "filter pruning"],
        "nlp": ["attention head pruning", "embedding pruning"],
        "reinforcement-learning": ["policy pruning", "value-function pruning"],
        "optimization": ["gradient pruning", "Hessian pruning"],
    },
    "gradient": {
        "optimization": ["gradient pruning", "Hessian pruning"],
        "computer-vision": ["filter saliency", "feature-map sparsity"],
    },
}


@dataclass
class TransformationResult:
    """One provenance-preserving transformation proposal."""

    original_hypothesis: Hypothesis
    transformed_hypotheses: List[Hypothesis]
    transformation_type: str
    confidence: float
    rationale: str = ""


class TransformationSkill:
    """Expand a search frontier without silently changing its provenance.

    Transformations are intentionally deterministic.  They create new
    candidates with neutral scores and an ``origin_id``; evaluators must earn
    their scores again instead of inheriting the parent's apparent strength.
    """

    def __init__(
        self,
        config: SkillConfig | Mapping[str, Any] | None = None,
        synonyms: Optional[Mapping[str, Iterable[str]]] = None,
        related_concepts: Optional[Mapping[str, Iterable[str]]] = None,
        cross_domain: Optional[Mapping[str, Mapping[str, Iterable[str]]]] = None,
    ) -> None:
        self.config = config
        self.synonyms = {
            key: list(values)
            for key, values in (synonyms or DEFAULT_SYNONYMS).items()
        }
        self.related_concepts = {
            key: list(values)
            for key, values in (related_concepts or DEFAULT_RELATED_CONCEPTS).items()
        }
        self.cross_domain = {
            key: {domain: list(values) for domain, values in domains.items()}
            for key, domains in (cross_domain or DEFAULT_CROSS_DOMAIN).items()
        }
        dictionary_path = self._dictionary_path(config)
        if dictionary_path:
            self._load_external(dictionary_path)

    @staticmethod
    def _dictionary_path(config: SkillConfig | Mapping[str, Any] | None) -> Optional[str]:
        if isinstance(config, SkillConfig):
            value = config.transformation_dictionary
        elif isinstance(config, Mapping):
            value = config.get("transformation_dictionary")
        else:
            value = None
        return str(value) if value else None

    def _load_external(self, path: str) -> None:
        """Load optional JSON dictionaries; malformed files fail safely."""

        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        for key, target in (
            ("synonyms", self.synonyms),
            ("related_concepts", self.related_concepts),
        ):
            values = payload.get(key)
            if isinstance(values, dict):
                for term, replacements in values.items():
                    if isinstance(replacements, list):
                        target[str(term)] = [str(item) for item in replacements]
        cross = payload.get("cross_domain_analogies")
        if isinstance(cross, dict):
            for term, domains in cross.items():
                if not isinstance(domains, dict):
                    continue
                self.cross_domain[str(term)] = {
                    str(domain): [str(item) for item in values]
                    for domain, values in domains.items()
                    if isinstance(values, list)
                }

    def transform_via_synonyms(self, hypothesis: Hypothesis) -> List[TransformationResult]:
        return self._replace_transformations(
            hypothesis, self.synonyms, "synonym", 0.80,
            "термин заменён контролируемым синонимом",
        )

    def transform_via_related_concepts(self, hypothesis: Hypothesis) -> List[TransformationResult]:
        return self._replace_transformations(
            hypothesis, self.related_concepts, "related_concept", 0.70,
            "термин заменён смежной измеримой концепцией",
        )

    def transform_via_cross_domain(self, hypothesis: Hypothesis) -> List[TransformationResult]:
        results: List[TransformationResult] = []
        lower = hypothesis.text.lower()
        for term, domains in self.cross_domain.items():
            if term.lower() not in lower:
                continue
            for domain, analogies in domains.items():
                for analogy in analogies:
                    text = f"{hypothesis.text} [аналогия: {analogy} в {domain}]"
                    child = self._child(hypothesis, text, "cross_domain", domain)
                    results.append(
                        TransformationResult(
                            original_hypothesis=hypothesis,
                            transformed_hypotheses=[child],
                            transformation_type="cross_domain",
                            confidence=0.60,
                            rationale=f"перенос проверяемого контура в {domain}",
                        )
                    )
        return results

    def transform(self, hypothesis: Hypothesis) -> List[TransformationResult]:
        """Run synonym, related-concept and cross-domain expansion in order."""

        output: List[TransformationResult] = []
        output.extend(self.transform_via_synonyms(hypothesis))
        output.extend(self.transform_via_related_concepts(hypothesis))
        output.extend(self.transform_via_cross_domain(hypothesis))
        limit = self._limit()
        return output[:limit]

    def _replace_transformations(
        self,
        hypothesis: Hypothesis,
        dictionary: Mapping[str, Iterable[str]],
        kind: str,
        confidence: float,
        rationale: str,
    ) -> List[TransformationResult]:
        results: List[TransformationResult] = []
        for term, replacements in dictionary.items():
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            if not pattern.search(hypothesis.text):
                continue
            for replacement in replacements:
                text = pattern.sub(str(replacement), hypothesis.text, count=1)
                if text == hypothesis.text:
                    continue
                child = self._child(hypothesis, text, kind, str(replacement))
                results.append(
                    TransformationResult(
                        original_hypothesis=hypothesis,
                        transformed_hypotheses=[child],
                        transformation_type=kind,
                        confidence=confidence,
                        rationale=rationale,
                    )
                )
        return results

    def _child(
        self, hypothesis: Hypothesis, text: str, kind: str, label: str
    ) -> Hypothesis:
        digest = hashlib.sha1(
            f"{hypothesis.id}|{kind}|{label}|{text}".encode("utf-8")
        ).hexdigest()[:12]
        return Hypothesis(
            id=f"{hypothesis.id}:{kind}:{digest}",
            region_id=hypothesis.region_id,
            text=text,
            mechanism=hypothesis.mechanism,
            signal_sources=list(hypothesis.signal_sources),
            estimated_hours=hypothesis.estimated_hours,
            forecast=hypothesis.forecast,
            origin_id=hypothesis.id,
            metadata={
                "transformation_type": kind,
                "transformation_label": label,
            },
        )

    def _limit(self) -> int:
        if isinstance(self.config, SkillConfig):
            return self.config.max_transformations_per_hypothesis
        if isinstance(self.config, Mapping):
            try:
                return max(1, int(self.config.get("max_transformations_per_hypothesis", 8)))
            except (TypeError, ValueError):
                pass
        return 8


__all__ = ["TransformationResult", "TransformationSkill"]
