"""Deterministic tests for the crew chat engine (рабочий чат экипажа).

No network, no GPU, no model calls: scenes are templates, the RNG is seeded,
tg.send is mocked. The chat must never break the contour.
"""

import os
import random
import re
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
        "limits": {"daily_gpu_hours_budget": 20},
        "crew": {
            "enabled": True,
            "max_messages_per_day": 30,
            "max_lines_per_event": 5,
            "dispute_probability": 1.0,     # споры — всегда, для теста
            "nudge_probability": 0.0,       # нуджи — никогда, для детерминизма
            "customer_line_probability": 0.0,   # пулы — точечно в своих тестах
            "noise_line_probability": 0.0,
            "customer_share_max": 0.06,
            "noise_share_max": 0.03,
            "quiet_hours": "",
            "agi_arrival": "2030-05-01",
        },
    }
}

EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


class CrewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "state.sqlite3"))
        self.rng = random.Random(42)
        patcher = mock.patch.object(crew.tg, "send", return_value={"ok": True})
        self.tg_send = patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("TELEGRAM_AICHAT_THREAD_ID", None)
        os.environ.pop("TELEGRAM_CHAT_THREAD_ID", None)
        os.environ.pop("TELEGRAM_CREW_THREAD_ID", None)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def emit(self, event, ctx=None, **kw):
        return crew.emit(event, ctx, conn=self.conn, config=CREW_CONFIG,
                         rng=self.rng, **kw)


class TestFormatting(CrewBase):
    def test_nicks_have_no_emoji_and_look_like_handles(self):
        for agent in crew.AGENTS.values():
            self.assertIsNone(EMOJI_RE.search(agent["name"]), agent["name"])
            self.assertLessEqual(len(agent["name"]), 9)
            self.assertTrue(agent["zone"].strip())

    def test_message_format_is_nick_bold_colon_text(self):
        res = self.emit("verdict_rejected", {"hid": "H-001", "forecast": 12,
                                             "actual": -4.9, "dev": "-141%",
                                             "hours": "0.4", "seeds": "0/3"},
                        force=True)
        for line in res["lines"]:
            name = crew.AGENTS[line["agent"]]["name"]
            self.assertIn(f"*{name}:*", crew.compose_message([line]))

    def test_lines_are_short_chat_style(self):
        res = self.emit("hypo_new", {"hid": "H-002", "forecast": 8, "signals": 4,
                                     "hours": 2}, force=True)
        for line in res["lines"]:
            self.assertLessEqual(len(line["text"]), 140)   # короткие реплики чата

    def test_scene_has_a_question_in_dialogue(self):
        # рабочая сцена — это обсуждение: в диалоге обязателен вопрос
        res = self.emit("queue_empty", {"min": 3}, force=True)
        self.assertTrue(any("?" in l["text"] for l in res["lines"]))


class TestScenes(CrewBase):
    def test_known_event_renders_lines(self):
        res = self.emit("verdict_rejected", {"hid": "H-001", "forecast": 12,
                                             "actual": -4.9, "dev": "-141%",
                                             "hours": "0.4", "seeds": "0/3"},
                        force=True)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(res["lines"]), 3)

    def test_unknown_event_is_a_noop(self):
        res = self.emit("martian_sunrise", force=True)
        self.assertFalse(res["ok"])

    def test_dispute_included_and_closed_by_boss_with_numbers(self):
        res = self.emit("hypo_new", {"hid": "H-003", "forecast": 9, "seeds": 3,
                                     "passed": 7}, force=True)
        dispute_lines = [l for l in res["lines"] if l.get("dispute_id")]
        self.assertTrue(dispute_lines)
        self.assertEqual(dispute_lines[-1]["agent"], "shef")
        self.assertTrue(dispute_lines[-1].get("arbiter"))

    def test_scene_respects_max_lines(self):
        res = self.emit("customer_lead", {"forecast": 12}, force=True)
        scene = [l for l in res["lines"] if l.get("event") == "customer_lead"]
        self.assertLessEqual(len(scene), 5)

    def test_missing_ctx_keys_render_dash_not_crash(self):
        res = self.emit("verdict_rejected", {}, force=True)
        self.assertTrue(res["ok"])
        for line in res["lines"]:
            self.assertNotIn("KeyError", line["text"])

    def test_weekly_scene_uses_bias(self):
        res = self.emit("weekly", {"bias": "+31"}, force=True)
        self.assertTrue(any("+31%" in l["text"] for l in res["lines"]))


class TestShare85x15(CrewBase):
    def test_customer_scene_declined_on_empty_history(self):
        # целиком «customer»-сцена на пустой истории отклоняется целиком
        res = self.emit("agi_day", {"agi": 10, "agi_txt": "10 дней"}, force=True)
        self.assertFalse(res["ok"])
        self.assertEqual(crew._side_share(self.conn, "customer"), 0.0)

    def test_customer_fits_after_work_history(self):
        # при достаточной рабочей истории суточная AGI-сцена встраивается сама
        # (cooldown 20 ч) — и доля пула остаётся в потолке ~5%
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           dispute_probability=0.0)
        for i in range(30):
            crew.emit("kill", {"hid": f"H-02{i}"}, conn=self.conn, config=cfg,
                      rng=self.rng, force=True)
        share = crew._side_share(self.conn, "customer")
        self.assertGreater(share, 0.0)
        self.assertLessEqual(share, 0.08)

    def test_pools_are_not_marked_in_replay(self):
        # в переписке НЕТ пометок вроде «(шёпот)» — пулы различает только stats
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(
            CREW_CONFIG["researchagen"]["crew"],
            dispute_probability=0.0, customer_line_probability=1.0)
        for _ in range(6):
            crew.emit("kill", {"hid": "H-030"}, conn=self.conn, config=cfg,
                      rng=self.rng, force=True)
            crew.emit("launch", {"hid": "H-031", "burn": 1, "budget": 20,
                                 "level": "L0"}, conn=self.conn, config=cfg,
                      rng=self.rng, force=True)
        text = crew.replay_text(crew.replay(self.conn, 100))
        self.assertNotIn("шёпот", text)
        self.assertGreater(crew._side_share(self.conn, "customer"), 0.0)

    def test_noise_pool_respects_cap(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(
            CREW_CONFIG["researchagen"]["crew"], dispute_probability=0.0,
            noise_line_probability=1.0)
        for _ in range(30):
            crew.emit("kill", {"hid": "H-032"}, conn=self.conn, config=cfg,
                      rng=self.rng, force=True)
        self.assertLessEqual(crew._side_share(self.conn, "noise"), 0.051)

    def test_side_budget_monotonic(self):
        b0 = crew._side_budget(self.conn, CREW_CONFIG, "customer", 10)
        self.assertGreaterEqual(b0, 0)
        for _ in range(5):   # рабочая история растёт — бюджет пула растёт
            self.emit("kill", {"hid": "H-021"}, force=True)
        self.assertGreaterEqual(
            crew._side_budget(self.conn, CREW_CONFIG, "customer", 10), b0)

    def test_arbiter_is_never_trimmed(self):
        # спор о заказчике не теряет арбитраж Boss
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           dispute_probability=1.0)
        for _ in range(4):
            crew.emit("customer_lead", {"forecast": 1}, conn=self.conn, config=cfg,
                      rng=self.rng, force=True)
        disputes = [r for r in crew.replay(self.conn, 400) if r.get("dispute_id")]
        by_id: dict[str, list] = {}
        for r in disputes:
            by_id.setdefault(r["dispute_id"], []).append(r)
        self.assertTrue(by_id)
        for lines in by_id.values():
            self.assertEqual(lines[-1]["agent"], "shef")
            self.assertEqual(lines[-1]["kind"], "work")

    def test_none_ctx_renders_dash(self):
        # симуляция поймала: None в контексте печататься как «None%»
        res = self.emit("hypo_new", {"hid": "H-022", "forecast": None}, force=True)
        self.assertTrue(res["ok"])
        for line in res["lines"]:
            self.assertNotIn("None", line["text"])

    def test_work_scenes_are_kind_work(self):
        for event in ("hypo_new", "gate_pass", "launch", "verdict_confirmed"):
            res = self.emit(event, {"hid": "H-004", "forecast": 5, "seeds": 3,
                                    "passed": 7, "hours": 1, "level": "L0",
                                    "budget": 20, "burn": 1, "dev": "+10%"}, force=True)
            work = [l for l in res["lines"] if l.get("kind") == "work"]
            self.assertTrue(work, event)


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
        with mock.patch.dict(os.environ, {"TELEGRAM_AICHAT_THREAD_ID": "777"}):
            for _ in range(5):
                crew.emit("verdict_confirmed", {"hid": "H-007"}, conn=self.conn,
                          config=cfg, rng=self.rng)
        self.assertEqual(self.tg_send.call_count, 2)
        self.assertEqual(crew.sent_today(self.conn), 2)

    def test_quiet_hours_block_sending(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           quiet_hours="00:00-23:59")
        with mock.patch.dict(os.environ, {"TELEGRAM_AICHAT_THREAD_ID": "777"}):
            res = crew.emit("launch", {"hid": "H-008"}, conn=self.conn, config=cfg,
                            rng=self.rng)
        self.assertTrue(res["ok"])
        self.assertFalse(res["sent"])

    def test_mute_pauses_delivery_and_unmutes(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_AICHAT_THREAD_ID": "777"}):
            crew.set_mute(self.conn, "2h")
            res = self.emit("launch", {"hid": "H-009"}, force=True)
            self.assertTrue(res["ok"])
            self.assertFalse(res["sent"])          # мьют блокирует доставку
            crew.set_mute(self.conn, "off")
            res2 = self.emit("finish_ok", {"hid": "H-009", "seeds": "3/3",
                                           "hours": "0.5"}, force=True)
            self.assertTrue(res2["sent"])          # сняли — пошло

    def test_cooldown_suppresses_repeated_noise(self):
        self.emit("queue_empty", {"min": 3})
        res2 = self.emit("queue_empty", {"min": 3})
        self.assertFalse(res2["ok"])
        self.assertIn("cooldown", res2["reason"])

    def test_disabled_crew_is_silent(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           enabled=False)
        res = crew.emit("launch", {"hid": "H-010"}, conn=self.conn, config=cfg,
                        rng=self.rng)
        self.assertFalse(res["ok"])

    def test_safe_emit_swallows_tg_exceptions(self):
        with mock.patch.object(crew.tg, "send", side_effect=RuntimeError("boom")):
            with mock.patch.dict(os.environ, {"TELEGRAM_AICHAT_THREAD_ID": "777"}):
                crew.safe_emit("launch", {"hid": "H-011"}, conn=self.conn,
                               config=CREW_CONFIG)
        n = self.conn.execute("SELECT COUNT(*) FROM crew_chat").fetchone()[0]
        self.assertGreater(n, 0)


class TestReview(CrewBase):
    def _add_hypo(self, hid, status="queued", signals=4, forecast=8, age_days=0,
                  card=None):
        created = core.iso(core.now() - __import__("datetime").timedelta(days=age_days))
        self.conn.execute(
            "INSERT INTO hypotheses (id, title, status, level, signals, novelty,"
            " early_pct, standard, money, decidability, est_hours, forecast,"
            " kill_checks_passed, source, card_path, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hid, "t " + hid, status, "L0", signals, 0.5, 10.0, 0.4, 0.7, 0.5,
             2.0, forecast, 0, "dr", card, created, created))
        self.conn.commit()

    def test_fake_evidence_detected_and_resolved(self):
        # карточка: один kill-check с passed: true и пустым evidence
        card = os.path.join(self.tmp.name, "H-100.yaml")
        with open(card, "w", encoding="utf-8") as fh:
            fh.write('kill_checks:\n  - check: "simple explanation"\n'
                     '    passed: true\n    evidence: ""\n')
        self._add_hypo("H-100", card=card)
        data = crew.run_review(self.conn, CREW_CONFIG, emit_scenes=False)
        kinds = [f["kind"] for f in data["fresh"]]
        self.assertIn("review_fake_evidence", kinds)
        # чиним: заполняем evidence — находка закрывается сценой «починено»
        with open(card, "w", encoding="utf-8") as fh:
            fh.write('kill_checks:\n  - check: "simple explanation"\n'
                     '    passed: true\n    evidence: "lr ablation in exp-1"\n')
        data2 = crew.run_review(self.conn, CREW_CONFIG, emit_scenes=False)
        self.assertEqual(crew.open_count(self.conn), 0)
        self.assertTrue(any(f["finding_id"].startswith("fake_evidence")
                            for f in data2["resolved"]))

    def test_weak_signals_and_no_forecast_found(self):
        self._add_hypo("H-101", signals=2, forecast=None, age_days=2)
        data = crew.run_review(self.conn, CREW_CONFIG, emit_scenes=False)
        kinds = [f["kind"] for f in data["fresh"]]
        self.assertIn("review_weak_signals", kinds)
        self.assertIn("review_no_forecast", kinds)

    def test_rotting_queue_and_patent_candidate(self):
        self._add_hypo("H-102", status="queued", age_days=9)
        self._add_hypo("H-103", status="confirmed", forecast=5)
        data = crew.run_review(self.conn, CREW_CONFIG, emit_scenes=False)
        kinds = [f["kind"] for f in data["fresh"]]
        self.assertIn("review_rotting_queue", kinds)
        self.assertIn("review_patent_candidate", kinds)

    def test_review_emits_chat_scenes(self):
        self._add_hypo("H-104", signals=1)
        with mock.patch.dict(os.environ, {"TELEGRAM_AICHAT_THREAD_ID": "777"}):
            crew.run_review(self.conn, CREW_CONFIG, emit_scenes=True)
        events = {r["event"] for r in crew.replay(self.conn, 50)}
        self.assertIn("review_weak_signals", events)

    def test_safe_review_respects_interval(self):
        self._add_hypo("H-105", signals=1)
        crew.safe_review(self.conn, CREW_CONFIG)
        first = crew.open_count(self.conn)
        self.assertGreaterEqual(first, 1)
        # второй вызов сразу — cooldown, новых сцен нет
        with mock.patch.object(crew, "run_review",
                               wraps=crew.run_review) as rr:
            crew.safe_review(self.conn, CREW_CONFIG)
            rr.assert_not_called()

    def test_review_never_crashes_on_empty_db(self):
        data = crew.run_review(self.conn, CREW_CONFIG, emit_scenes=False)
        self.assertEqual(data["fresh"], [])


class TestJokes(CrewBase):
    def test_joke_bank_has_layers_and_mechanisms(self):
        for joke in crew.JOKES:
            self.assertTrue(joke["text"].strip())
            self.assertGreaterEqual(len(joke["mechs"]), 2, joke["text"][:30])

    def test_render_joke_intensity_gates(self):
        rng = random.Random(1)
        seen_none, seen_tag = False, False
        for _ in range(300):
            joke = crew.render_joke(rng)
            if joke is None:
                seen_none = True
            else:
                self.assertNotIn("{", joke["text"])
                self.assertLessEqual(len(joke["text"]), 200)
                if joke["joke_meta"]["intensity"] >= 75:
                    seen_tag = seen_tag or True
        self.assertTrue(seen_none, "низкая интенсивность иногда должна резать шутку")
        self.assertTrue(seen_tag, "высокая интенсивность иногда должна давать Tag")

    def test_render_joke_is_deterministic(self):
        self.assertEqual(crew.render_joke(random.Random(9))["text"],
                         crew.render_joke(random.Random(9))["text"])

    def test_joke_in_scene_when_probability_is_one(self):
        cfg = {"researchagen": dict(CREW_CONFIG["researchagen"])}
        cfg["researchagen"]["crew"] = dict(CREW_CONFIG["researchagen"]["crew"],
                                           joke_probability=1.0)
        events = set()
        for i in range(60):
            res = crew.emit("kill", {"hid": f"H-04{i}"}, conn=self.conn, config=cfg,
                            rng=random.Random(i), force=True)
            events |= {l["event"] for l in res["lines"]}
        self.assertIn("joke", events)   # при p=1 и 60 бросках точно хотя бы раз


class TestNudges(CrewBase):
    def test_nudge_metadata_is_sane(self):
        for n in crew.NUDGES:
            self.assertIn(n["agent"], crew.AGENTS)
            self.assertTrue(0.0 < n["effectiveness"] <= 1.0, n["id"])
            self.assertTrue(0.0 < n["positive"] <= 1.0, n["id"])

    def test_pick_nudge_is_weighted_and_deterministic(self):
        rng = random.Random(7)
        picks = {crew.pick_nudge(rng, self.conn)["id"] for _ in range(200)}
        self.assertGreater(len(picks), 3)
        self.assertEqual(crew.pick_nudge(random.Random(7), self.conn)["id"],
                         crew.pick_nudge(random.Random(7), self.conn)["id"])

    def test_nudge_weight_shifts_with_measured_success(self):
        nudge = next(n for n in crew.NUDGES if n["id"] == "n06")
        base = crew.nudge_weight(nudge, {})
        losing = {"n06": {"uses": 10, "wins": 0}}
        self.assertLess(crew.nudge_weight(nudge, losing), base)


class TestReplayAndStats(CrewBase):
    def test_replay_returns_chronological_lines(self):
        self.emit("kill", {"hid": "H-011"}, force=True)
        self.emit("digest", {"open_findings": 0}, force=True)
        items = crew.replay(self.conn, 50)
        self.assertEqual([i["event"] for i in items][:1], ["kill"])
        self.assertIn("Морг", crew.replay_text(items))

    def test_replay_empty_is_polite(self):
        self.assertIn("молчит", crew.replay_text([]))

    def test_stats_shape_and_zero_cost(self):
        self.emit("hypo_new", {"hid": "H-012", "forecast": 5, "signals": 4,
                               "hours": 2}, force=True)
        data = crew.stats(self.conn, CREW_CONFIG)
        self.assertEqual(data["cost"], {"gpu_hours": 0.0, "tokens": 0})
        self.assertIn("customer_share", data)
        self.assertIn("noise_share", data)
        self.assertIn("open_findings", data)
        self.assertIn("agents", data)
        self.assertTrue(data["nudges"])

    def test_plural_ru(self):
        self.assertEqual(crew.plural(1, "день", "дня", "дней"), "1 день")
        self.assertEqual(crew.plural(3, "день", "дня", "дней"), "3 дня")
        self.assertEqual(crew.plural(1341, "день", "дня", "дней"), "1341 день")
        self.assertEqual(crew.plural(11, "день", "дня", "дней"), "11 дней")


if __name__ == "__main__":
    unittest.main()
