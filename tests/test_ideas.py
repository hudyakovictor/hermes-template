"""Конвейер идей: таймлайн от старта до конца, в порядке реального потока.

Без сетей и GPU: база и inbox во временном каталоге, доставка замокана.
Порядок воспроизводит жизнь идеи: приём → обсуждение → разбор → очередь или
лог неэффективных → дубли получают ранний отказ с причиной.
"""

import os
import random
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import core      # noqa: E402
import crew      # noqa: E402
import hypo      # noqa: E402
import ideas     # noqa: E402
import inbox     # noqa: E402


GOOD_IDEA = ("Иерархический KV-кэш для длинных контекстов: ускорение 30%%, "
             "статья arxiv 2401.12345 и репо github.com/x/kv — покупатель "
             "вендоры RAG-платформ, метрика latency, экономия на инференсе")
GOOD_LIKE = ("иерархический kv-кэш для длинных контекстов, ускорение, "
             "arxiv 2401.12345, продавать вендорам RAG-платформ")
VAGUE_IDEA = ("надо попробовать как-то улучшить обучение, мне кажется будет лучше")
VAGUE_LIKE = ("попробовать улучшить обучение, мне кажется станет лучше")


class IdeaFlowBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        core.allow_root(self.tmp.name)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home, exist_ok=True)
        self.db_path = os.path.join(self.home, "state.sqlite3")
        self.conn = core.db(self.db_path)
        self.inbox_path = os.path.join(self.home, "inbox.jsonl")
        self.hypo_dir = os.path.join(self.home, "hypotheses")
        os.makedirs(self.hypo_dir, exist_ok=True)
        self._db = core.db
        for ev in ("TELEGRAM_AICHAT_THREAD_ID", "TELEGRAM_CHAT_THREAD_ID",
                   "TELEGRAM_CREW_THREAD_ID", "TELEGRAM_BOT_TOKEN",
                   "TELEGRAM_HOME_CHANNEL"):
            os.environ.pop(ev, None)

    def tearDown(self):
        core.db = self._db
        self.conn.close()
        self.tmp.cleanup()

    def run_flow(self, fn, *args, **kw):
        """Выполнить шаг потока с патченными путями и доставкой."""
        with mock.patch.object(core, "db",
                               side_effect=lambda path=None: self._db(
                                   path or self.db_path)), \
                mock.patch.object(inbox, "INBOX_PATH", self.inbox_path), \
                mock.patch.object(core, "HYPO_DIR", self.hypo_dir), \
                mock.patch.object(crew.tg, "send", return_value={"ok": True}):
            return fn(*args, **kw)

    def chat_events(self) -> list[str]:
        return [r["event"] for r in self.conn.execute(
            "SELECT event FROM crew_chat ORDER BY msg_id")]

    def inbox_states(self) -> dict:
        """Снять inbox можно только с патчем пути — иначе читается живой профиль."""
        with mock.patch.object(inbox, "INBOX_PATH", self.inbox_path):
            return {i["id"]: i["state"] for i in inbox._load()}


class TestTimeline(IdeaFlowBase):
    """Один поток, шаги строго по таймлайну — как в реальной работе."""

    def test_full_timeline(self):
        # ── ШАГ 1 (старт): идея A приходит из бота → inbox + обсуждение
        res = self.run_flow(ideas.submit, GOOD_IDEA)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["inbox_id"], "IN-001")
        self.assertIn("idea_intake", self.chat_events())
        # оценка предварительная: источники в тексте видны
        self.assertGreaterEqual(res["estimate"]["signals"], 2)

        # ── ШАГ 2: разбор A экипажем — цифры и пороги, идея сильная → очередь
        out = self.run_flow(ideas.triage, "IN-001", factors={
            "signals": 4, "money": 0.7, "decidability": 0.8, "forecast": 12.0})
        self.assertEqual(out["verdict"], "queued")
        hid = out["hid"]
        self.assertTrue(hid.startswith("H-"))
        self.assertGreaterEqual(out["pi"], ideas.PI_MIN)
        self.assertGreaterEqual(out["bets"], 2)      # ставки стоят до вердикта
        self.assertIn("idea_review", self.chat_events())
        self.assertIn("idea_queued", self.chat_events())
        row = self.conn.execute("SELECT status FROM hypotheses WHERE id=?",
                                (hid,)).fetchone()
        self.assertEqual(row["status"], "queued")

        # ── ШАГ 3: слабая идея B → inbox
        res_b = self.run_flow(ideas.submit, VAGUE_IDEA)
        self.assertTrue(res_b["ok"])
        self.assertEqual(res_b["inbox_id"], "IN-002")

        # ── ШАГ 4: разбор B — без источников и покупателя → лог неэффективных
        out_b = self.run_flow(ideas.triage, "IN-002")
        self.assertEqual(out_b["verdict"], "rejected")
        self.assertIn("сигналов", out_b["reason"])
        self.assertIn("idea_rejected", self.chat_events())
        logged = self.conn.execute(
            "SELECT reason FROM idea_log WHERE idea_id='IN-002'").fetchone()
        self.assertIn("сигналов", logged["reason"])

        # ── ШАГ 5 (ранняя стадия): дубль сильной идеи → «уже в очереди»
        dup_q = self.run_flow(ideas.submit, GOOD_LIKE)
        self.assertTrue(dup_q["duplicate"])
        self.assertIn(hid, dup_q["reason"])
        self.assertIn("очеред", dup_q["reason"])
        self.assertIn("idea_dup", self.chat_events())
        # дубль не попадает в inbox — его не будут разбирать заново
        states = self.inbox_states()
        self.assertNotIn("IN-003", states)

        # ── ШАГ 6: дубль отклонённой идеи → причина отказа вспоминается
        dup_r = self.run_flow(ideas.submit, VAGUE_LIKE)
        self.assertTrue(dup_r["duplicate"])
        self.assertIn("IN-002", dup_r["reason"])
        self.assertIn("отклон", dup_r["reason"])
        self.assertIn("сигналов", dup_r["reason"])   # причина — та же

        # ── ШАГ 7: агент пытается создать похожую гипотезу — ранний отказ
        with mock.patch.object(core, "db",
                               side_effect=lambda path=None: self._db(
                                   path or self.db_path)), \
                mock.patch.object(crew, "safe_emit"), \
                mock.patch.object(core, "emit"):
            with self.assertRaises(SystemExit):
                hypo.main(["hypo.py", "new", "Попробовать улучшить обучение как-то",
                           "--signals", "3", "--hours", "2"])

        # ── ШАГ 8: лог отражает всю историю решений
        rows = ideas.log_rows(self.conn)
        verdicts = {r["idea_id"]: r["verdict"] for r in rows}
        self.assertEqual(verdicts.get("IN-001"), "queued")
        self.assertEqual(verdicts.get("IN-002"), "rejected")
        self.assertIn("duplicate", verdicts.values())

        # ── ШАГ 9: очередь по-прежнему единственный источник порядка
        live = [r for r in self.conn.execute(
            "SELECT id FROM hypotheses WHERE status='queued'")]
        self.assertTrue(any(r["id"] == hid for r in live))


class TestSimilarity(IdeaFlowBase):
    def test_similarity_bounds(self):
        a = "Иерархический KV-кэш для длинных контекстов ускорение 30"
        self.assertGreaterEqual(ideas.similarity(a, GOOD_IDEA), 0.45)
        self.assertLess(ideas.similarity(a, "расписание дежурств на вторник"), 0.2)
        self.assertEqual(ideas.similarity("", "текст"), 0.0)

    def test_find_duplicates_covers_log_and_hypotheses(self):
        self.run_flow(ideas.submit, GOOD_IDEA)
        self.run_flow(ideas.triage, "IN-001", factors={"signals": 4, "money": 0.7})
        dups = ideas.find_duplicates(self.conn, GOOD_LIKE)
        self.assertTrue(dups)
        # первый матч — сама идея из лога, и она указывает на гипотезу в очереди
        self.assertEqual(dups[0]["kind"], "идея")
        self.assertTrue(dups[0]["hypo_id"].startswith("H-"))


class TestScenes(IdeaFlowBase):
    def test_new_scenes_render_with_numbers(self):
        ctx = {"iid": "IN-001", "title": "кэш", "pi": "0.42", "ppi": "0.21",
               "signals": 4, "money": "0.7", "note": "оценка агента",
               "signals_est": 2, "reason": "сигналов 2 < 3", "dup_id": "IN-002",
               "dup_verdict": "отклонено", "dup_why": "нет покупателя",
               "score": "61%", "hid": "H-001", "forecast": "12%",
               "bets_line": "ставки — за: Крот."}
        for event in ("idea_intake", "idea_review", "idea_queued",
                      "idea_rejected", "idea_dup"):
            lines = crew.render_scene(event, ctx, random.Random(1), None, limit=5)
            self.assertTrue(lines, event)
            joined = " ".join(l["text"] for l in lines)
            self.assertNotIn("{", joined, f"{event}: нераскрытый шаблон")
            self.assertNotIn("None", joined, f"{event}: None в реплике")

    def test_triage_dispute_allowed(self):
        self.assertIn("idea_review", crew.DISPUTE_EVENTS)


if __name__ == "__main__":
    unittest.main()
