"""Тесты контура researchagen. Только stdlib: python -m unittest discover -s tests

Проверяется не «код запускается», а инварианты, которые нельзя доверить модели:
приоритеты, гейты, отказы вердикта и калибровки, чекпойнт, границы платформ.
"""

import os
import sys
import tempfile
import unittest
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import core  # noqa: E402
import queue as q  # noqa: E402
import verdict as v  # noqa: E402
import calib  # noqa: E402
import gpu  # noqa: E402
import dispatch  # noqa: E402
import exp_runner  # noqa: E402
import hypo  # noqa: E402
import hygiene  # noqa: E402
import board  # noqa: E402
import tg  # noqa: E402

MAC_DEBUG = {"researchagen": {"platform": "macos", "mode": "debug",
                              "limits": {"gpu_free_gb_required": 20,
                                         "daily_gpu_hours_budget": 8,
                                         "approval_gpu_hours": 6,
                                         "max_parallel_experiments": 1,
                                         "preempt_ratio": 2.0}}}
WIN_PROD = {"researchagen": {"platform": "windows", "mode": "production",
                             "limits": {"gpu_free_gb_required": 6}}}


class TempDb(unittest.TestCase):
    """Каждый тест — своя база: тесты не трогают рабочее состояние профиля."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = core.db(os.path.join(self.tmp.name, "t.sqlite3"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def row(self, hid):
        return self.conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()


class TestScoring(unittest.TestCase):
    def test_signals_below_three_score_zero(self):
        # MISSION.md: гипотеза с <3 независимыми сигналами не имеет права на GPU
        for n in (0, 1, 2):
            self.assertEqual(q.signal_score(n), 0.0)
        self.assertEqual(q.signal_score(3), 0.50)
        self.assertEqual(q.signal_score(4), 0.67)
        self.assertEqual(q.signal_score(5), 0.84)
        self.assertEqual(q.signal_score(9), 1.0)

    def test_early_score_monotonic(self):
        self.assertEqual(q.early_score(1.0), 1.0)
        self.assertEqual(q.early_score(0.3), 1.0)
        self.assertEqual(q.early_score(10.0), 0.0)
        self.assertEqual(q.early_score(50.0), 0.0)
        self.assertGreater(q.early_score(3.0), q.early_score(7.0))

    def test_weights_normalised(self):
        w = q.weights({})
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)

    def test_bins(self):
        self.assertEqual(q.bin_of(4), "P1")
        self.assertEqual(q.bin_of(4.1), "P2")
        self.assertEqual(q.bin_of(12), "P2")
        self.assertEqual(q.bin_of(40), "P3")
        self.assertEqual(q.bin_of(96), "P4")

    def test_pi_bounded_and_ppi_is_pi_per_hour(self):
        best = {"signals": 9, "novelty": 1, "early_pct": 1, "standard": 1,
                "money": 1, "decidability": 1, "est_hours": 4,
                "created_at": core.iso()}
        worst = dict(best, signals=0, novelty=0, early_pct=50, standard=0,
                     money=0, decidability=0)
        self.assertAlmostEqual(q.pi(best), 1.0, places=3)
        self.assertAlmostEqual(q.pi(worst), 0.0, places=3)
        self.assertAlmostEqual(q.ppi(best), q.pi(best) / 4.0, places=3)

    def test_aging_is_capped(self):
        old = core.iso(core.now() - timedelta(days=365))
        self.assertAlmostEqual(q.aging_bonus(old), 0.30, places=6)


class TestQueue(TempDb):
    def test_pick_next_prefers_cheap_bin_over_bigger_pi(self):
        q.add(self.conn, "Дорогая красивая", signals=6, novelty=1.0, early_pct=1,
              standard=1.0, money=1.0, decidability=1.0, est_hours=40, forecast=20)
        q.add(self.conn, "Дешёвая приличная", signals=4, novelty=0.6, early_pct=3,
              standard=0.6, money=0.6, decidability=0.9, est_hours=3, forecast=10)
        chosen = q.pick_next(self.conn)
        self.assertEqual(chosen["title"], "Дешёвая приличная")
        self.assertIn(chosen["bin"], ("P1", "P2"))
        self.assertIn("PPI", chosen["reason"])

    def test_pick_next_falls_back_to_pi_when_no_cheap(self):
        q.add(self.conn, "Только дорогая", signals=5, est_hours=60, forecast=15)
        chosen = q.pick_next(self.conn)
        self.assertEqual(chosen["bin"], "P4")
        self.assertIn("aging", chosen["reason"])

    def test_closed_hypotheses_leave_the_queue(self):
        h = q.add(self.conn, "Закроем", signals=4)
        q.set_status(self.conn, h["id"], "rejected")
        self.assertIsNone(q.pick_next(self.conn))
        self.assertEqual(q.live_count(self.conn), 0)

    def test_ids_increment(self):
        self.assertEqual(q.add(self.conn, "a")["id"], "H-001")
        self.assertEqual(q.add(self.conn, "b")["id"], "H-002")


class TestHypoGate(TempDb):
    def test_gate_blocks_weak_hypothesis(self):
        h = q.add(self.conn, "Без карточки и прогноза", signals=1)
        gate = hypo.check(h["id"], self.conn)
        self.assertFalse(gate["ok"])
        joined = " | ".join(gate["problems"])
        self.assertIn("сигналов", joined)
        self.assertIn("прогноз", joined)
        self.assertIn("kill-stage", joined)

    def test_gate_counts_kill_checks(self):
        h = q.add(self.conn, "С карточкой", signals=4, forecast=12)
        path = hypo.write_card(h["id"], "С карточкой", signals=4, forecast=12)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        gate = hypo.check(h["id"], self.conn)
        self.assertEqual(gate["kill_checks_passed"], 0)
        self.assertGreaterEqual(gate["kill_checks_total"], 7)
        self.assertFalse(gate["ok"])  # незаполненная карточка не даёт запуска


class TestVerdict(TempDb):
    def test_deviation(self):
        self.assertEqual(v.deviation(20, 10), -50.0)
        self.assertEqual(v.deviation(10, 15), 50.0)
        self.assertIsNone(v.deviation(None, 10))
        self.assertIsNone(v.deviation(0, 10))

    def test_banned_wording_detected(self):
        self.assertTrue(v.check_language("выглядит интересно, надо копать"))
        self.assertFalse(v.check_language("эффект 3.1% на 3/3 seeds, идём на L2"))

    def test_record_refuses_evaluative_wording(self):
        h = q.add(self.conn, "Гипотеза", signals=4, forecast=10)
        with self.assertRaises(SystemExit):
            v.record(self.conn, h["id"], "partial", actual=5,
                     changes="перспективно, продолжаем")

    def test_record_stores_deviation_and_closes_hypothesis(self):
        h = q.add(self.conn, "Гипотеза", signals=4, forecast=10)
        res = v.record(self.conn, h["id"], "rejected", actual=2, seeds_pass=1,
                       seeds_total=3, gpu_hours=0.4,
                       changes="L2 не запускаем, ветку сигналов закрываем")
        self.addCleanup(lambda: os.path.exists(res["report"]) and os.remove(res["report"]))
        self.assertEqual(res["deviation"], -80.0)
        self.assertEqual(self.row(h["id"])["status"], "rejected")
        self.assertIn("ОПРОВЕРГНУТО", res["text"])

    def test_unknown_kind_refused(self):
        h = q.add(self.conn, "Гипотеза", signals=4, forecast=10)
        with self.assertRaises(SystemExit):
            v.record(self.conn, h["id"], "почти получилось", actual=1, changes="—")


class TestCalibration(TempDb):
    def test_refuses_on_small_sample(self):
        res = calib.apply(self.conn, config=MAC_DEBUG)
        self.assertFalse(res["applied"])
        self.assertIn("самообман", res["reason"])

    def test_step_is_bounded(self):
        current = calib.effective_weights(self.conn, MAC_DEBUG)
        proposed = calib.proposed_weights(self.conn, MAC_DEBUG)
        for factor, weight in current.items():
            self.assertLessEqual(abs(proposed[factor] - weight),
                                 weight * calib.MAX_REL_STEP + 1e-6)
        self.assertAlmostEqual(sum(proposed.values()), 1.0, places=3)


class TestGpuGate(unittest.TestCase):
    def test_macos_debug_allows_dry_run(self):
        ok, why, snap = gpu.can_launch(config=MAC_DEBUG)
        self.assertTrue(ok)
        self.assertIn("dry-run", why)
        self.assertTrue(snap["debug"])

    def test_production_without_gpu_refuses(self):
        if gpu.read_nvidia_smi():
            self.skipTest("на этой машине есть nvidia-smi")
        ok, why, _ = gpu.can_launch(config=WIN_PROD)
        self.assertFalse(ok)
        self.assertIn("production", why)


class TestDispatchGates(TempDb):
    def test_pause_blocks_launch(self):
        h = q.add(self.conn, "Гипотеза", signals=4, forecast=10)
        core.set_setting(self.conn, "dispatch.paused", True)
        self.assertTrue(dispatch.is_paused(self.conn))
        res = dispatch.launch(self.conn, h["id"], "L0", config=MAC_DEBUG)
        self.assertFalse(res["ok"])
        self.assertIn("пауз", res["reason"])

    def test_tick_reports_pause_without_touching_gpu(self):
        core.set_setting(self.conn, "dispatch.paused", True)
        self.assertEqual(dispatch.tick(self.conn, MAC_DEBUG)["action"], "paused")

    def test_gate_failure_blocks_launch(self):
        h = q.add(self.conn, "Сырая гипотеза", signals=1)
        res = dispatch.launch(self.conn, h["id"], "L0", config=MAC_DEBUG)
        self.assertFalse(res["ok"])
        self.assertIn("гейт", res["reason"])
        self.assertTrue(res["problems"])

    def test_approval_required_for_expensive_run(self):
        h = q.add(self.conn, "Дорогая", signals=6, forecast=10, est_hours=40)
        self.assertFalse(dispatch.approved(self.conn, h["id"]))
        dispatch.approve(self.conn, h["id"])
        self.assertTrue(dispatch.approved(self.conn, h["id"]))

    def test_preempt_without_running_run(self):
        res = dispatch.preempt(self.conn, MAC_DEBUG)
        self.assertFalse(res["ok"])

    def test_preempt_requires_ratio(self):
        cur = q.add(self.conn, "Текущая", signals=6, novelty=1.0, early_pct=1,
                    standard=1.0, money=1.0, decidability=1.0, est_hours=2, forecast=10)
        q.add(self.conn, "Слабый претендент", signals=3, novelty=0.2, early_pct=9,
              standard=0.2, money=0.2, decidability=0.3, est_hours=4, forecast=5)
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at) VALUES (?,?,'running',?)",
            (cur["id"], "L1", core.iso()))
        self.conn.commit()
        res = dispatch.preempt(self.conn, MAC_DEBUG)
        self.assertFalse(res["ok"])
        self.assertIn("не оправдано", res["reason"])

    def test_budget_is_counted_from_runs(self):
        h = q.add(self.conn, "Гипотеза", signals=4, forecast=10)
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, gpu_hours)"
            " VALUES (?,?,'done',?,?)", (h["id"], "L1", core.iso(), 7.5))
        self.conn.commit()
        self.assertAlmostEqual(dispatch.gpu_hours_today(self.conn), 7.5, places=3)


class TestExperimentRunner(unittest.TestCase):
    def test_stop_flag_roundtrip(self):
        hid = "H-TEST"
        exp_runner.clear_stop(hid)
        self.assertFalse(exp_runner.stop_requested(hid))
        flag = os.path.join(core.STATE_DIR, f"stop-{hid}.flag")
        with open(flag, "w", encoding="utf-8") as fh:
            fh.write("test")
        self.assertTrue(exp_runner.stop_requested(hid))
        exp_runner.clear_stop(hid)
        self.assertFalse(exp_runner.stop_requested(hid))

    def test_seed_run_stops_at_checkpoint_not_by_kill(self):
        hid = "H-TEST"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        tmp.close()
        flag = os.path.join(core.STATE_DIR, f"stop-{hid}.flag")
        with open(flag, "w", encoding="utf-8") as fh:
            fh.write("stop")
        try:
            res = exp_runner.synthetic_seed_run(1, 500, hid, "L0", tmp.name, dry_run=True)
            self.assertTrue(res["stopped"])
            self.assertLess(res["steps_done"], 500)   # прервано, но результат сохранён
            self.assertGreater(res["steps_done"], 0)
        finally:
            exp_runner.clear_stop(hid)
            os.remove(tmp.name)

    def test_level_budgets_grow(self):
        seeds = exp_runner.LEVEL_SEEDS
        steps = exp_runner.LEVEL_STEPS
        self.assertEqual(seeds["L0"], 1)
        self.assertGreaterEqual(seeds["L1"], 3)
        self.assertTrue(steps["L0"] < steps["L1"] < steps["L2"] < steps["L3"])


class TestTelegramFormatting(unittest.TestCase):
    def test_progress_card(self):
        text = tg.progress_card("H-007", "L1", 50.0, "Идёт seed 2/3",
                                {"прогноз": "12%"})
        self.assertIn("H-007", text)
        self.assertIn("L1", text)
        self.assertIn("50%", text)
        self.assertIn("прогноз", text)
        self.assertIn("█", text)

    def test_endpoint_is_https_bot_api(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
        url = tg.endpoint("sendMessage")
        self.assertTrue(url.startswith("https://"))
        self.assertTrue(url.endswith("/sendMessage"))
        self.assertNotIn("{", url)      # защита от артефактов подстановки


class TestConfigAndEnv(unittest.TestCase):
    def test_repo_config_parses(self):
        conf = core.load_config(os.path.join(ROOT, "config.yaml"))
        self.assertIn("researchagen", conf)
        self.assertIsNotNone(core.cfg("researchagen.limits.preempt_ratio",
                                      None, conf))
        self.assertEqual(core.cfg("delegation.max_spawn_depth", None, conf), 1)
        self.assertFalse(core.cfg("delegation.orchestrator_enabled", True, conf))
        self.assertEqual(core.cfg("researchagen.governor.max_research_children", None, conf), 2)

    def test_cfg_returns_default_for_missing(self):
        self.assertEqual(core.cfg("researchagen.нет.такого", 42, {}), 42)

    def test_platform_mode_pairs(self):
        self.assertEqual(core.platform_mode(MAC_DEBUG), ("macos", True))
        self.assertEqual(core.platform_mode(WIN_PROD), ("windows", False))


class TestHygiene(TempDb):
    def test_dead_pid(self):
        self.assertFalse(hygiene.pid_alive(None))
        self.assertFalse(hygiene.pid_alive(0))

    def test_stale_run_is_reaped(self):
        h = q.add(self.conn, "Зависшая", signals=4, forecast=10)
        old = core.iso(core.now() - timedelta(hours=48))
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, pid)"
            " VALUES (?,?,'running',?,?)", (h["id"], "L2", old, 999999999))
        self.conn.commit()
        reaped = hygiene.reap_stale_runs(self.conn, 24.0)
        self.assertEqual(len(reaped), 1)
        self.assertEqual(self.row(h["id"])["status"], "blocked")

    def test_fresh_live_run_is_kept(self):
        h = q.add(self.conn, "Живая", signals=4, forecast=10)
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, pid)"
            " VALUES (?,?,'running',?,?)", (h["id"], "L1", core.iso(), os.getpid()))
        self.conn.commit()
        self.assertEqual(hygiene.reap_stale_runs(self.conn, 24.0), [])


class TestBoardMapping(unittest.TestCase):
    def test_every_status_has_kanban_column(self):
        for status in core.LIVE_STATUSES + core.CLOSED_STATUSES:
            self.assertIn(status, board.STATUS_MAP)
        self.assertEqual(board.STATUS_MAP["paused_checkpoint"], "review")
        self.assertEqual(board.STATUS_MAP["killed"], "archived")


if __name__ == "__main__":
    unittest.main(verbosity=2)
