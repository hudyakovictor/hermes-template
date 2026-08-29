"""Mission-scoped Bottom Detection orchestration.

This is the hybrid layer: it owns search-tree state and asynchronous evaluator
fan-out, while the existing researchagen SQLite queue, hypothesis gate and
verdict tools remain the only authority for GPU work and scientific closure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import core
import hypo
import queue as q

from .config import SkillConfig
from .evaluators import (
    DEFAULT_EVALUATORS,
    Evaluation,
    EvaluationContext,
    Evaluator,
)
from .exceptions import ExperimentError, SkillError
from .finalizer import format_verdict
from .logging_config import log_extra, setup_logging
from .mcp import HTTPMCPTransport, JsonCommandMCPTransport, MCPClient, MCPTransport
from .metrics import Metrics
from .state import (
    Evidence,
    Hypothesis,
    Region,
    SearchState,
    append_history,
    load_state,
    persist_state,
)
from .transformation import TransformationSkill

LOGGER = logging.getLogger("researchagen.bottom_detection.skill")

PRIORITY_WEIGHTS = {
    "evidence": 0.25,
    "novelty": 0.20,
    "mechanism": 0.20,
    "experiment": 0.15,
    "commercial": 0.10,
    "decidability": 0.10,
}


class BottomDetectionSkill:
    """Explore a mission as a bounded region tree and candidate frontier."""

    def __init__(
        self,
        config: SkillConfig,
        conn: Any = None,
        transport: Optional[MCPTransport] = None,
    ) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level, config.log_format)
        self.conn = conn or core.db()
        self._owns_connection = conn is None
        self.metrics = Metrics()
        self.state = load_state(self.conn, config.mission, config.domain)
        self.transformer = TransformationSkill(config)
        self.evaluators: List[Evaluator] = list(DEFAULT_EVALUATORS)
        selected_transport = transport or self._transport_from_config()
        self.mcp = MCPClient(
            self.conn,
            self.state.namespace,
            config.mcp_tools,
            rate_limit=config.mcp_rate_limit,
            cache_ttl_hours=config.mcp_cache_ttl_hours,
            retry_attempts=config.retry_attempts,
            retry_base_seconds=config.retry_base_seconds,
            transport=selected_transport,
            metrics=self.metrics,
        )
        self._run_id: Optional[int] = None
        if not self.state.regions:
            self._seed_regions()
        self._persist()
        self.logger.info(
            "BottomDetectionSkill initialized",
            extra=log_extra(event="skill_initialized", region_id=None),
        )

    def close(self) -> None:
        """Close a connection created by the skill."""

        if self._owns_connection:
            self.conn.close()
            self._owns_connection = False

    def __enter__(self) -> "BottomDetectionSkill":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def register_evaluator(self, evaluator: Evaluator) -> None:
        """Add or replace a custom evaluator by its stable ``name``."""

        self.evaluators = [e for e in self.evaluators if e.name != evaluator.name]
        self.evaluators.append(evaluator)

    def add_region(
        self,
        name: str,
        query: str,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Region:
        """Add a region to the persistent frontier."""

        query = query.strip()
        if not query:
            raise SkillError("region query must not be empty")
        region_id = self._stable_id("region", parent_id or "root", query)
        if region_id in self.state.regions:
            return self.state.regions[region_id]
        depth = self.state.regions[parent_id].depth + 1 if parent_id in self.state.regions else 0
        region = Region(
            id=region_id,
            name=name.strip() or query.strip()[:80],
            query=query.strip(),
            parent_id=parent_id,
            depth=depth,
            metadata=dict(metadata or {}),
        )
        self.state.regions[region.id] = region
        self._mark_frontier(region.id)
        append_history(
            self.conn,
            self.state,
            "region.created",
            run_id=self._run_id,
            region_id=region.id,
            name=region.name,
            parent_id=parent_id,
        )
        self._persist()
        return region

    def add_candidate(
        self,
        region_id: str,
        text: str,
        mechanism: str = "",
        signal_sources: Optional[Iterable[str]] = None,
        estimated_hours: float = 0.25,
        forecast: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        candidate_id: Optional[str] = None,
    ) -> Hypothesis:
        """Insert a candidate while retaining a deterministic provenance id."""

        if region_id not in self.state.regions:
            raise SkillError(f"unknown region: {region_id}")
        text = text.strip()
        if not text:
            raise SkillError("candidate text must not be empty")
        hid = candidate_id or self._stable_id("hypothesis", region_id, text)
        if hid in self.state.hypotheses:
            return self.state.hypotheses[hid]
        hypothesis = Hypothesis(
            id=hid,
            region_id=region_id,
            text=text,
            mechanism=mechanism.strip(),
            signal_sources=[str(item) for item in (signal_sources or []) if str(item)],
            estimated_hours=max(0.25, float(estimated_hours)),
            forecast=None if forecast is None else float(forecast),
            metadata=dict(metadata or {}),
        )
        self.state.hypotheses[hid] = hypothesis
        append_history(
            self.conn,
            self.state,
            "hypothesis.created",
            run_id=self._run_id,
            region_id=region_id,
            hypothesis_id=hid,
            origin_id=hypothesis.origin_id,
        )
        self._persist()
        return hypothesis

    def add_evidence(
        self,
        hypothesis_id: str,
        source: str,
        claim: str,
        kind: str = "literature",
        independent_key: str = "",
        strength: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Evidence:
        """Attach manually verified evidence before promotion."""

        hypothesis = self._get_hypothesis(hypothesis_id)
        source = source.strip()
        claim = claim.strip()
        if not source or not claim:
            raise SkillError("evidence requires both source and claim")
        evidence_id = self._stable_id("evidence", hypothesis_id, source, claim)
        evidence = self.state.evidence.get(evidence_id)
        if evidence is None:
            evidence = Evidence(
                id=evidence_id,
                candidate_id=hypothesis_id,
                source=source,
                claim=claim,
                kind=kind,
                independent_key=independent_key.strip() or source,
                strength=max(0.0, min(1.0, float(strength))),
                metadata=dict(metadata or {}),
            )
            self.state.evidence[evidence.id] = evidence
        if evidence.id not in hypothesis.evidence_ids:
            hypothesis.evidence_ids.append(evidence.id)
        if evidence.source not in hypothesis.signal_sources:
            hypothesis.signal_sources.append(evidence.source)
        hypothesis.updated_at = core.iso()
        append_history(
            self.conn,
            self.state,
            "evidence.attached",
            run_id=self._run_id,
            hypothesis_id=hypothesis_id,
            source=source,
        )
        self._persist()
        return evidence

    async def run(self, max_iterations: Optional[int] = None) -> Dict[str, Any]:
        """Run one bounded asynchronous search session."""

        limit = min(self.config.max_iterations, max_iterations or self.config.max_iterations)
        started_at = core.iso()
        cursor = self.conn.execute(
            "INSERT INTO bd_runs(namespace,started_at,status) VALUES (?,?,?)",
            (self.state.namespace, started_at, "running"),
        )
        self.conn.commit()
        self._run_id = int(cursor.lastrowid)
        append_history(self.conn, self.state, "run.started", run_id=self._run_id)

        try:
            while self.state.iteration < limit and not self._should_stop():
                region = self._select_next_region()
                if region is None:
                    break
                region.status = "active"
                region.visits += 1
                region.updated_at = core.iso()
                append_history(
                    self.conn,
                    self.state,
                    "region.selected",
                    run_id=self._run_id,
                    region_id=region.id,
                    visits=region.visits,
                )

                candidates = await self._generate_candidates(region)
                evaluated = await self._evaluate_candidates(region, candidates)
                self._update_state(region, evaluated)
                decision = self._decide_action(region)
                await self._apply_action(decision, region)
                self.state.iteration += 1
                self._persist()
                self.metrics.set("iterations", self.state.iteration)
                if decision == "stop":
                    break
        except asyncio.CancelledError:
            self.logger.warning(
                "Bottom Detection run cancelled",
                extra=log_extra(event="run_cancelled", run_id=self._run_id),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - run boundary records failure
            append_history(
                self.conn,
                self.state,
                "run.error",
                run_id=self._run_id,
                error=str(exc),
            )
            self._finish_run("failed")
            raise

        summary = self._finalize()
        self._finish_run("done", summary)
        self.logger.info(
            "Bottom Detection run completed",
            extra=log_extra(
                event="run_completed",
                run_id=self._run_id,
            ),
        )
        return summary

    async def _generate_candidates(self, region: Region) -> List[Hypothesis]:
        """Generate deterministic fallback candidates from mission regions.

        Hermes can add richer LLM-generated candidates through ``add_candidate``;
        the fallback keeps the skill testable and useful without an LLM call.
        """

        existing = [
            h
            for h in self.state.hypotheses.values()
            if h.region_id == region.id and h.status not in ("rejected", "archived")
        ]
        if existing:
            return existing[: self.config.max_candidates_per_region]
        templates = (
            "Ранний измеримый предиктор в {query} появляется до 10% обучения и позволяет остановить полный прогон.",
            "Конкуренция вычислительных контуров в {query} подавляет полезную структуру; маскирование слабого контура должно восстановить метрику.",
            "Разделение полезной структуры и паразитной памяти в {query} сохраняется при контрольной абляции и снижает compute cost.",
            "Стабильность знаков/рангов в {query} предсказывает итоговую generalization без изменения learning rate.",
        )
        output: List[Hypothesis] = []
        for index, template in enumerate(templates[: self.config.max_candidates_per_region]):
            text = template.format(query=region.query)
            mechanism = (
                "Если ранний контур причинно связан с итоговым качеством, то его "
                "стабильность должна сохраняться при контрольной абляции и быть "
                "измерима до завершения обучения."
            )
            candidate = self.add_candidate(
                region.id,
                text,
                mechanism=mechanism,
                estimated_hours=0.5 + index * 0.5,
                forecast=10.0 - index,
                metadata={
                    "level": "L0",
                    "metric": "relative_quality_delta_pct",
                    "pass_fail": "PASS если delta >= 5% на 3 seeds; FAIL если delta < 2%",
                    "generated": "mission-region-fallback",
                },
            )
            output.append(candidate)
        return output

    async def _evaluate_candidates(
        self,
        region: Region,
        candidates: Sequence[Hypothesis],
    ) -> List[Tuple[Hypothesis, List[Evaluation]]]:
        """Evaluate candidates concurrently with a global evaluator semaphore."""

        semaphore = asyncio.Semaphore(self.config.max_parallel_evaluations)

        async def evaluate_one(
            candidate: Hypothesis,
        ) -> Tuple[Hypothesis, List[Evaluation]]:
            async def invoke(evaluator: Evaluator) -> Evaluation:
                async with semaphore:
                    try:
                        return await evaluator.evaluate(
                            candidate,
                            self.state,
                            EvaluationContext(self.config, self.mcp, self.metrics),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - one evaluator must not kill a run
                        self.metrics.inc("evaluator_errors")
                        self.logger.error(
                            "Evaluator failed",
                            extra=log_extra(
                                event="evaluator_error",
                                region_id=region.id,
                                hypothesis_id=candidate.id,
                                error=str(exc),
                            ),
                        )
                        return Evaluation(
                            evaluator=getattr(evaluator, "name", "unknown"),
                            score=0.0,
                            detail={"error": str(exc)},
                        )

            results = await asyncio.gather(*(invoke(e) for e in self.evaluators))
            return candidate, list(results)

        return list(await asyncio.gather(*(evaluate_one(c) for c in candidates)))

    def _update_state(
        self,
        region: Region,
        evaluated: Sequence[Tuple[Hypothesis, List[Evaluation]]],
    ) -> None:
        priorities: List[float] = []
        for candidate, results in evaluated:
            scores = {result.evaluator: max(0.0, min(1.0, result.score)) for result in results}
            for result in results:
                self.state.cost_usd += max(0.0, float(result.cost_usd))
                for evidence in result.evidence:
                    is_new = evidence.id not in self.state.evidence
                    self.state.evidence[evidence.id] = evidence
                    if evidence.id not in candidate.evidence_ids:
                        candidate.evidence_ids.append(evidence.id)
                    if evidence.source not in candidate.signal_sources:
                        candidate.signal_sources.append(evidence.source)
                    if is_new:
                        append_history(
                            self.conn,
                            self.state,
                            "evidence.attached",
                            run_id=self._run_id,
                            region_id=region.id,
                            hypothesis_id=candidate.id,
                            source=evidence.source,
                            kind=evidence.kind,
                        )
            unique_sources = {
                self.state.evidence[eid].independent_key
                for eid in candidate.evidence_ids
                if eid in self.state.evidence and self.state.evidence[eid].independent_key
            }
            unique_sources.update(item for item in candidate.signal_sources if item)
            evidence_score = min(1.0, len(unique_sources) / 3.0)
            candidate.novelty_score = max(scores.get("novelty", 0.0), scores.get("literature", 0.0))
            candidate.mechanism_score = scores.get("mechanism", 0.0)
            candidate.experiment_score = scores.get("experiment", 0.0)
            candidate.commercial_score = scores.get("commercial", 0.0)
            candidate.decidability_score = max(
                candidate.experiment_score,
                1.0 if any(r.detail.get("has_numeric_gate") for r in results) else 0.0,
            )
            candidate.priority = round(
                PRIORITY_WEIGHTS["evidence"] * evidence_score
                + PRIORITY_WEIGHTS["novelty"] * candidate.novelty_score
                + PRIORITY_WEIGHTS["mechanism"] * candidate.mechanism_score
                + PRIORITY_WEIGHTS["experiment"] * candidate.experiment_score
                + PRIORITY_WEIGHTS["commercial"] * candidate.commercial_score
                + PRIORITY_WEIGHTS["decidability"] * candidate.decidability_score,
                4,
            )
            candidate.status = "evaluated"
            candidate.updated_at = core.iso()
            priorities.append(candidate.priority)
            self.metrics.inc("candidates_evaluated")
            append_history(
                self.conn,
                self.state,
                "hypothesis.evaluated",
                run_id=self._run_id,
                region_id=region.id,
                hypothesis_id=candidate.id,
                priority=candidate.priority,
                evidence=len(candidate.evidence_ids),
            )
        region.signal_score = round(max(priorities) if priorities else 0.0, 4)
        if region.signal_score < self.config.min_signal_score:
            region.no_signal_streak += 1
        else:
            region.no_signal_streak = 0
        region.updated_at = core.iso()
        self.metrics.set("cost_usd", self.state.cost_usd)

    def _decide_action(self, region: Region) -> str:
        if self.state.cost_usd >= self.config.max_cost_usd:
            return "stop"
        candidates = [
            h
            for h in self.state.hypotheses.values()
            if h.region_id == region.id and h.status == "evaluated"
        ]
        best = max(candidates, key=lambda h: h.priority, default=None)
        if region.no_signal_streak >= self.config.region_no_signal_limit:
            return "backtrack" if region.parent_id else "expand"
        if best is None:
            return "expand" if region.depth < self.config.max_region_depth else "backtrack"
        if not region.metadata.get("transformed") and best.priority < 0.75:
            return "transform"
        if best.priority >= self.config.min_signal_score:
            return "refine" if region.depth < self.config.max_region_depth else "close"
        return "expand" if region.depth < self.config.max_region_depth else "backtrack"

    async def _apply_action(self, decision: str, region: Region) -> None:
        append_history(
            self.conn,
            self.state,
            "region.action",
            run_id=self._run_id,
            region_id=region.id,
            action=decision,
        )
        if decision == "stop":
            return
        if decision == "transform":
            await self._transform_region(region)
            return
        if decision == "refine":
            child = self.add_region(
                f"{region.name} / mechanism",
                f"{region.query} — causal mechanism and control",
                parent_id=region.id,
                metadata={"action": "refine"},
            )
            region.status = "exhausted"
            region.metadata["refined_to"] = child.id
            return
        if decision == "expand":
            if region.depth >= self.config.max_region_depth:
                region.status = "closed"
                return
            for suffix in ("early predictor", "negative control"):
                self.add_region(
                    f"{region.name} / {suffix}",
                    f"{region.query} — {suffix}",
                    parent_id=region.id,
                    metadata={"action": "expand", "branch": suffix},
                )
            region.status = "exhausted"
            return
        if decision == "backtrack":
            self._backtrack(region)
            return
        if decision == "close":
            region.status = "closed"
            self._remove_from_frontier(region.id)
            return
        raise SkillError(f"unknown region action: {decision}")

    async def _transform_region(self, region: Region) -> None:
        hypotheses = [
            h
            for h in self.state.hypotheses.values()
            if h.region_id == region.id and h.status == "evaluated"
        ]
        created = 0
        for hypothesis in hypotheses:
            for result in self.transformer.transform(hypothesis):
                for transformed in result.transformed_hypotheses:
                    if transformed.id in self.state.hypotheses:
                        continue
                    self.state.hypotheses[transformed.id] = transformed
                    created += 1
                    append_history(
                        self.conn,
                        self.state,
                        "hypothesis.transformed",
                        run_id=self._run_id,
                        region_id=region.id,
                        hypothesis_id=transformed.id,
                        origin_id=hypothesis.id,
                        transformation_type=result.transformation_type,
                        confidence=result.confidence,
                    )
        region.metadata["transformed"] = True
        region.metadata["transformations_created"] = created
        region.status = "frontier"
        self._mark_frontier(region.id)
        self.metrics.inc("transformations_created", created)
        self._persist()

    def _backtrack(self, region: Region) -> None:
        region.status = "backtracked"
        self._remove_from_frontier(region.id)
        parent = self.state.regions.get(region.parent_id or "")
        if parent is not None:
            parent.status = "frontier"
            parent.updated_at = core.iso()
            self._mark_frontier(parent.id)
        siblings = [
            item
            for item in self.state.regions.values()
            if item.parent_id == region.parent_id
            and item.id != region.id
            and item.status not in ("closed", "backtracked")
        ]
        for sibling in siblings:
            self._mark_frontier(sibling.id)

    def promote(self, hypothesis_id: str) -> Dict[str, Any]:
        """Hand a candidate to the existing queue; never bypass its gate."""

        candidate = self._get_hypothesis(hypothesis_id)
        independent = {
            self.state.evidence[eid].independent_key
            for eid in candidate.evidence_ids
            if eid in self.state.evidence and self.state.evidence[eid].independent_key
        }
        if len(independent) < 3:
            raise ExperimentError(
                f"{hypothesis_id}: promotion requires 3 independent evidence sources"
            )
        if candidate.forecast is None:
            raise ExperimentError(f"{hypothesis_id}: forecast must be fixed before promotion")
        row = q.add(
            self.conn,
            candidate.text,
            signals=len(independent),
            novelty=candidate.novelty_score,
            early_pct=float(candidate.metadata.get("early_pct", 10.0)),
            standard=candidate.metadata.get("standard", 0.4),
            money=candidate.commercial_score,
            decidability=candidate.decidability_score,
            est_hours=candidate.estimated_hours,
            forecast=candidate.forecast,
            source=f"bottom:{self.state.namespace}:{hypothesis_id}",
        )
        path = hypo.write_card(
            row["id"],
            candidate.text,
            signals=len(independent),
            novelty=candidate.novelty_score,
            early_pct=float(candidate.metadata.get("early_pct", 10.0)),
            standard=candidate.metadata.get("standard", 0.4),
            money=candidate.commercial_score,
            decidability=candidate.decidability_score,
            est_hours=candidate.estimated_hours,
            forecast=candidate.forecast,
            source=f"bottom:{self.state.namespace}:{hypothesis_id}",
        )
        q.update_fields(self.conn, row["id"], card_path=path)
        candidate.status = "promoted"
        candidate.metadata["profile_hypothesis_id"] = row["id"]
        candidate.updated_at = core.iso()
        append_history(
            self.conn,
            self.state,
            "hypothesis.promoted",
            run_id=self._run_id,
            hypothesis_id=hypothesis_id,
            profile_hypothesis_id=row["id"],
        )
        self._persist()
        return {"candidate": asdict(candidate), "profile_hypothesis": dict(row), "card": path}

    def stats(self) -> Dict[str, Any]:
        """Return compact state and evaluator metrics for status/cron."""

        region_counts: Dict[str, int] = {}
        for region in self.state.regions.values():
            region_counts[region.status] = region_counts.get(region.status, 0) + 1
        hypothesis_counts: Dict[str, int] = {}
        for hypothesis in self.state.hypotheses.values():
            hypothesis_counts[hypothesis.status] = hypothesis_counts.get(hypothesis.status, 0) + 1
        return {
            "namespace": self.state.namespace,
            "iteration": self.state.iteration,
            "cost_usd": round(self.state.cost_usd, 4),
            "regions": region_counts,
            "hypotheses": hypothesis_counts,
            "evidence": len(self.state.evidence),
            "history_events": len(self.state.history),
            "metrics": self.metrics.snapshot(),
        }

    def _seed_regions(self) -> None:
        terms: List[str] = []
        for raw in self.config.mission.splitlines():
            line = raw.strip().lstrip("-*").strip()
            if not line:
                continue
            if line.startswith("#"):
                line = line.lstrip("# ")
            line = re.sub(r"[`*_]", "", line).strip()
            if 4 <= len(line) <= 110:
                terms.append(line)
        defaults = [
            "early measurable predictors",
            "competition between useful and parasitic circuits",
            "pruning, masking and structural selection",
            "sign stability and gradient dynamics",
            "memorization versus generalization",
            "cross-architecture reproducibility",
            "low-rank extraction and amplification",
            "compute-saving intervention",
        ]
        unique: List[str] = []
        seen = set()
        for term in terms + defaults:
            key = re.sub(r"\s+", " ", term.lower())
            if key not in seen:
                unique.append(term)
                seen.add(key)
        for index, term in enumerate(unique[: self.config.num_initial_regions], start=1):
            region_id = f"R-{index:03d}"
            region = Region(
                id=region_id,
                name=term[:80],
                query=f"{self.config.domain}: {term}",
                metadata={"source": "MISSION.md"},
            )
            self.state.regions[region.id] = region
            self._mark_frontier(region.id)
        append_history(
            self.conn,
            self.state,
            "regions.seeded",
            run_id=self._run_id,
            count=len(self.state.regions),
        )

    def _select_next_region(self) -> Optional[Region]:
        available = [
            region
            for region in self.state.regions.values()
            if region.status in ("frontier", "active")
            and region.visits < self.config.max_iterations
        ]
        if not available:
            available = [
                region
                for region in self.state.regions.values()
                if region.status == "backtracked"
                and region.visits < self.config.max_iterations
            ]
        if not available:
            return None
        return max(
            available,
            key=lambda region: (
                region.signal_score
                + 0.20 / (1 + region.visits)
                - 0.10 * region.no_signal_streak,
                -region.depth,
            ),
        )

    def _should_stop(self) -> bool:
        if self.state.cost_usd >= self.config.max_cost_usd:
            return True
        confirmed = self.conn.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE status IN ('confirmed','partial') "
            "AND source LIKE ?",
            (f"bottom:{self.state.namespace}:%",),
        ).fetchone()[0]
        return int(confirmed) >= self.config.target_confirmed_hypotheses

    def _transport_from_config(self) -> Optional[MCPTransport]:
        if self.config.mcp_endpoints:
            return HTTPMCPTransport(self.config.mcp_endpoints)
        if self.config.mcp_commands:
            command = self.config.mcp_commands.get("*")
            if command is None and self.config.mcp_tools:
                command = self.config.mcp_commands.get(self.config.mcp_tools[0])
            if command:
                return JsonCommandMCPTransport(command)
        return None

    def _finish_run(self, status: str, summary: Optional[Dict[str, Any]] = None) -> None:
        if self._run_id is None:
            return
        self.conn.execute(
            "UPDATE bd_runs SET finished_at=?,iterations=?,cost_usd=?,status=?,summary=? WHERE run_id=?",
            (
                core.iso(),
                self.state.iteration,
                self.state.cost_usd,
                status,
                json.dumps(summary or {}, ensure_ascii=False, default=str),
                self._run_id,
            ),
        )
        self.conn.commit()

    def _finalize(self) -> Dict[str, Any]:
        top = sorted(
            self.state.hypotheses.values(),
            key=lambda item: (-item.priority, item.id),
        )[: self.config.target_confirmed_hypotheses]
        verdicts = {
            item.id: format_verdict(
                item,
                [self.state.evidence[eid] for eid in item.evidence_ids if eid in self.state.evidence],
            )
            for item in top
        }
        return {
            "namespace": self.state.namespace,
            "domain": self.state.domain,
            "iterations": self.state.iteration,
            "cost_usd": round(self.state.cost_usd, 4),
            "regions": [asdict(item) for item in self.state.regions.values()],
            "hypotheses": [asdict(item) for item in self.state.hypotheses.values()],
            "evidence": [asdict(item) for item in self.state.evidence.values()],
            "history": list(self.state.history),
            "top_hypotheses": [asdict(item) for item in top],
            "verdicts": verdicts,
            "metrics": self.metrics.snapshot(),
            "prometheus": self.metrics.prometheus(),
        }

    def _persist(self) -> None:
        persist_state(self.conn, self.state)

    def _get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        candidate = self.state.hypotheses.get(hypothesis_id)
        if candidate is None:
            raise SkillError(f"unknown exploratory hypothesis: {hypothesis_id}")
        return candidate

    def _mark_frontier(self, region_id: str) -> None:
        if region_id not in self.state.frontier:
            self.state.frontier.append(region_id)

    def _remove_from_frontier(self, region_id: str) -> None:
        self.state.frontier = [item for item in self.state.frontier if item != region_id]

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
        return f"{prefix}-{digest}"


__all__ = ["BottomDetectionSkill", "PRIORITY_WEIGHTS"]
