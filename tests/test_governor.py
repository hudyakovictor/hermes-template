"""Deterministic tests for the Hermes research/resource governor."""

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import core  # noqa: E402
import governor  # noqa: E402
import governor_benchmark  # noqa: E402


DEBUG_CONFIG = {
    "researchagen": {
        "platform": "macos",
        "mode": "debug",
        "governor": {
            "enabled": True,
            "max_research_children": 2,
            "max_triage_children": 1,
            "daily_research_task_budget": 12,
            "debug_research_capacity": 1,
            "hermes_cron_control": False,
            "lease_ttl_seconds": 300,
        },
    }
}


def telemetry(free=24.0, util=35.0):
    return {
        "available": True,
        "debug": False,
        "free_gb": free,
        "best": {"free_gb": free, "util_pct": util, "name": "test-gpu"},
        "gpus": [],
        "source": "test",
    }


class GovernorDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_benchmark_recommendation_requires_latency_and_zero_errors(self):
        levels = [
            {"concurrency": 1, "errors": 0, "latency_ms": {"p95": 100}},
            {"concurrency": 2, "errors": 0, "latency_ms": {"p95": 120}},
            {"concurrency": 3, "errors": 0, "latency_ms": {"p95": 140}},
        ]
        config = {"researchagen": {"governor": {"max_research_children": 2}}}
        recommendation = governor_benchmark._recommend(levels, config)
        self.assertEqual(recommendation["recommended_max_concurrency"], 2)
        levels[1]["errors"] = 1
        self.assertEqual(
            governor_benchmark._recommend(levels, config)["recommended_max_concurrency"], 1
        )

    def test_debug_capacity_is_bounded_and_task_count_is_respected(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            plan = governor.plan(self.conn, DEBUG_CONFIG, task_count=5)
        self.assertEqual(plan["capacity"], 1)
        self.assertEqual(plan["available_slots"], 1)
        self.assertTrue(plan["can_spawn"])

    def test_live_benchmark_cap_can_only_tighten_configured_cap(self):
        config = {"researchagen": {"governor": {
            "enabled": True, "max_research_children": 2,
            "daily_research_task_budget": 12, "hermes_cron_control": False,
        }}}
        core.set_setting(self.conn, "governor.measured_max_concurrency", 1)
        with mock.patch.object(governor.gpu, "snapshot", return_value=telemetry(24, 30)):
            plan = governor.plan(self.conn, config)
        self.assertEqual(plan["configured_cap_raw"], 2)
        self.assertEqual(plan["configured_cap"], 1)
        self.assertEqual(plan["measured_cap"], 1)

    def test_ready_testing_candidate_closes_exploratory_admission(self):
        candidate = {"id": "H-001", "title": "ready", "ppi": 0.5, "bin": "P1"}
        config = {"researchagen": {"governor": {
            "enabled": True, "max_research_children": 2,
            "daily_research_task_budget": 12, "hermes_cron_control": False,
        }}}
        with mock.patch.object(governor, "_ready_experiment_candidate", return_value=candidate), \
             mock.patch.object(governor.gpu, "snapshot", return_value={
                 "available": False, "debug": True, "gpus": [], "free_gb": 0,
             }):
            plan = governor.plan(self.conn, config)
        self.assertEqual(plan["capacity"], 0)
        self.assertEqual(plan["testing_candidate"]["id"], "H-001")
        self.assertIn("testing priority", " ".join(plan["reasons"]))

    def test_healthy_degraded_and_saturated_gpu_paths(self):
        config = {"researchagen": {"governor": {
            "enabled": True, "max_research_children": 2,
            "research_worker_vram_gb": 4, "experiment_reserve_gb": 8,
            "min_free_vram_gb": 4, "low_free_vram_gb": 6,
            "critical_free_vram_gb": 2, "high_utilization_pct": 85,
            "saturated_utilization_pct": 95, "hermes_cron_control": False,
        }}}
        with mock.patch.object(governor.gpu, "snapshot", return_value=telemetry(24, 30)):
            healthy = governor.plan(self.conn, config)
        self.assertEqual(healthy["capacity"], 2)

        with mock.patch.object(governor.gpu, "snapshot", return_value=telemetry(12, 90)):
            degraded = governor.plan(self.conn, config)
        self.assertEqual(degraded["capacity"], 1)

        with mock.patch.object(governor.gpu, "snapshot", return_value=telemetry(12, 98)):
            saturated = governor.plan(self.conn, config)
        self.assertEqual(saturated["capacity"], 0)
        self.assertFalse(saturated["can_spawn"])

    def test_production_without_telemetry_fails_closed(self):
        config = {"researchagen": {"platform": "linux", "mode": "production",
                                   "governor": {"enabled": True,
                                                 "hermes_cron_control": False}}}
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": False, "gpus": [], "free_gb": 0,
        }):
            plan = governor.plan(self.conn, config)
        self.assertEqual(plan["capacity"], 0)
        self.assertIn("fail-closed", " ".join(plan["reasons"]))

    def test_two_research_leases_fit_in_two_slot_plan(self):
        config = {"researchagen": {"governor": {
            "enabled": True, "max_research_children": 2,
            "research_worker_vram_gb": 4, "experiment_reserve_gb": 8,
            "daily_research_task_budget": 12, "hermes_cron_control": False,
        }}}
        with mock.patch.object(governor.gpu, "snapshot", return_value=telemetry(24, 30)):
            one = governor.acquire_research(self.conn, "worker-1", "task-1", config=config)
            two = governor.acquire_research(self.conn, "worker-2", "task-2", config=config)
        self.assertTrue(one["ok"])
        self.assertTrue(two["ok"])
        self.assertEqual(governor.plan(self.conn, config)["available_slots"], 0)

    def test_research_reservation_is_atomic_and_idempotent(self):
        config = DEBUG_CONFIG
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            first = governor.acquire_research(self.conn, "worker-1", "task-1", config=config)
            second = governor.acquire_research(self.conn, "worker-1", "task-1", config=config)
            denied = governor.acquire_research(self.conn, "worker-2", "task-2", config=config)
        self.assertTrue(first["ok"])
        self.assertEqual(first["lease_id"], second["lease_id"])
        self.assertTrue(second["idempotent"])
        self.assertFalse(denied["ok"])
        self.assertEqual(len(governor.leases(self.conn)), 1)

    def test_testing_requests_pause_and_experiment_waits_for_checkpoint(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            lease = governor.acquire_research(self.conn, "worker-1", "task-1", config=DEBUG_CONFIG)
            transition = governor.set_mode(self.conn, "testing", DEBUG_CONFIG)
            self.assertFalse(transition["ready_for_testing"])
            self.assertEqual(transition["pause_requested"], [lease["lease_id"]])
            blocked = governor.acquire_experiment(self.conn, "H-001", "L0", DEBUG_CONFIG)
            self.assertFalse(blocked["ok"])
            self.assertIn("active", blocked["reason"])

            checkpoint = governor.checkpoint(self.conn, lease["lease_id"], "reports/task-1.json")
            self.assertTrue(checkpoint["ok"])
            admitted = governor.acquire_experiment(self.conn, "H-001", "L0", DEBUG_CONFIG)
        self.assertTrue(admitted["ok"])
        self.assertEqual(admitted["mode"], "testing")
        self.assertEqual(governor.plan(self.conn, DEBUG_CONFIG)["capacity"], 0)

    def test_finish_enters_analyze_and_verdict_returns_to_discover(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            admitted = governor.acquire_experiment(self.conn, "H-001", "L0", DEBUG_CONFIG)
            self.assertTrue(admitted["ok"])
            finished = governor.finish_experiment(self.conn, "H-001", DEBUG_CONFIG)
            self.assertEqual(finished["transition"]["mode"], "analyze")
            self.assertEqual(governor.plan(self.conn, DEBUG_CONFIG)["mode"], "analyze")
            resumed = governor.complete_analysis(self.conn, DEBUG_CONFIG)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["mode"], "discover")
        self.assertEqual(governor.plan(self.conn, DEBUG_CONFIG)["mode"], "discover")

    def test_pause_checkpoint_resume_and_stop_semantics(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            lease = governor.acquire_research(self.conn, "worker-1", "task-1", config=DEBUG_CONFIG)
            self.assertTrue(governor.request_stop(self.conn, lease["lease_id"])["ok"])
            self.assertEqual(
                governor.confirm_stop(self.conn, lease["lease_id"])["state"], "stopped"
            )
            self.assertFalse(governor.resume(self.conn, lease["lease_id"], DEBUG_CONFIG)["ok"])

            lease2 = governor.acquire_research(self.conn, "worker-2", "task-2", config=DEBUG_CONFIG)
            self.assertTrue(governor.request_pause(self.conn, lease2["lease_id"])["ok"])
            self.assertEqual(
                governor.checkpoint(self.conn, lease2["lease_id"], "ckpt.json")["state"], "paused"
            )
            resumed = governor.resume(self.conn, lease2["lease_id"], DEBUG_CONFIG)
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["state"], "active")

    def test_expired_research_lease_remains_a_safety_blocker(self):
        config = dict(DEBUG_CONFIG)
        config["researchagen"] = dict(DEBUG_CONFIG["researchagen"])
        config["researchagen"]["governor"] = dict(DEBUG_CONFIG["researchagen"]["governor"])
        config["researchagen"]["governor"]["lease_ttl_seconds"] = 30
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            lease = governor.acquire_research(self.conn, "worker-1", "task-1", config=config)
        self.conn.execute(
            "UPDATE governor_leases SET expires_at=? WHERE lease_id=?",
            (core.iso(core.now() - timedelta(seconds=1)), lease["lease_id"]),
        )
        self.conn.commit()
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            plan = governor.plan(self.conn, config)
            blocked = governor.acquire_experiment(self.conn, "H-001", "L0", config)
        self.assertEqual(plan["active_research"], 1)
        self.assertFalse(blocked["ok"])
        self.assertTrue(governor.confirm_stop(self.conn, lease["lease_id"])["ok"])

    def test_worker_stop_cannot_interrupt_experiment_lease(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value={
            "available": False, "debug": True, "gpus": [], "free_gb": 0,
        }):
            experiment = governor.acquire_experiment(self.conn, "H-001", "L0", DEBUG_CONFIG)
        self.assertTrue(experiment["ok"])
        stopped = governor.request_stop(self.conn, experiment["lease_id"])
        self.assertFalse(stopped["ok"])
        self.assertIn("dispatch", stopped["reason"])

    def test_report_validation_is_not_scientific_promotion(self):
        report = {
            "task_id": "task-1", "status": "completed",
            "claims": [{"claim": "x", "evidence_refs": ["E-1"]}],
            "evidence_refs": ["E-1"], "sources": ["https://example.test/paper"],
            "confidence": 0.7, "duplicate_of": None,
            "recommended_next_action": "parent verifies source",
            "changed_files": [], "resource_usage": {"qwen_requests": 1},
            "failure_reason": None,
        }
        invalid = governor.validate_report(dict(report, confidence=2))
        self.assertFalse(invalid["valid"])
        self.assertFalse(invalid["scientific_state_changed"])
        self.assertTrue(governor.validate_report(report)["valid"])
        path = os.path.join(self.tmp.name, "report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        result = governor.record_report(self.conn, path, "worker-1")
        self.assertTrue(result["valid"])
        self.assertTrue(result["review_pending"])
        self.assertFalse(result["scientific_state_changed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT accepted FROM governor_reports").fetchone()[0], 1)

    def test_failed_report_requires_failure_reason(self):
        base = {
            "task_id": "task-1", "status": "failed", "claims": [],
            "evidence_refs": [], "sources": [], "confidence": 0,
            "duplicate_of": None, "recommended_next_action": "retry",
            "changed_files": [], "resource_usage": {},
        }
        self.assertFalse(governor.validate_report(base)["valid"])
        self.assertTrue(governor.validate_report(dict(base, failure_reason="timeout"))["valid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
