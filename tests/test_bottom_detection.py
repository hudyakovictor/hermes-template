"""Tests for the optional hybrid Bottom Detection layer."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from typing import Any, List
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import core  # noqa: E402
import inbox  # noqa: E402
import bottom_study  # noqa: E402
from bottom_detection import (  # noqa: E402
    BottomDetectionSkill,
    CallableMCPTransport,
    ExperimentError,
    Hypothesis,
    MCPError,
    SkillError,
    SkillConfig,
    TransformationSkill,
    format_verdict,
)
from bottom_detection.evaluators import (  # noqa: E402
    CommercialEvaluator,
    Evaluation,
    EvaluationContext,
    ExperimentEvaluator,
    LiteratureMCPEvaluator,
    MechanismEvaluator,
    NoveltyEvaluator,
)
from bottom_detection.mcp import (  # noqa: E402
    HTTPMCPTransport,
    JsonCommandMCPTransport,
    MCPClient,
    RateLimiter,
    TTLCache,
    _normalise_results,
    retry_async,
)
from bottom_detection.metrics import Metrics  # noqa: E402
from bottom_detection.state import (  # noqa: E402
    Evidence,
    Region,
    SearchState,
    append_history,
    load_state,
    namespace_for,
    persist_state,
)


class TempDb(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()


class TestConfigStateMetrics(TempDb):
    def test_config_clamps_bounds_and_reads_mission_file(self) -> None:
        with self.assertRaises(ValueError):
            SkillConfig("", "test")
        with self.assertRaises(ValueError):
            SkillConfig("mission", "")
        config = SkillConfig(
            "mission",
            "test",
            max_parallel_evaluations=99,
            max_iterations=0,
            max_cost_usd=-1,
            min_signal_score=3,
            retry_attempts=0,
        )
        self.assertEqual(config.max_parallel_evaluations, 10)
        self.assertEqual(config.max_iterations, 1)
        self.assertEqual(config.max_cost_usd, 0.0)
        self.assertEqual(config.min_signal_score, 1.0)
        self.assertEqual(config.retry_attempts, 1)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as fh:
            fh.write("# profile mission")
            mission_path = fh.name
        try:
            loaded = SkillConfig.from_profile(mission_path=mission_path, domain="custom")
            self.assertEqual(loaded.mission, "# profile mission")
            self.assertEqual(loaded.domain, "custom")
            self.assertIn("github", loaded.mcp_tools)
        finally:
            os.remove(mission_path)

    def test_state_roundtrip_history_and_malformed_json(self) -> None:
        mission = "mission"
        domain = "domain"
        self.assertNotEqual(namespace_for(mission, domain), namespace_for(mission, "other"))
        state = SearchState(mission, domain, namespace_for(mission, domain))
        region = Region("R", "region", "query")
        state.regions[region.id] = region
        state.frontier.append(region.id)
        from bottom_detection.state import _json_loads

        self.assertEqual(_json_loads("{bad", {"fallback": True}), {"fallback": True})
        state.hypotheses["H"] = Hypothesis(
            id="H", region_id="R", text="candidate", metadata={"metric": "delta"}
        )
        state.evidence["E"] = Evidence(
            id="E", candidate_id="H", source="paper", claim="claim", independent_key="lab"
        )
        state.hypotheses["H"].evidence_ids.append("E")
        persist_state(self.conn, state)
        append_history(self.conn, state, "test.event", payload_value="ok")
        reopened = load_state(self.conn, mission, domain)
        self.assertIn("R", reopened.regions)
        self.assertIn("H", reopened.hypotheses)
        self.assertEqual(reopened.hypotheses["H"].metadata["metric"], "delta")
        self.assertEqual(reopened.evidence["E"].independent_key, "lab")
        self.assertEqual(reopened.history[-1]["event"], "test.event")
        self.assertEqual(reopened.to_summary()["history_events"], 1)

    def test_metrics_snapshot_and_prometheus_escape_labels(self) -> None:
        metrics = Metrics()
        metrics.inc("requests", 2)
        metrics.set("cost_usd", 1.25)
        self.assertEqual(metrics.snapshot(), {"counters": {"requests": 2}, "gauges": {"cost_usd": 1.25}})
        self.assertIn("researchagen_bottom_detection_requests 2", metrics.prometheus())
        self.assertIn("researchagen_bottom_detection_cost_usd 1.25", metrics.prometheus())


class TestInboxBridge(TempDb):
    def test_take_uses_the_existing_hypothesis_queue(self) -> None:
        old_inbox = inbox.INBOX_PATH
        old_hypo_dir = core.HYPO_DIR
        inbox.INBOX_PATH = os.path.join(self.tmp.name, "nested", "inbox.jsonl")
        core.HYPO_DIR = os.path.join(self.tmp.name, "hypotheses")
        original_db = core.db
        db_path = os.path.join(self.tmp.name, "state.sqlite3")
        try:
            with mock.patch.object(core, "db", side_effect=lambda: original_db(db_path)):
                item = inbox.add("lead text", "human")
                result = inbox.take(
                    item["id"],
                    {"title": "Lead title", "signals": 3, "forecast": 10, "est_hours": 1},
                )
            self.assertEqual(result["source"], f"inbox:{item['id']}")
            self.assertTrue(os.path.exists(result["card_path"]))
            self.assertEqual(inbox._load()[0]["state"], "promoted")
        finally:
            inbox.INBOX_PATH = old_inbox
            core.HYPO_DIR = old_hypo_dir


class TestTransformations(unittest.TestCase):
    def test_all_three_transformation_families_keep_provenance(self) -> None:
        original = Hypothesis(
            id="H-test",
            region_id="R-001",
            text="gradient activation sparsity neuron pruning improves generalization",
            mechanism="a causal mechanism",
        )
        transformer = TransformationSkill({"max_transformations_per_hypothesis": 20})
        results = transformer.transform(original)
        kinds = {result.transformation_type for result in results}
        self.assertIn("synonym", kinds)
        self.assertIn("related_concept", kinds)
        self.assertIn("cross_domain", kinds)
        self.assertTrue(any(result.transformed_hypotheses[0].origin_id == original.id for result in results))
        self.assertTrue(all(result.transformed_hypotheses[0].priority == 0.0 for result in results))

    def test_external_dictionary_is_optional(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            fh.write('{"synonyms": {"gradient": ["jacobian"]}}')
            path = fh.name
        try:
            original = Hypothesis(id="H", region_id="R", text="gradient signal")
            results = TransformationSkill(
                SkillConfig("mission", "domain", transformation_dictionary=path)
            ).transform(original)
            self.assertTrue(any("jacobian" in item.transformed_hypotheses[0].text for item in results))
        finally:
            os.remove(path)


class TestEvaluators(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))
        self.config = SkillConfig("mission", "test")
        self.client = MCPClient(self.conn, "ns", ["arxiv"], transport=None)
        self.context = EvaluationContext(self.config, self.client, Metrics())
        self.state = SearchState("mission", "test", "ns")

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    async def test_builtin_evaluators_cover_positive_and_negative_paths(self) -> None:
        weak = Hypothesis(id="weak", region_id="R", text="plain", mechanism="short")
        strong = Hypothesis(
            id="strong",
            region_id="R",
            text="compute cost and latency",
            mechanism=(
                "A causal mechanism because the control ablation changes the metric "
                "by 12 percent."
            ),
            estimated_hours=0.25,
            metadata={"metric": "quality", "commercial_score": 1.2, "level": "L1"},
            signal_sources=["lab-a", "lab-b"],
        )
        weak_mechanism = await MechanismEvaluator().evaluate(weak, self.state, self.context)
        strong_mechanism = await MechanismEvaluator().evaluate(strong, self.state, self.context)
        self.assertLess(weak_mechanism.score, strong_mechanism.score)
        cheap = await ExperimentEvaluator().evaluate(strong, self.state, self.context)
        self.assertGreater(cheap.score, 0.7)
        expensive = Hypothesis(id="exp", region_id="R", text="x", estimated_hours=99)
        expensive_eval = await ExperimentEvaluator().evaluate(expensive, self.state, self.context)
        self.assertLess(expensive_eval.score, cheap.score)
        commercial = await CommercialEvaluator().evaluate(strong, self.state, self.context)
        self.assertEqual(commercial.score, 1.0)
        self.state.evidence["E"] = Evidence(
            id="E", candidate_id="strong", source="paper", claim="claim", independent_key="lab-c"
        )
        strong.evidence_ids = ["E"]
        novelty = await NoveltyEvaluator().evaluate(strong, self.state, self.context)
        self.assertEqual(novelty.detail["independent_sources"], 3)
        literature = await LiteratureMCPEvaluator().evaluate(weak, self.state, self.context)
        self.assertEqual(literature.score, 0.0)

    async def test_literature_normalises_results_and_keeps_provenance(self) -> None:
        async def search(_tool: str, _query: str) -> List[dict[str, Any]]:
            return [
                {"url": "https://paper", "title": "claim", "authors": "lab", "strength": 2},
                "plain result",
            ]

        self.client.transport = CallableMCPTransport(search)
        hypothesis = Hypothesis(id="H", region_id="R", text="candidate")
        result = await LiteratureMCPEvaluator().evaluate(hypothesis, self.state, self.context)
        self.assertEqual(len(result.evidence), 2)
        self.assertEqual(result.evidence[0].source, "https://paper")
        self.assertEqual(result.evidence[0].strength, 1.0)
        self.assertEqual(result.evidence[1].claim, "plain result")


class TestMCP(unittest.IsolatedAsyncioTestCase):
    async def test_retry_and_ttl_cache(self) -> None:
        calls = {"n": 0}

        async def search(tool: str, query: str) -> List[dict[str, Any]]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return [{"source": tool, "claim": query, "independent_key": tool}]

        tmp = tempfile.TemporaryDirectory()
        try:
            conn = core.db(os.path.join(tmp.name, "state.sqlite3"))
            client = MCPClient(
                conn,
                "ns",
                ["arxiv"],
                rate_limit=100,
                cache_ttl_hours=24,
                retry_attempts=2,
                retry_base_seconds=0,
                transport=CallableMCPTransport(search),
            )
            first = await client.search_all("query")
            second = await client.search_all("query")
            self.assertEqual(len(first), 1)
            self.assertEqual(first, second)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(client.metrics.counters["mcp_cache_hits"], 1)
            conn.close()
        finally:
            tmp.cleanup()


class TestMCPAdapters(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))

    async def asyncTearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    async def test_json_command_transport_and_normalisation(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import json; request=json.load(__import__('sys').stdin); print(json.dumps({'results': [{'claim': request['tool']}] }))",
        ]
        transport = JsonCommandMCPTransport(command, timeout_seconds=2)
        result = await transport.search("arxiv", "query")
        self.assertEqual(result[0]["claim"], "arxiv")
        self.assertEqual(_normalise_results({"data": ["x"]})[0]["claim"], "x")
        self.assertEqual(_normalise_results({"other": []}), [])
        self.assertEqual(_normalise_results("not a list"), [])
        self.assertEqual(await HTTPMCPTransport({}).search("arxiv", "query"), [])

    async def test_mcp_cache_expiry_and_retry_failure(self) -> None:
        cache = TTLCache(self.conn, "ns", ttl_hours=24)
        cache.set("key", {"value": 1})
        self.assertEqual(cache.get("key"), {"value": 1})
        expired = core.iso(core.now() - timedelta(seconds=1))
        self.conn.execute(
            "UPDATE bd_cache SET expires_at=? WHERE namespace=? AND cache_key=?",
            (expired, "ns", "key"),
        )
        self.conn.commit()
        self.assertIsNone(cache.get("key"))
        self.conn.execute(
            "INSERT INTO bd_cache(namespace,cache_key,payload,expires_at) VALUES (?,?,?,?)",
            ("ns", "bad", "{bad", core.iso(core.now() + timedelta(hours=1))),
        )
        self.conn.commit()
        self.assertIsNone(cache.get("bad"))

        async def fail() -> None:
            raise RuntimeError("permanent")

        with self.assertRaises(MCPError):
            await retry_async(fail, attempts=2, base_seconds=0)

    async def test_rate_limiter_can_acquire_within_budget(self) -> None:
        limiter = RateLimiter(2)
        await limiter.acquire()
        await limiter.acquire()
        self.assertEqual(len(limiter.calls), 2)


class SlowEvaluator:
    name = "slow"

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def evaluate(self, hypothesis: Hypothesis, state: Any, context: Any) -> Evaluation:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.005)
        self.active -= 1
        return Evaluation(self.name, 0.5)


class TestSkill(TempDb, unittest.IsolatedAsyncioTestCase):
    def make_skill(self, **kwargs: Any) -> BottomDetectionSkill:
        values = {
            "mission": "# Mission\n- early structure\n- pruning",
            "domain": "test",
            "num_initial_regions": 2,
            "max_iterations": 2,
            "max_parallel_evaluations": 2,
            "max_cost_usd": 10,
        }
        values.update(kwargs)
        return BottomDetectionSkill(SkillConfig(**values), conn=self.conn)

    async def test_state_and_history_survive_reopen(self) -> None:
        skill = self.make_skill()
        region = next(iter(skill.state.regions.values()))
        candidate = skill.add_candidate(region.id, "gradient pruning", mechanism="causal mechanism")
        self.assertIn(candidate.id, skill.state.hypotheses)
        reopened = self.make_skill()
        self.assertIn(region.id, reopened.state.regions)
        self.assertIn(candidate.id, reopened.state.hypotheses)
        self.assertGreaterEqual(len(reopened.state.history), 2)

    async def test_evaluator_concurrency_is_capped(self) -> None:
        skill = self.make_skill(max_parallel_evaluations=2)
        slow = SlowEvaluator()
        skill.evaluators = [slow]
        region = next(iter(skill.state.regions.values()))
        candidates = [
            skill.add_candidate(region.id, f"candidate {i}") for i in range(6)
        ]
        await skill._evaluate_candidates(region, candidates)
        self.assertLessEqual(slow.peak, 2)

    async def test_run_records_regions_candidates_and_prometheus(self) -> None:
        skill = self.make_skill(num_initial_regions=1, max_iterations=1)
        result = await skill.run()
        self.assertEqual(result["iterations"], 1)
        self.assertTrue(result["regions"])
        self.assertTrue(result["hypotheses"])
        self.assertIn("SIGNAL", next(iter(result["verdicts"].values())))
        self.assertIn("researchagen_bottom_detection_", result["prometheus"])

    async def test_backtracking_returns_parent_to_frontier(self) -> None:
        skill = self.make_skill()
        parent = next(iter(skill.state.regions.values()))
        child = skill.add_region("child", "child query", parent_id=parent.id)
        skill._backtrack(child)
        self.assertEqual(child.status, "backtracked")
        self.assertEqual(parent.status, "frontier")
        self.assertIn(parent.id, skill.state.frontier)

    async def test_promotion_requires_three_independent_sources(self) -> None:
        skill = self.make_skill()
        region = next(iter(skill.state.regions.values()))
        candidate = skill.add_candidate(region.id, "gradient pruning", forecast=10)
        for index in range(2):
            skill.add_evidence(candidate.id, f"source-{index}", "claim", independent_key=f"lab-{index}")
        with self.assertRaises(ExperimentError):
            skill.promote(candidate.id)

    async def test_candidate_evidence_validation_and_action_branches(self) -> None:
        skill = self.make_skill(max_region_depth=1)
        region = next(iter(skill.state.regions.values()))
        with self.assertRaises(SkillError):
            skill.add_candidate("missing", "candidate")
        with self.assertRaises(SkillError):
            skill.add_candidate(region.id, "")
        candidate = skill.add_candidate(region.id, "gradient pruning")
        self.assertIs(skill.add_candidate(region.id, "gradient pruning"), candidate)
        with self.assertRaises(SkillError):
            skill.add_evidence(candidate.id, "", "claim")
        await skill._apply_action("transform", region)
        self.assertTrue(region.metadata["transformed"])
        await skill._apply_action("close", region)
        self.assertEqual(region.status, "closed")
        with self.assertRaises(SkillError):
            await skill._apply_action("unknown", region)

        expand_skill = self.make_skill(num_initial_regions=1, max_region_depth=1)
        expand_region = next(iter(expand_skill.state.regions.values()))
        await expand_skill._apply_action("expand", expand_region)
        self.assertEqual(expand_region.status, "exhausted")
        self.assertGreaterEqual(len(expand_skill.state.regions), 3)
        refine_skill = self.make_skill(num_initial_regions=1, max_region_depth=2)
        refine_region = next(iter(refine_skill.state.regions.values()))
        await refine_skill._apply_action("refine", refine_region)
        self.assertEqual(refine_region.status, "exhausted")
        self.assertTrue(refine_region.metadata["refined_to"])
        await refine_skill._apply_action("stop", refine_region)

    async def test_run_failure_is_recorded_and_transport_selection_is_safe(self) -> None:
        skill = self.make_skill(num_initial_regions=1, max_iterations=1)

        async def boom(_region: Region) -> List[Hypothesis]:
            raise RuntimeError("generator failed")

        skill._generate_candidates = boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await skill.run()
        status = self.conn.execute(
            "SELECT status FROM bd_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()["status"]
        self.assertEqual(status, "failed")

        class BudgetEvaluator:
            name = "budget"

            async def evaluate(
                self, hypothesis: Hypothesis, state: SearchState, context: EvaluationContext
            ) -> Evaluation:
                return Evaluation(self.name, 0.1, cost_usd=2.0)

        budget_skill = self.make_skill(num_initial_regions=1, max_cost_usd=0.5)
        budget_skill.evaluators = []
        budget_skill.register_evaluator(BudgetEvaluator())
        budget_result = await budget_skill.run()
        self.assertGreaterEqual(budget_result["cost_usd"], 2.0)
        endpoint_skill = BottomDetectionSkill(
            SkillConfig("endpoint mission", "test", mcp_endpoints={"*": "http://127.0.0.1"}),
            conn=self.conn,
        )
        self.assertIsInstance(endpoint_skill.mcp.transport, HTTPMCPTransport)
        endpoint_skill.close()
        command_skill = BottomDetectionSkill(
            SkillConfig("command mission", "test", mcp_commands={"*": [sys.executable, "-c", ""]}),
            conn=self.conn,
        )
        self.assertIsInstance(command_skill.mcp.transport, JsonCommandMCPTransport)
        command_skill.close()

    async def test_promotion_creates_the_normal_queue_card(self) -> None:
        skill = self.make_skill(num_initial_regions=1)
        region = next(iter(skill.state.regions.values()))
        candidate = skill.add_candidate(
            region.id,
            "gradient pruning lowers compute cost",
            mechanism="causal mechanism",
            forecast=10,
            metadata={"metric": "delta", "pass_fail": "PASS", "level": "L1"},
        )
        for index in range(3):
            skill.add_evidence(
                candidate.id,
                f"source-{index}",
                "independent claim",
                independent_key=f"lab-{index}",
            )
        old_hypo_dir = core.HYPO_DIR
        core.HYPO_DIR = os.path.join(self.tmp.name, "hypotheses")
        try:
            promoted = skill.promote(candidate.id)
            self.assertEqual(promoted["candidate"]["status"], "promoted")
            self.assertTrue(os.path.exists(promoted["card"]))
            self.assertTrue(promoted["profile_hypothesis"]["id"].startswith("H-"))
        finally:
            core.HYPO_DIR = old_hypo_dir


class TestFinalizer(unittest.TestCase):
    def test_contract_headers_are_stable(self) -> None:
        hypothesis = Hypothesis(id="H", region_id="R", text="test")
        text = format_verdict(hypothesis)
        for heading in ("SIGNAL", "HYPOTHESIS", "EXPERIMENT PLAN", "VERDICT"):
            self.assertIn(heading, text)


class TestStudy(unittest.TestCase):
    def test_study_has_requested_sample_and_hybrid_wins(self) -> None:
        result = bottom_study.simulate(150)
        self.assertEqual(result["runs"], 150)
        self.assertEqual(result["recommended"], "hybrid")
        self.assertGreaterEqual(result["variants"]["hybrid"]["pass_rate"], 0.95)
