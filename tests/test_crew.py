"""Deterministic tests for the crew chat engine («Курилка»).

No network, no GPU, no model calls: scenes are templates, the RNG is seeded,
tg.send is mocked. The crew must never break the contour.
"""

import os
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import core  # noqa: E402
import crew  # noqa: E402


CREW_CONFIG = {
    "researchagen": {
        "platform": "macos",
        "mode": "debug",
        "crew": {
            "enabled": True,
            "max_messages_per_day": 30,
            "max_lines_per_event": 4,
            "dispute_probability": 1.0,     # споры — всегда, для теста
            "nudge_probability": 0.0,       # нуджи — никогда, для детерминизма
            "quiet_hours": "",
            "agi_arrival": "2030-05-01",
        },
    }
}


class CrewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))
        self.rng = random.Random(42)
        # отправка выключена: тесты не ходят в сеть
        patcher = mock.patch.object(crew.tg, "send", return_value={"ok": True})
        self.tg_send = patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TELEGRAM_CREW_THREAD_ID", None)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def emit(self, event, ctx=None, **kw):
        return crew.emit(event, ctx, conn=self.conn, config=CREW_CONFIG,
                         rng=self.rng, **kw)


class TestScenes(CrewBase):
    def test_known_event_renders_lines(self):
        res = self.emit("verdict_rejected", {"hid": "H-001", "forecast": 12,
                                             "actual": -4.9, "dev": "-141%",
                                             "hours": "0.4", "seeds": "0/3"},
                        force=True)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["lines"]), 3)
        for line in res["lines"]:
            self.assertIn(line["agent"], crew.AGENTS)
            self.assertTrue(line["text"].strip())

    def test_unknown_event_is_a_noop(self):
        res = self.emit("martian_sunrise", force=True)
        self.assertFalse(res["ok"])

    def test_dispute_included_when_probability_is_one(self):
        res = self.emit("hypo_new", {"hid": "H-002", "forecast": 8}, force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(any(l.get("dispute_id") for l in res["lines"]))
        # арбитраж всегда закрывает спор, и всегда Шеф
        dispute_lines = [l for l in res["lines"] if l.get("dispute_id")]
        self.assertEqual(dispute_lines[-1]["agent"], "shef")

    def test_dispute_arbiter_ends_with_numbers(self):
        res = self.emit("gate_pass", {"hid": "H-003", "passed": 7, "forecast": 9},
                        force=True)
        arbiters = [l for l in res["lines"] if l.get("arbiter")]
        self.assertTrue(arbiters)
        self.assertIn("7/7", arbiters[-1]["text"])

    def test_scene_respects_max_lines(self):
        res = self.emit("customer_lead", {"forecast": 12}, force=True)
        # реплик самой сцены — не больше лимита (спор/AGI/нудж считаются отдельно)
        scene = [l for l in res["lines"] if l.get("event") == "customer_lead"]
        self.assertLessEqual(len(scene), 4)

    def test_missing_ctx_keys_render_dash_not_crash(self):
        res = self.emit("verdict_rejected", {}, force=True)
        self.assertTrue(res["ok"])
        for line in res["lines"]:
            self.assertNotIn("KeyError", line["text"])

    def test_agi_scene_injected_once_per_day(self):
        self.emit("launch", {"hid": "H-004"}, force=True)
        first = self.conn.execute(
            "SELECT COUNT(*) FROM crew_chat WHERE event='agi_day'").fetchone()[0]
        self.assertGreater(first, 0)
        self.emit("launch", {"hid": "H-005"}, force=True)
        second = self.conn.execute(
            "SELECT COUNT(*) FROM crew_chat WHERE event='agi_day'").fetchone()[0]
        self.assertEqual(first, second)  # повторно в тот же день — нет


class TestBudgetAndGuards(CrewBase):
    def test_no_send_without_thread(self):
        res = self.emit("launch", {"hid": "H-006"}, force=True)
        self.assertTrue(res["ok"])
        self.assertFalse(res["sent"])
        self.tg_send.assert_not_called()

    def test_daily_budget_is_respected(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           max_messages_per_day=2)
        with mock.patch.dict(os.environ, {"TELEGRAM_CREW_THREAD_ID": "777"}):
            for _ in range(5):
                crew.emit("verdict_confirmed", {"hid": "H-007"}, conn=self.conn,
                          config=cfg, rng=self.rng)
        # бюджет считается пачками-сообщениями: отправлены ровно первые две,
        # остальные события остались только в базе
        self.assertEqual(self.tg_send.call_count, 2)
        self.assertEqual(crew.sent_today(self.conn), 2)
        logged = self.conn.execute("SELECT COUNT(*) FROM crew_chat").fetchone()[0]
        self.assertGreater(logged, 2)   # непереданное тоже журналируется

    def test_quiet_hours_block_sending(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           quiet_hours="00:00-23:59")
        with mock.patch.dict(os.environ, {"TELEGRAM_CREW_THREAD_ID": "777"}):
            res = crew.emit("launch", {"hid": "H-008"}, conn=self.conn, config=cfg,
                            rng=self.rng)
        self.assertTrue(res["ok"])
        self.assertFalse(res["sent"])

    def test_cooldown_suppresses_repeated_noise(self):
        self.emit("queue_empty", {"min": 3})             # первый — прошёл
        res2 = self.emit("queue_empty", {"min": 3})      # второй за час — нет
        self.assertFalse(res2["ok"])
        self.assertIn("cooldown", res2["reason"])

    def test_disabled_crew_is_silent(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           enabled=False)
        res = crew.emit("launch", {"hid": "H-009"}, conn=self.conn, config=cfg,
                        rng=self.rng)
        self.assertFalse(res["ok"])

    def test_safe_emit_swallows_tg_exceptions(self):
        with mock.patch.object(crew.tg, "send", side_effect=RuntimeError("boom")):
            with mock.patch.dict(os.environ, {"TELEGRAM_CREW_THREAD_ID": "777"}):
                crew.safe_emit("launch", {"hid": "H-010"}, conn=self.conn,
                               config=CREW_CONFIG)
        # контур жив, реплики в базе
        n = self.conn.execute("SELECT COUNT(*) FROM crew_chat").fetchone()[0]
        self.assertGreater(n, 0)


class TestNudges(CrewBase):
    def test_nudge_metadata_is_sane(self):
        for n in crew.NUDGES:
            self.assertIn(n["agent"], crew.AGENTS)
            self.assertTrue(0.0 < n["effectiveness"] <= 1.0, n["id"])
            self.assertTrue(0.0 < n["positive"] <= 1.0, n["id"])
            self.assertTrue(n["text"].strip())

    def test_pick_nudge_is_weighted_and_deterministic(self):
        rng = random.Random(7)
        picks = {crew.pick_nudge(rng, self.conn)["id"] for _ in range(200)}
        # взвешенный выбор должен доставать больше одной фразы
        self.assertGreater(len(picks), 3)
        rng2 = random.Random(7)
        self.assertEqual(crew.pick_nudge(rng2, self.conn)["id"],
                         crew.pick_nudge(random.Random(7), self.conn)["id"])

    def test_nudge_usage_is_tracked(self):
        before = crew.nudge_stats(self.conn)
        crew.record_nudge(self.conn, "n04", won=True)
        crew.record_nudge(self.conn, "n04", won=False)
        after = crew.nudge_stats(self.conn)
        self.assertEqual(after["n04"]["uses"], before.get("n04", {}).get("uses", 0) + 2)

    def test_nudge_weight_shifts_with_measured_success(self):
        nudge = next(n for n in crew.NUDGES if n["id"] == "n06")
        base = crew.nudge_weight(nudge, {})
        losing = {"n06": {"uses": 10, "wins": 0}}
        self.assertLess(crew.nudge_weight(nudge, losing), base)


class TestReplayAndStats(CrewBase):
    def test_replay_returns_chronological_lines(self):
        self.emit("kill", {"hid": "H-011"}, force=True)
        self.emit("digest", {}, force=True)
        items = crew.replay(self.conn, 50)
        self.assertEqual([i["event"] for i in items][:1], ["kill"])
        self.assertLessEqual(len(items), 50)
        text = crew.replay_text(items)
        self.assertIn("Морг", text)      # kill-сцена начинается с Морга

    def test_replay_empty_is_polite(self):
        self.assertIn("молчит", crew.replay_text([]))

    def test_stats_shape_and_zero_cost(self):
        self.emit("hypo_new", {"hid": "H-012", "forecast": 5}, force=True)
        data = crew.stats(self.conn, CREW_CONFIG)
        self.assertEqual(data["cost"], {"gpu_hours": 0.0, "tokens": 0})
        self.assertGreaterEqual(data["total_lines"], 3)
        self.assertGreaterEqual(data["agi_days_left"], 0)
        self.assertIn("by_agent", data)
        self.assertTrue(data["nudges"])

    def test_agi_days_left_matches_config(self):
        data = crew.stats(self.conn, CREW_CONFIG)
        self.assertEqual(data["agi_days_left"], crew.agi_days_left(CREW_CONFIG))

    def test_plural_ru(self):
        self.assertEqual(crew.plural(1, "день", "дня", "дней"), "1 день")
        self.assertEqual(crew.plural(3, "день", "дня", "дней"), "3 дня")
        self.assertEqual(crew.plural(1341, "день", "дня", "дней"), "1341 день")
        self.assertEqual(crew.plural(11, "день", "дня", "дней"), "11 дней")


if __name__ == "__main__":
    unittest.main()
