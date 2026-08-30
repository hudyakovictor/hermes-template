"""v5: ставки, коридор прогноза, асимметричный штраф, спрос-гейт, MII, спорность.

Всё детерминировано: сидированный RNG, мок tg.send, никаких сетей и GPU.
"""

import os
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import calib  # noqa: E402
import core   # noqa: E402
import crew   # noqa: E402
import hypo   # noqa: E402
import queue  # noqa: E402
import verdict  # noqa: E402


def seeded_config(**over):
    cfg = {
        "researchagen": {
            "platform": "macos", "mode": "debug",
            "limits": {"daily_gpu_hours_budget": 20},
            "crew": {
                "enabled": True, "max_messages_per_day": 100,
                "max_lines_per_event": 5, "dispute_probability": 1.0,
                "nudge_probability": 0.0, "customer_line_probability": 0.0,
                "noise_line_probability": 0.0, "joke_probability": 0.0,
                "customer_share_max": 0.06, "noise_share_max": 0.03,
                "quiet_hours": "", "agi_arrival": "2030-05-01",
            },
        }
    }
    cfg["researchagen"]["crew"].update(over)
    return cfg


def hypo_row(conn, hid):
    return conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()


class V5Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        self.conn = core.db(self.db)
        self.rng = random.Random(42)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_hypo(self, hid="H-001", **kw):
        fields = dict(title="Тест", question="q", method="m", expected="e",
                      signals=4, forecast=10.0, est_hours=2.0, level="L0",
                      kill="k", cost="c", diff="d", result="r")
        fields.update(kw)
        row = queue.add(self.conn, fields.pop("title"), **fields)
        if hid != row["id"]:
            self.conn.execute("UPDATE hypotheses SET id=? WHERE id=?", (hid, row["id"]))
            self.conn.commit()
        return hid


class TestMigrations(V5Base):
    def test_fresh_db_has_v5_columns(self):
        cols_h = {r["name"] for r in self.conn.execute("PRAGMA table_info(hypotheses)")}
        for c in ("forecast_low", "forecast_high", "p_repro", "base_rate",
                  "buyer", "industry_usecase", "demand_signals", "controversy"):
            self.assertIn(c, cols_h)
        cols_v = {r["name"] for r in self.conn.execute("PRAGMA table_info(verdicts)")}
        for c in ("in_corridor", "forecast_low", "forecast_high"):
            self.assertIn(c, cols_v)
        tabs = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("agent_bets", tabs)

    def test_old_db_upgraded_in_place(self):
        # старая база без v5-колонок накатывается ALTER'ами без потери данных
        self.conn.close()
        os.remove(self.db)
        old = """CREATE TABLE hypotheses (
            id TEXT PRIMARY KEY, title TEXT, status TEXT,
            created_at TEXT, updated_at TEXT);
        CREATE TABLE verdicts (
            vid INTEGER PRIMARY KEY AUTOINCREMENT, hypo_id TEXT, level TEXT,
            kind TEXT, forecast REAL, actual REAL, deviation REAL,
            seeds_pass INTEGER, seeds_total INTEGER, sigma REAL,
            gpu_hours REAL, what_changes TEXT, created_at TEXT);"""
        c = core.sqlite3.connect(self.db)
        c.executescript(old)
        c.execute("INSERT INTO hypotheses (id, title) VALUES ('H-OLD', 'старая')")
        c.commit()
        c.close()
        conn = core.db(self.db)
        row = conn.execute("SELECT title, controversy FROM hypotheses "
                           "WHERE id='H-OLD'").fetchone()
        self.assertEqual(row["title"], "старая")
        self.assertEqual(row["controversy"], 0)


class TestBets(V5Base):
    def test_place_bets_deterministic_and_capped(self):
        hid = self.add_hypo(p_repro=0.7)
        crew.place_bets(self.conn, hid, 0.7)
        first = self.conn.execute("SELECT agent, bet FROM agent_bets "
                                  "WHERE hypo_id=?", (hid,)).fetchall()
        self.assertTrue(2 <= len(first) <= 4)
        crew.place_bets(self.conn, hid, 0.7)   # повтор — не дублирует
        second = self.conn.execute("SELECT COUNT(*) FROM agent_bets "
                                   "WHERE hypo_id=?", (hid,)).fetchone()[0]
        self.assertEqual(second, len(first))
        for r in first:
            self.assertIn(r["bet"], ("confirmed", "rejected"))

    def test_resolve_bets_confirmed_rewards_yes(self):
        hid = self.add_hypo()
        crew.place_bets(self.conn, hid, 0.9)   # p высокий → почти все «за»
        summary = crew.resolve_bets(self.conn, hid, "confirmed")
        self.assertEqual(summary["n"], len(summary["won"]))
        self.assertEqual(summary["lost"], [])
        rows = self.conn.execute("SELECT resolved, won FROM agent_bets "
                                 "WHERE hypo_id=?", (hid,)).fetchall()
        self.assertTrue(all(r["resolved"] and r["won"] for r in rows))

    def test_resolve_bets_rejected_rewards_no(self):
        hid = self.add_hypo()
        for agent, bet in (("krot", "rejected"), ("shim", "rejected"),
                           ("kira", "confirmed")):
            self.conn.execute(
                "INSERT INTO agent_bets (agent, hypo_id, bet, made_at)"
                " VALUES (?,?,?,?)", (agent, hid, bet, core.iso()))
        self.conn.commit()
        summary = crew.resolve_bets(self.conn, hid, "rejected")
        self.assertEqual(summary["n"], 3)
        self.assertEqual(len(summary["won"]), 2)   # «против» выиграли
        self.assertEqual(len(summary["lost"]), 1)  # «за» проиграл

    def test_bet_scores_counted(self):
        hid = self.add_hypo()
        crew.place_bets(self.conn, hid, 0.9)
        crew.resolve_bets(self.conn, hid, "confirmed")
        scores = crew.bet_scores(self.conn)
        self.assertTrue(scores)
        for row in scores:
            if row["bets"]:
                self.assertGreaterEqual(row["won"], 0)
                self.assertLessEqual(row["won"], row["bets"])

    def test_manual_bet_cli(self):
        hid = self.add_hypo()
        with mock.patch.object(core, "emit"), \
             mock.patch.object(core, "db", return_value=self.conn):
            rc = crew.main(["crew.py", "bet", hid, "--agent", "krot",
                            "--outcome", "rejected"])
        self.assertEqual(rc, 0)
        row = self.conn.execute("SELECT agent, bet FROM agent_bets "
                                "WHERE hypo_id=?", (hid,)).fetchone()
        self.assertEqual((row["agent"], row["bet"]), ("krot", "rejected"))

    def test_scene_shows_bets(self):
        hid = self.add_hypo(p_repro=0.7)
        crew.place_bets(self.conn, hid, 0.7)
        line = crew._bets_line(self.conn, hid)
        self.assertIn("став", line.lower())
        res = crew.emit("hypo_new", {"hid": hid, "forecast": "10%",
                                     "ppi": "0.50", "hours": "2", "signals": 4,
                                     "bets_line": line},
                        conn=self.conn, config=seeded_config(),
                        rng=random.Random(1), force=True)
        self.assertTrue(res["ok"])


class TestCorridorAndAsym(V5Base):
    def test_corridor_default_and_pass(self):
        hid = self.add_hypo(forecast=10.0, forecast_low=None, forecast_high=None)
        row = hypo_row(self.conn, hid)
        self.assertEqual((row["forecast_low"], row["forecast_high"]), (6.0, 14.0))

    def test_verdict_records_corridor_hit(self):
        hid = self.add_hypo(forecast=10.0)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"):
            verdict.record(self.conn, hid, "confirmed", actual=12.0,
                           seeds_pass="3", seeds_total="3", sigma="0.1",
                           gpu_hours="1.0", changes="ok")
        v = self.conn.execute("SELECT in_corridor, forecast_low, forecast_high "
                              "FROM verdicts WHERE hypo_id=?", (hid,)).fetchone()
        self.assertEqual(v["in_corridor"], 1)      # 12 внутри [6, 14]

    def test_verdict_records_corridor_miss(self):
        hid = self.add_hypo(forecast=10.0)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"):
            verdict.record(self.conn, hid, "rejected", actual=-4.0,
                           seeds_pass="0", seeds_total="3", sigma="0.2",
                           gpu_hours="1.0", changes="нет")
        v = self.conn.execute("SELECT in_corridor FROM verdicts "
                              "WHERE hypo_id=?", (hid,)).fetchone()
        self.assertEqual(v["in_corridor"], 0)

    def test_auto_penalty_thin_evidence(self):
        # #8: сигналов < 3 → прогноз срезается на 20% при постановке
        with mock.patch.object(hypo, "write_card", return_value="/dev/null"):
            row = hypo.create(self.conn, "Тонкая evidencia",
                              {"title": "Тонкая evidencia", "signals": 2,
                               "forecast": 10.0, "est_hours": 2.0})
        self.assertEqual(row["forecast"], 8.0)
        self.assertIn("Автоштраф", row["penalty_note"])
        db_row = hypo_row(self.conn, row["id"])
        self.assertEqual(db_row["forecast"], 8.0)

    def test_no_penalty_with_strong_evidence(self):
        with mock.patch.object(hypo, "write_card", return_value="/dev/null"):
            row = hypo.create(self.conn, "Сильная evidencia",
                              {"title": "Сильная evidencia", "signals": 4,
                               "forecast": 10.0, "est_hours": 2.0})
        self.assertEqual(row["forecast"], 10.0)
        self.assertEqual(row["penalty_note"], "")

    def test_asym_penalty_in_calibration(self):
        hid1 = self.add_hypo("H-001", forecast=10.0)
        hid2 = self.add_hypo("H-002", forecast=10.0)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"):
            verdict.record(self.conn, hid1, "confirmed", actual=5.0,   # -50%
                           seeds_pass="3", seeds_total="3", sigma="0.1",
                           gpu_hours="1.0", changes="ok")
            verdict.record(self.conn, hid2, "confirmed", actual=15.0,  # +50%
                           seeds_pass="3", seeds_total="3", sigma="0.1",
                           gpu_hours="1.0", changes="ok")
        rep = verdict.calibration(self.conn)
        # обычный MAE = 50, асимметричный = (100 + 50)/2 = 75
        self.assertEqual(rep["mean_abs_deviation_pct"], 50.0)
        self.assertEqual(rep["asym_penalty_pct"], 75.0)
        self.assertEqual(rep["corridor_hits"], "0/2")


class TestDemandGate(V5Base):
    def _gate_ready(self, hid):
        # гейт схемы: карточка со всеми секциями и 8/8 kill-галочек
        card = os.path.join(self.tmp.name, f"{hid}.md")
        text = "\n\n".join(f"{sec}: |\n  заполнено" for sec in hypo.REQUIRED_SECTIONS)
        text += "\n\nkill_checks:\n" + "\n".join(["- passed: true"] * 8)
        with open(card, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.conn.execute("UPDATE hypotheses SET card_path=?, signals=4 WHERE id=?",
                          (card, hid))
        self.conn.commit()

    def test_l2_blocked_without_demand(self):
        import dispatch
        hid = self.add_hypo(level="L1", demand_signals=1)
        self._gate_ready(hid)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"):
            out = dispatch.launch(self.conn, hid, "L2", config=seeded_config())
        self.assertFalse(out["ok"])
        self.assertIn("спрос", out["reason"])
        ev = self.conn.execute("SELECT kind FROM events "
                               "ORDER BY rowid DESC").fetchone()
        self.assertEqual(ev["kind"], "dispatch.demand_block")

    def test_l2_allowed_with_demand(self):
        import dispatch
        import governor
        import tg
        hid = self.add_hypo(level="L1", demand_signals=3)
        self._gate_ready(hid)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"), \
             mock.patch.object(tg, "send"), \
             mock.patch.object(dispatch.gpu, "can_launch",
                               return_value=(True, "ok", {"debug": True, "available": False})), \
             mock.patch.object(governor, "acquire_experiment",
                               return_value={"ok": True, "lease_id": 1}), \
             mock.patch.object(dispatch.subprocess, "Popen") as popen:
            popen.return_value.pid = 123
            out = dispatch.launch(self.conn, hid, "L2", config=seeded_config())
        self.assertTrue(out["ok"], out)


class TestMII(V5Base):
    def test_mii_tiebreak_prefers_money(self):
        # PPI в пределах 10%: побеждает больший MII (money×decidability/ч)
        self.add_hypo("H-001", forecast=10.0, est_hours=2.0,
                      money=0.5, decidability=0.7)   # ppi-лидер (0.294)
        self.add_hypo("H-002", forecast=10.0, est_hours=2.2,
                      money=0.5, decidability=0.9)   # ppi 0.287 — в ничьей, MII выше
        row = queue.pick_next(self.conn)
        self.assertEqual(row["id"], "H-002")   # ppi почти равны → MII решает
        self.assertIn("MII-tiebreak", row["reason"])

    def test_ppi_dominates_mii(self):
        # PPI заметно выше (не 10%-ничьей) — MII не крадёт очередь
        self.add_hypo("H-001", forecast=30.0, est_hours=1.0,
                      signals=9)                    # сильная и дешёвая
        self.add_hypo("H-002", forecast=3.0, est_hours=2.0,
                      money=1.0, decidability=1.0)  # монетизируемая, но слабее
        row = queue.pick_next(self.conn)
        self.assertEqual(row["id"], "H-001")


class TestControversy(V5Base):
    def test_bump_and_dispute_probability(self):
        hid = self.add_hypo()
        crew.bump_controversy(self.conn, hid)
        crew.bump_controversy(self.conn, hid)
        self.assertEqual(crew.controversy_of(self.conn, hid), 2)
        # спорность без hid не падает
        self.assertEqual(crew.controversy_of(self.conn, None), 0)

    def test_dispute_grows_with_controversy(self):
        hid = self.add_hypo()
        for _ in range(6):
            crew.bump_controversy(self.conn, hid)
        disputes = 0
        cfg = seeded_config(dispute_probability=0.3)
        for seed in range(40):
            res = crew.emit("hypo_new", {"hid": hid, "forecast": "10%",
                                         "ppi": "0.5", "hours": "2", "signals": 4},
                            conn=self.conn, config=cfg,
                            rng=random.Random(seed), force=True)
            if any(l.get("event") == "dispute" for l in res["lines"]):
                disputes += 1
        # при controversy 6 спор должен случаться чаще трети событий
        self.assertGreater(disputes, 13)

    def test_high_controversy_no_long_dialogues(self):
        hid = self.add_hypo()
        for _ in range(10):
            crew.bump_controversy(self.conn, hid)
        cfg = seeded_config(dispute_probability=0.0)
        core.set_setting(self.conn, "crew.last.agi_day", core.iso())  # без agi-вставки
        for seed in range(20):
            res = crew.emit("hypo_new", {"hid": hid, "forecast": "10%",
                                         "ppi": "0.5", "hours": "2", "signals": 4},
                            conn=self.conn, config=cfg,
                            rng=random.Random(seed), force=True)
            self.assertLessEqual(len(res["lines"]), 7)   # сцена+бонусы ≤ 7 реплик


class TestPatentWeight(V5Base):
    def test_patent_rows_double_weight(self):
        h_patent = self.add_hypo("H-001", signals=9)   # патент → вес 2
        h_plain1 = self.add_hypo("H-002", signals=3)   # без патента
        h_bad = self.add_hypo("H-003", signals=6)
        with mock.patch.object(core, "emit"), \
             mock.patch.object(crew, "safe_emit"):
            verdict.record(self.conn, h_patent, "confirmed", actual=10.0,
                           seeds_pass="3", seeds_total="3", sigma="0.1",
                           gpu_hours="1.0", changes="ok")
            verdict.record(self.conn, h_plain1, "confirmed", actual=10.0,
                           seeds_pass="3", seeds_total="3", sigma="0.1",
                           gpu_hours="1.0", changes="ok")
            verdict.record(self.conn, h_bad, "rejected", actual=0.0,
                           seeds_pass="0", seeds_total="3", sigma="0.2",
                           gpu_hours="1.0", changes="нет")
        os.makedirs(core.REPORTS_DIR, exist_ok=True)
        patent = os.path.join(core.REPORTS_DIR, "patent-H-001.md")
        with open(patent, "w", encoding="utf-8") as fh:
            fh.write("# p")
        try:
            disc = calib.discrimination(self.conn)
            f = calib.q.signal_score
            # взвешенное среднее good: (2·f(9) + 1·f(3)) / 3 ≠ простому (f(9)+f(3))/2
            expected = round((2 * f(9) + f(3)) / 3, 3)
            naive = round((f(9) + f(3)) / 2, 3)
            self.assertNotEqual(expected, naive)      # проверка сама осмысленна
            self.assertEqual(disc["factors"]["signals"]["mean_good"], expected)
            self.assertEqual(disc["factors"]["signals"]["mean_bad"], f(6))
        finally:
            os.remove(patent)


if __name__ == "__main__":
    unittest.main()
