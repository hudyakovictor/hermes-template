"""v6: аудит (30 анализов), prior-art источники, изоляция путей, UX CLI.

Offline: сеть замокана, CLI — во временном RESEARCHAGEN_HOME, без GPU/токенов.
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit    # noqa: E402
import core     # noqa: E402
import inbox    # noqa: E402
import priors   # noqa: E402


def cli(home, tool, *args, timeout=40):
    env = dict(os.environ, RESEARCHAGEN_HOME=home,
               TELEGRAM_BOT_TOKEN="", TELEGRAM_HOME_CHANNEL="")
    return subprocess.run([sys.executable, os.path.join(ROOT, "tools", tool), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


class AuditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(self.home, "state"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()


class TestPriors(AuditBase):
    def _fetch_ok(self, url):
        if "arxiv" in url:
            return "<entry><title>T1</title><link href='http://a'/></entry>"
        if "semanticscholar" in url:
            return json.dumps({"data": [{"title": "T", "url": "u"}]})
        if "openalex" in url:
            return json.dumps({"results": [{"display_name": "T", "id": "i"}]})
        if "crossref" in url:
            return json.dumps({"message": {"items": [{"title": "T", "DOI": "d"}]}})
        if "github" in url:
            return json.dumps({"items": [{"full_name": "o/r", "html_url": "h"}]})
        return json.dumps({"results": {"cluster": [{"result": [{"title": "P"}]}]}})

    def test_full_coverage_ok_flag(self):
        cache = os.path.join(self.home, "state", "pc.json")
        with mock.patch.object(priors, "CACHE_PATH", cache), \
                mock.patch.object(core, "ROOT", self.home):
            rep = priors.search("тест", fresh=True, fetch=self._fetch_ok)
        self.assertEqual(rep["coverage"], 1.0)
        self.assertTrue(rep["ok"])
        self.assertEqual(len(rep["sources"]), 6)

    def test_offline_honest_degradation(self):
        def dead(url):
            raise OSError("down")
        cache = os.path.join(self.home, "state", "pc.json")
        with mock.patch.object(priors, "CACHE_PATH", cache), \
                mock.patch.object(core, "ROOT", self.home):
            rep = priors.search("тест 2", fresh=True, fetch=dead)
        self.assertEqual(rep["coverage"], 0.0)
        self.assertFalse(rep["ok"])
        self.assertIn("нельзя", rep["verdict"])

    def test_coverage_below_planck_not_ok(self):
        def half(url):
            if "arxiv" in url:
                return "<entry><title>T</title></entry>"
            raise OSError("down")
        cache = os.path.join(self.home, "state", "pc.json")
        with mock.patch.object(priors, "CACHE_PATH", cache), \
                mock.patch.object(core, "ROOT", self.home):
            rep = priors.search("тест 3", fresh=True, fetch=half)
        self.assertEqual(rep["coverage"], round(1 / 6, 2))
        self.assertFalse(rep["ok"])

    def test_empty_query(self):
        rep = priors.search("   ")
        self.assertFalse(rep["ok"])


class TestSafePath(AuditBase):
    def test_inside_root_allowed(self):
        p = core.safe_path("state/x.db")
        self.assertTrue(p.startswith(core.ROOT))

    def test_outside_root_refused(self):
        # собственные каталоги профиля (state/, memory/) — легальны;
        # наружу (к памяти основного агента, системным файлам) — отказ
        for bad in ("/tmp/evil.db", "../../etc/passwd", "../sibling.json"):
            with self.assertRaises(PermissionError):
                core.safe_path(bad)

    def test_to_number_rejects_nonfinite(self):
        for bad in ("NaN", "inf", "-inf", float("nan")):
            with self.assertRaises(ValueError):
                core.to_number(bad, "поле")
        self.assertEqual(core.to_number("2.5", "поле"), 2.5)


class TestInboxSanitize(AuditBase):
    def test_control_chars_and_length(self):
        raw = "идея\x00\x1b[2j про\u00a0кэш\t\tи   пробелы"
        clean = inbox.sanitize(raw)
        self.assertNotIn("\x00", clean)
        self.assertNotIn("\x1b", clean)
        self.assertEqual(clean, " ".join(clean.split()))
        self.assertLessEqual(len(clean), 4000)

    def test_add_marks_untrusted(self):
        with mock.patch.object(inbox, "INBOX_PATH",
                               os.path.join(self.home, "inbox.jsonl")), \
             mock.patch.object(core, "ROOT", self.home), \
             mock.patch.object(core, "ensure_dirs"), \
             mock.patch.object(core, "log_event"):
            item = inbox.add("  ссылка на статью  ")
        self.assertFalse(item["trusted"])
        self.assertEqual(item["text"], "ссылка на статью")


class TestCliUx(AuditBase):
    def test_help_everywhere(self):
        tools = [x for x in os.listdir(os.path.join(ROOT, "tools"))
                 if x.endswith(".py") and x not in ("audit.py", "crew_sim.py")]
        for tool in sorted(tools):
            p = cli(self.home, tool, "--help", timeout=30)
            blob = p.stdout + p.stderr
            self.assertNotIn("Traceback", blob, tool)
            self.assertNotIn("неизвестная команда", blob, tool)

    def test_queue_rejects_nan_gracefully(self):
        p = cli(self.home, "queue.py", "add", "X", "--hours", "NaN")
        blob = p.stdout + p.stderr
        self.assertNotIn("Traceback", blob)
        self.assertNotIn("Добавлено", blob)
        self.assertIn("конечным", blob)

    def test_queue_rejects_inf_forecast(self):
        p = cli(self.home, "queue.py", "add", "Y", "--forecast", "inf")
        self.assertNotIn("Добавлено", p.stdout)

    def test_governor_report_needs_file_hint(self):
        p = cli(self.home, "governor.py", "report")
        blob = p.stdout + p.stderr
        self.assertNotIn("Errno 2", blob)
        self.assertIn("--file", blob)

    def test_rg_typo_hint(self):
        p = cli(self.home, "rg.py", "queu")
        blob = p.stdout + p.stderr
        self.assertIn("queue", blob)

    def test_rg_new_routes(self):
        for cmd in (("priors", "sources"), ("inbox", "list"), ("audit", "--no-coverage")):
            p = cli(self.home, "rg.py", *cmd, timeout=180)
            self.assertNotIn("Traceback", p.stdout + p.stderr, cmd)


class TestAuditSuite(unittest.TestCase):
    def test_30_analyses_registered(self):
        self.assertEqual(len(audit.ANALYSES), 80)
        ids = [a[0] for a in audit.ANALYSES]
        self.assertEqual(len(set(ids)), 80)

    def test_run_all_green(self):
        report = audit.run_all(with_coverage=False)
        self.assertEqual(report["fails"], 0, report["top_errors"])
        self.assertEqual(report["analyses"], 80)


if __name__ == "__main__":
    unittest.main()
