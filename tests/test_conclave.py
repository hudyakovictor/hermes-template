"""Regression tests for adaptive persona review and Telegram transcript."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import conclave  # noqa: E402
import core  # noqa: E402
import governor  # noqa: E402


CONFIG = {
    "researchagen": {
        "platform": "macos",
        "mode": "debug",
        "governor": {
            "enabled": True,
            "max_research_children": 2,
            "max_triage_children": 1,
            "daily_research_task_budget": 12,
            "debug_research_capacity": 2,
            "hermes_cron_control": False,
            "lease_ttl_seconds": 300,
        },
        "conclave": {
            "enabled": True,
            "max_rounds": 2,
            "min_slots_for_debate": 2,
            "trigger_score": 0.45,
            "trigger_confidence_gap": 0.25,
            "nudge_probability": 0.0,
            "nudge_cooldown_seconds": 0,
            "client_comment_probability": 0.35,
            "max_visible_chars": 200,
            "allow_banter": True,
            "allow_profanity": True,
        },
    }
}


class ConclaveDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def telemetry(self, free=24.0, util=30.0):
        return {
            "available": True,
            "debug": False,
            "free_gb": free,
            "best": {"free_gb": free, "util_pct": util, "name": "test-gpu"},
            "gpus": [],
            "source": "test",
        }

    def open_debate(self):
        context = {
            "stage": "critique",
            "source_conflict": True,
            "reports": [
                {"confidence": 0.91, "claims": ["works"]},
                {"confidence": 0.42, "claims": ["does not work"]},
            ],
        }
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            result = conclave.open_session(
                self.conn, "H-001", "Спор H-001", "critique", context, config=CONFIG
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["opened"])
        return result["session_id"]

    def test_trigger_requires_exceptional_case_and_detects_conflict(self):
        quiet = conclave.detect_triggers({"stage": "critique"}, CONFIG)
        self.assertFalse(quiet["required"])
        trigger = conclave.detect_triggers({
            "source_conflict": True,
            "reports": [{"confidence": 0.9}, {"confidence": 0.4}],
        }, CONFIG)
        self.assertTrue(trigger["required"])
        self.assertIn("reports cite conflicting evidence", trigger["reasons"])
        self.assertGreaterEqual(trigger["score"], 0.45)

    def test_role_plan_is_bounded_by_governor_and_does_not_fake_consensus(self):
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            plan = conclave.role_plan(self.conn, "H-001", "critique", {
                "source_conflict": True,
                "reports": [{"confidence": 0.9}, {"confidence": 0.3}],
            }, CONFIG)
        self.assertTrue(plan["debate_possible"])
        self.assertEqual([role["id"] for role in plan["roles"]], ["evidence", "falsifier"])
        self.assertEqual(plan["parent_role"]["id"], "parent")

        one_slot = dict(CONFIG)
        one_slot["researchagen"] = dict(CONFIG["researchagen"])
        one_slot["researchagen"]["governor"] = dict(CONFIG["researchagen"]["governor"])
        one_slot["researchagen"]["governor"]["max_research_children"] = 1
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            fallback = conclave.role_plan(self.conn, "H-002", "critique", {
                "source_conflict": True,
                "reports": [{"confidence": 0.9}, {"confidence": 0.3}],
            }, one_slot)
        self.assertFalse(fallback["debate_possible"])
        self.assertEqual(len(fallback["roles"]), 1)
        self.assertIn("self_review", fallback["fallback"])

    def test_open_is_blocked_during_testing_without_creating_session(self):
        core.set_setting(self.conn, "governor.mode", "testing")
        context = {"source_conflict": True, "force_debate": True}
        result = conclave.open_session(
            self.conn, "H-003", "blocked", "critique", context, config=CONFIG
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["opened"])
        self.assertIn("blocks", result["reason"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM conclave_sessions").fetchone()[0], 0)

    def test_assign_binds_two_fixed_personas_and_leases(self):
        session_id = self.open_debate()
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            result = conclave.assign(self.conn, session_id, config=CONFIG)
        self.assertTrue(result["ok"])
        self.assertTrue(result["debate"])
        self.assertEqual(len(result["assignments"]), 2)
        self.assertEqual({row["role_id"] for row in result["assignments"]}, {"evidence", "falsifier"})
        self.assertTrue(all(row["lease_id"] for row in result["assignments"]))
        self.assertEqual(len(governor.leases(self.conn)), 2)
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            again = conclave.assign(self.conn, session_id, config=CONFIG)
        self.assertTrue(again["ok"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(again["assignments"]), 2)

    def test_brief_separates_english_protocol_from_russian_public_contract(self):
        session_id = self.open_debate()
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            assigned = conclave.assign(self.conn, session_id, config=CONFIG)
        assignment_id = assigned["assignments"][0]["assignment_id"]
        result = conclave.brief(self.conn, session_id, assignment_id, CONFIG)
        self.assertEqual(result["internal_protocol"]["reasoning_language"], "en")
        self.assertIn("Audit", result["internal_protocol"]["text"])
        self.assertEqual(result["public_protocol"]["language"], "ru")
        self.assertIn("POSITION", result["public_protocol"]["format"])
        self.assertEqual(result["report_contract"]["task_id"], assigned["assignments"][0]["task_id"])
        template_ids = {item["id"] for item in result["challenge_templates"]}
        self.assertTrue({"source-audit", "steelman", "falsification"}.issubset(template_ids))

    def test_context_and_templates_are_durable_without_hidden_reasoning(self):
        context = {
            "source_conflict": True,
            "chain_of_thought": "do not persist this",
            "reports": [
                {"confidence": 0.9, "private_reasoning": "also hide"},
                {"confidence": 0.3},
            ],
        }
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            opened = conclave.open_session(
                self.conn, "H-009", "redaction", "critique", context, config=CONFIG
            )
        self.assertTrue(opened["ok"])
        stored = self.conn.execute(
            "SELECT context FROM conclave_sessions WHERE session_id=?", (opened["session_id"],)
        ).fetchone()["context"]
        self.assertNotIn("do not persist", stored)
        self.assertNotIn("also hide", stored)
        self.assertTrue(opened["plan"]["challenge_templates"])

    def test_rounds_are_bounded_by_session_contract(self):
        session_id = self.open_debate()
        with mock.patch.object(conclave.tg, "send", return_value={"ok": True}):
            result = conclave.post_message(
                self.conn, session_id, "слишком поздний раунд", "debate", "rebuttal", 3,
                nudge=False, config=CONFIG,
            )
        self.assertFalse(result["ok"])
        self.assertIn("0..2", result["reason"])

    def test_speak_persists_short_task_and_parallel_client_comment(self):
        session_id = self.open_debate()
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            assigned = conclave.assign(self.conn, session_id, config=CONFIG)
        assignment_id = assigned["assignments"][0]["assignment_id"]
        rng = mock.Mock()
        rng.random.side_effect = [1.0, 0.0]  # no nudge, client comment is due
        with mock.patch.object(conclave, "_RNG", rng), \
             mock.patch.object(conclave.tg, "send", return_value={
                 "ok": True, "result": {"message_id": 101}
             }) as send:
            result = conclave.speak(
                self.conn, session_id, assignment_id,
                "Контроль отсутствует — это пока красивая легенда.",
                "Заказчик просит кнопку бабло; выдаём кнопку проверить.",
                "critique", 1, config=CONFIG,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["client_due"])
        self.assertEqual(send.call_count, 2)
        rows = self.conn.execute(
            "SELECT audience,language,text FROM conclave_messages ORDER BY message_id"
        ).fetchall()
        self.assertEqual([row["audience"] for row in rows], ["task", "client"])
        self.assertTrue(all(row["language"] == "ru" for row in rows))

    def test_public_post_removes_hidden_reasoning_and_limits_length(self):
        session_id = self.open_debate()
        long_text = "<think>private chain</think>" + (" datum " * 100)
        with mock.patch.object(conclave.tg, "send", return_value={"ok": True}):
            result = conclave.post_message(
                self.conn, session_id, long_text, audience="debate", kind="analysis",
                config=CONFIG,
            )
        row = self.conn.execute("SELECT text FROM conclave_messages").fetchone()
        self.assertTrue(result["ok"])
        self.assertNotIn("private chain", row["text"])
        self.assertLessEqual(len(row["text"]), 200)

        with mock.patch.object(conclave.tg, "send", return_value={"ok": True}):
            conclave.post_message(self.conn, session_id, "Ты идиот, это всё равно не сработает.",
                                  audience="debate", kind="critique", nudge=False, config=CONFIG)
        latest = self.conn.execute(
            "SELECT text FROM conclave_messages ORDER BY message_id DESC LIMIT 1"
        ).fetchone()["text"]
        self.assertNotIn("Ты идиот", latest)
        self.assertIn("допущение наивно", latest)

    def test_nudge_is_logged_as_prior_not_as_guaranteed_effectiveness(self):
        session_id = self.open_debate()
        rng = mock.Mock()
        rng.choice.return_value = dict(conclave.PHRASES[0])
        with mock.patch.object(conclave, "_RNG", rng):
            phrase = conclave.choose_nudge(
                self.conn, session_id, context={"source_conflict": True},
                force=True, config=CONFIG,
            )
        self.assertEqual(phrase["prior_effectiveness"], 0.95)
        self.assertEqual(phrase["target_positive_effect"], 0.90)
        stats = conclave.phrase_stats(self.conn)
        self.assertTrue(stats["note"].startswith("95%/90%"))
        self.assertTrue(stats["phrases"][0]["prior_is_not_measurement"])
        outcome = conclave.mark_phrase_outcome(
            self.conn, session_id, phrase["id"], "positive"
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual(conclave.phrase_stats(self.conn)["phrases"][0]["measured_positive_rate"], 1.0)

    def test_report_is_attached_and_released_without_promoting_science(self):
        session_id = self.open_debate()
        with mock.patch.object(governor.gpu, "snapshot", return_value=self.telemetry()):
            assigned = conclave.assign(self.conn, session_id, config=CONFIG)
        assignment = assigned["assignments"][0]
        path = os.path.join(self.tmp.name, "worker.json")
        report = {
            "task_id": assignment["task_id"],
            "status": "completed",
            "claims": [{"claim": "control is missing"}],
            "evidence_refs": ["E-1"],
            "sources": ["https://example.test/source"],
            "confidence": 0.62,
            "duplicate_of": None,
            "recommended_next_action": "parent adds control",
            "changed_files": [],
            "resource_usage": {"qwen_requests": 1},
            "failure_reason": None,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        result = conclave.receive_report(
            self.conn, session_id, assignment["assignment_id"], path, CONFIG
        )
        self.assertTrue(result["valid"])
        self.assertTrue(result["review_pending"])
        self.assertFalse(result["scientific_state_changed"])
        row = self.conn.execute(
            "SELECT state FROM conclave_assignments WHERE assignment_id=?",
            (assignment["assignment_id"],),
        ).fetchone()
        self.assertEqual(row["state"], "reported")
        # Only this child finished; the other debate worker remains admitted
        # until its own report/lease release, so the room cannot fake a quorum.
        self.assertEqual(len(governor.leases(self.conn)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0], 0)

    def test_watch_is_cpu_only_and_throttles_telegram(self):
        session_id = self.open_debate()
        with mock.patch.object(conclave.tg, "send", return_value={"ok": True}) as send:
            first = conclave.watch(self.conn, send=True, config=CONFIG)
            second = conclave.watch(self.conn, send=True, config=CONFIG)
        self.assertTrue(first["ok"])
        self.assertTrue(first["send_due"])
        self.assertFalse(second["send_due"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(first["rooms"][0]["session_id"], session_id)

    def test_transcript_is_public_only_and_close_is_not_verdict(self):
        session_id = self.open_debate()
        with mock.patch.object(conclave.tg, "send", return_value={"ok": True}):
            conclave.post_message(self.conn, session_id, "Факт: нужен контроль.", "task", "claim", config=CONFIG)
        data = conclave.transcript(self.conn, session_id)
        self.assertIn("Факт: нужен контроль.", data["text"])
        self.assertNotIn("internal_protocol", data["text"])
        closed = conclave.close_session(
            self.conn, session_id, "Сначала L0 control", config=CONFIG
        )
        self.assertTrue(closed["ok"])
        self.assertFalse(closed["scientific_state_changed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
