"""Сигнал-банк: двусторонняя память истории.

Сторона 1 — не рассматривать проверенное заново: заголовок опровергнутой
гипотезы возвращается предупреждением. Сторона 2 — signals выживают гипотезу:
claims непрошедшей карточки помечаются reusable и предлагаются как блоки для
новых идей, включая данные тестов.
"""
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import core          # noqa: E402
import crew          # noqa: E402
import hypo          # noqa: E402
import ideas         # noqa: E402
import queue as q    # noqa: E402
import verdict as v  # noqa: E402

GOOD_CARD = """# H-001 — тест
id: H-001
title: "тест"
signal_chain:
  - id: A
    claim: "градиентный шум растёт до переобучения на двух архитектурах"
    source: "x"
mechanism: |
  цепочка
"""


class BankBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        core.allow_root(self.tmp.name)
        self.db_path = os.path.join(self.tmp.name, "s.db")
        self.real_db = core.db
        real = self.real_db
        patcher = mock.patch.object(
            core, "db", side_effect=lambda path=None: real(path or self.db_path))
        patcher.start(); self.addCleanup(patcher.stop)
        self.inbox_path = os.path.join(self.tmp.name, "inbox.jsonl")
        self.hypo_dir = os.path.join(self.tmp.name, "hypo")
        os.makedirs(self.hypo_dir, exist_ok=True)
        import inbox
        for mod, attr, val in ((inbox, "INBOX_PATH", self.inbox_path),
                               (core, "HYPO_DIR", self.hypo_dir)):
            p = mock.patch.object(mod, attr, val); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(crew.tg, "send", return_value={"ok": True})
        p.start(); self.addCleanup(p.stop)
        self.conn = core.db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def mk_hypo(self, hid="H-900", title="градиентный шум как ранний триггер"):
        self.conn.execute(
            "INSERT INTO hypotheses (id, title, status, level, source, signals,"
            " novelty, early_pct, standard, money, decidability, est_hours,"
            " forecast, forecast_low, forecast_high, p_repro, base_rate,"
            " demand_signals, created_at, updated_at, card_path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),"
            " datetime('now'),?)",
            (hid, title, "queued", "L0", "dr", 4, 0.7, 4, 0.5, 0.6, 0.8, 2,
             12, 8, 16, 0.5, 0.35, 3, os.path.join(self.hypo_dir, f"{hid}.yaml")))
        self.conn.commit()
        with open(os.path.join(self.hypo_dir, f"{hid}.yaml"), "w",
                  encoding="utf-8") as fh:
            fh.write(GOOD_CARD.replace("H-001", hid).replace("тест", title))
        q.update_fields(self.conn, hid,
                        card_path=os.path.join(self.hypo_dir, f"{hid}.yaml"))


class TestBank(BankBase):
    def test_verdict_rejected_keeps_signals_reusable(self):
        """Гипотеза опровергнута — заголовок refuted, сигналы карточки живы."""
        self.mk_hypo()
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, dry_run)"
            " VALUES ('H-900','L0','done',?,0)", (core.iso(),))
        self.conn.commit()
        v.record(self.conn, "H-900", "rejected", actual=2, seeds_pass=1,
                 seeds_total=3, gpu_hours=1.0, changes="масштаб не пережил")
        rows = {r["claim"]: r["outcome"] for r in self.conn.execute(
            "SELECT claim, outcome FROM signal_bank WHERE hid='H-900'")}
        self.assertIn("refuted", rows.values())
        self.assertTrue(any(o == "reusable" for o in rows.values()),
                        f"сигналы должны остаться: {rows}")

    def test_new_idea_gets_refuted_warning_and_reusable_hint(self):
        """Сторона 1: опровергнутое — предупреждение; сторона 2: живой сигнал."""
        self.mk_hypo()
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, dry_run)"
            " VALUES ('H-900','L0','done',?,0)", (core.iso(),))
        self.conn.commit()
        v.record(self.conn, "H-900", "rejected", actual=2, seeds_pass=1,
                 seeds_total=3, gpu_hours=1.0, changes="масштаб не пережил")
        # идея, похожая на живой сигнал (claims) — не дубликат, а подсказка
        res = ideas.submit("если на двух архитектурах шум градиентов растёт "
                           "до переобучения, это дешёвый триггер", source="telegram")
        self.assertTrue(res["ok"])
        self.assertIn("signal_matches", res)
        outcomes = {m["outcome"] for m in res["signal_matches"]}
        self.assertIn("reusable", outcomes)
        # сцена экипажа: Хроник узнаёт сигнал
        ev = [r["event"] for r in self.conn.execute(
            "SELECT event FROM crew_chat").fetchall()]
        self.assertIn("signal_recall", ev)

    def test_kill_before_gpu_writes_reusable(self):
        """Снятие до GPU: сигнал не опровергнут — остаётся в банке."""
        self.mk_hypo(hid="H-901")
        argv = ["hypo.py", "kill", "H-901", "--why", "нет контроля",
                "--lesson", "проверять контроль"]
        with mock.patch.object(core, "db",
                               side_effect=lambda path=None: self.conn), \
             mock.patch.object(sys, "argv", argv):
            hypo.main(argv)
        rows = [r["outcome"] for r in self.conn.execute(
            "SELECT outcome FROM signal_bank WHERE hid='H-901'")]
        self.assertTrue(rows and set(rows) == {"reusable"})

    def test_confirmed_verdict_marks_claims_confirmed(self):
        self.mk_hypo(hid="H-902")
        self.conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, dry_run)"
            " VALUES ('H-902','L0','done',?,0)", (core.iso(),))
        self.conn.commit()
        v.record(self.conn, "H-902", "confirmed", actual=12, seeds_pass=3,
                 seeds_total=3, gpu_hours=1.0, changes="контроль добавлен")
        rows = {r["outcome"] for r in self.conn.execute(
            "SELECT outcome FROM signal_bank WHERE hid='H-902'")}
        self.assertEqual(rows, {"confirmed"})


class TestMicroFactors(unittest.TestCase):
    def test_20_factors_present(self):
        self.assertEqual(len(crew.MICRO_FACTORS), 20)
        self.assertTrue(all(len(f) > 10 for f in crew.MICRO_FACTORS))

    def test_recall_mostly_constructive_and_never_boss(self):
        rng = random.Random(3)
        kinds = []
        for _ in range(600):
            r = crew.render_recall(rng, {})
            self.assertNotEqual(r["agent"], "shef")
            kinds.append(r["recall_kind"])
        share = sum(1 for k in kinds if k != "neutral") / len(kinds)
        self.assertGreater(share, 0.6, share)   # ~70% конструктива
        self.assertLess(share, 0.8, share)
        self.assertTrue(set(kinds) >= {"remind", "fix", "admit", "neutral"})


if __name__ == "__main__":
    unittest.main()
