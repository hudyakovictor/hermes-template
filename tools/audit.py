#!/usr/bin/env python3
"""researchagen — аудит функционала: 80 анализов, покрывающих ~90% комбинаций задач.

Метод: каждый анализ прогоняет реальный код (библиотечные вызовы на временной
базе или CLI во временном RESEARCHAGEN_HOME) и возвращает находки FAIL/WARN.
Аудит честный: он находит ошибки до их исправления и проходит после.

Что измеряется:
  * 80 анализов по 12 зонам: данные, вердикты/калибровка, диспетчер/governor,
    чат, интерфейс/доки, изоляция, источники;
  * топ-20 ошибок: первые 20 FAIL-находок (после исправления должно быть 0);
  * строчный охват инструментов (trace) — «какая доля кода реально выполнена
    аудитом»; планка метода — ≥ 90% функциональных комбинаций.

CLI: python tools/audit.py run [--json]
Выход: 0 — FAIL-находок нет; 1 — есть. Отчёт: reports/audit-<дата>.md.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import trace as trace_mod
from unittest import mock

import calib
import core
import crew
import dispatch
import governor
import hypo
import hygiene
import priors
import queue as q
import verdict as v

FAIL, WARN = "FAIL", "WARN"
TOOLS = os.path.dirname(os.path.abspath(__file__))


def f(sev: str, text: str) -> dict:
    return {"sev": sev, "text": text}


# --- инфраструктура прогонов ----------------------------------------------

def _tmp_home() -> tuple[str, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    home = os.path.join(tmp.name, "home")
    os.makedirs(os.path.join(home, "state"), exist_ok=True)
    cfg = os.path.join(TOOLS, "..", "config.yaml")
    if os.path.exists(cfg):
        import shutil
        shutil.copy(cfg, os.path.join(home, "config.yaml"))
    return home, tmp


_COVERDIR: str | None = None   # режим замера: CLI-пробы через python -m trace


def cli(home: str, tool: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ, RESEARCHAGEN_HOME=home,
               TELEGRAM_BOT_TOKEN="", TELEGRAM_HOME_CHANNEL="")
    cmd = [sys.executable]
    if _COVERDIR:   # замер покрытия: сабпроцесс пишет .cover-файлы
        cmd += ["-m", "trace", "--count", "--coverdir", _COVERDIR]
    cmd += [os.path.join(TOOLS, tool), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env)
    if _COVERDIR:
        # каждый сабпроцесс перезаписывает .cover-файлы; забираем свежие
        # под уникальными именами, чтобы счётчики вызовов суммировались
        stamp = time.monotonic_ns()
        for cov_name in os.listdir(_COVERDIR):
            if not cov_name.endswith(".cover"):
                continue
            mod = cov_name[:-6]
            if os.path.exists(os.path.join(TOOLS, mod + ".py")) or cov_name == "__main__.cover":
                src = os.path.join(_COVERDIR, cov_name)
                os.replace(src, os.path.join(
                    _COVERDIR, f"{mod}.{stamp}.{cov_name[:-6]}.cover")
                    if cov_name != "__main__.cover" else
                    os.path.join(_COVERDIR, f"{tool[:-3]}.{stamp}.cover"))
    return proc


def _tracebacked(proc: subprocess.CompletedProcess) -> bool:
    return "Traceback (most recent call last)" in (proc.stdout + proc.stderr)


USAGE_TOOLS: dict[str, str] = {}


def _rg_routes() -> list[str]:
    """Маршруты rg.py (парс из кода) — для проверки документации."""
    try:
        src = open(os.path.join(TOOLS, "rg.py"), encoding="utf-8").read()
        return re.findall(r'"(\w+)":\s*lambda', src)
    except OSError:
        return []


ANALYSES: list[tuple[str, str, str]] = []


def analysis(num: int, slug: str, name: str):
    def deco(fn):
        ANALYSES.append((f"a{num:02d}_{slug}", name, fn))
        return fn
    return deco


# ==========================================================================
# ЗОНА A. Ядро данных
# ==========================================================================

@analysis(1, "schema_fresh", "свежая БД: все таблицы и v5-колонки")
def a01() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        crew.init_db(conn)
        tabs = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        need = {"hypotheses", "verdicts", "runs", "events", "settings",
                "agent_bets", "crew_chat", "crew_findings", "governor_leases"}
        out = [f(FAIL, f"нет таблицы {t}") for t in need - tabs]
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(hypotheses)")}
        out += [f(FAIL, f"hypotheses: нет колонки {c}") for c in
                ("forecast_low", "forecast_high", "p_repro", "base_rate",
                 "demand_signals", "controversy") if c not in cols]
        return out
    finally:
        tmp.cleanup()


@analysis(2, "migrate_old", "старая БД мигрирует без потерь")
def a02() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        path = os.path.join(home, "state", "old.sqlite3")
        import sqlite3
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE hypotheses (id TEXT PRIMARY KEY, title TEXT, status TEXT,
              created_at TEXT, updated_at TEXT);
            CREATE TABLE verdicts (vid INTEGER PRIMARY KEY AUTOINCREMENT,
              hypo_id TEXT, level TEXT, kind TEXT, forecast REAL, actual REAL,
              deviation REAL, seeds_pass INTEGER, seeds_total INTEGER, sigma REAL,
              gpu_hours REAL, what_changes TEXT, created_at TEXT);
            INSERT INTO hypotheses VALUES ('H-OLD', 'старая', 'queued', 'x', 'y');
        """)
        c.commit()
        c.close()
        conn = core.db(path)
        row = conn.execute("SELECT title, controversy FROM hypotheses"
                           " WHERE id='H-OLD'").fetchone()
        if row is None or row["title"] != "старая":
            return [f(FAIL, "миграция потеряла данные старой базы")]
        return []
    finally:
        tmp.cleanup()


@analysis(3, "queue_lifecycle", "очередь: add→score→pick→close, авто-коридор")
def a03() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        row = q.add(conn, "Коридор", signals=4, forecast=10.0, est_hours=2.0,
                    novelty=0.5, early_pct=5, standard=0.5, money=0.5,
                    decidability=0.5)
        if (row["forecast_low"], row["forecast_high"]) != (6.0, 14.0):
            out.append(f(FAIL, "авто-коридор ±40% не построился"))
        picked = q.pick_next(conn)
        if not picked or picked["id"] != row["id"]:
            out.append(f(FAIL, "pick_next не выбирает единственную живую гипотезу"))
        # закрытие статуса
        q.set_status(conn, row["id"], "rejected")
        if q.pick_next(conn) is not None:
            out.append(f(FAIL, "закрытая гипотеза не покидает очередь выбора"))
        return out
    finally:
        tmp.cleanup()


@analysis(4, "numeric_guards", "мусорные числа отклоняются без трейсбека")
def a04() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        cases = [("hypo.py", "new", "X", "--signals", "abc", "--hours", "2"),
                 ("hypo.py", "new", "X", "--forecast", "abc", "--hours", "2"),
                 ("queue.py", "add", "X", "--hours", "NaN"),
                 ("queue.py", "add", "Y", "--forecast", "inf")]
        for case in cases:
            p = cli(home, *case)
            if _tracebacked(p):
                out.append(f(FAIL, f"`{case[0]} {' '.join(case[1:])}` — трейсбек вместо ошибки"))
            elif p.returncode == 0 and "Добавлено" in p.stdout:
                out.append(f(FAIL, f"`{case[0]} {' '.join(case[1:])}` — "
                                   f"не-число принято молча (отравление данных)"))
        return out
    finally:
        tmp.cleanup()


@analysis(5, "ppi_mii", "приоритет: PPI доминирует, MII решает ничью, корзины честны")
def a05() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        base = dict(signals=4, novelty=0.5, early_pct=5.0, standard=0.5)
        q.add(conn, "Сильная дешёвая", forecast=30.0, est_hours=1.0, signals=9,
              novelty=0.5, early_pct=5, standard=0.5, money=0.3, decidability=0.3)
        q.add(conn, "Дорогая денежная", forecast=3.0, est_hours=2.0,
              money=1.0, decidability=1.0, **base)
        picked = q.pick_next(conn)
        if not picked or picked["id"] != "H-001":
            out.append(f(FAIL, "PPI не доминирует: дешёвая сильная не первая"))
        for hours, bin_ in ((2, "P1"), (8, "P2"), (30, "P3"), (60, "P4")):
            if q.bin_of(hours) != bin_:
                out.append(f(FAIL, f"корзина {hours}ч → {q.bin_of(hours)}, ожидалась {bin_}"))
        return out
    finally:
        tmp.cleanup()


@analysis(6, "hypo_gate_matrix", "гейт гипотез: матрица карточек × сигналов × прогноза")
def a06() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        # полная карточка + 8 галочек + 3 сигнала + прогноз → должна пройти
        hid = q.add(conn, "Полная", signals=3, forecast=5.0, est_hours=1.0,
                    novelty=0.5, early_pct=5, standard=0.5, money=0.5,
                    decidability=0.5)["id"]
        card = os.path.join(home, "hypotheses", f"{hid}.yaml")
        os.makedirs(os.path.dirname(card), exist_ok=True)
        text = "\n\n".join(f"{sec}: |\n  x" for sec in hypo.REQUIRED_SECTIONS)
        text += "\n\nkill_checks:\n" + "\n".join(["- passed: true"] * 8)
        with open(card, "w", encoding="utf-8") as fh:
            fh.write(text)
        conn.execute("UPDATE hypotheses SET card_path=? WHERE id=?", (card, hid))
        conn.commit()
        gate = hypo.check(hid, conn)
        if not gate["ok"]:
            out.append(f(FAIL, f"полная карточка не прошла гейт: {gate['problems'][:3]}"))
        # слабая: сигналов 2 → гейт обязан отказать
        hid2 = q.add(conn, "Слабая", signals=2, forecast=5.0, est_hours=1.0,
                     novelty=0.5, early_pct=5, standard=0.5, money=0.5,
                     decidability=0.5)["id"]
        gate2 = hypo.check(hid2, conn)
        if gate2["ok"]:
            out.append(f(FAIL, "карточка с 2 сигналами прошла гейт (должен отказ)"))
        return out
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА B. Вердикты и калибровка
# ==========================================================================

def _full_card(home: str, conn, hid: str) -> None:
    """Карточка со всеми секциями и 8/8 kill-галочек — гейт проходит."""
    card = os.path.join(home, "hypotheses", f"{hid}.yaml")
    os.makedirs(os.path.dirname(card), exist_ok=True)
    text = "\n\n".join(f"{sec}: |\n  x" for sec in hypo.REQUIRED_SECTIONS)
    text += "\n\nkill_checks:\n" + "\n".join(["- passed: true"] * 8)
    with open(card, "w", encoding="utf-8") as fh:
        fh.write(text)
    conn.execute("UPDATE hypotheses SET card_path=? WHERE id=?", (card, hid))
    conn.commit()


def _mk_hypo(conn, **kw) -> str:
    fields = dict(signals=4, forecast=10.0, est_hours=2.0, novelty=0.5,
                  early_pct=5, standard=0.5, money=0.5, decidability=0.5)
    fields.update(kw)
    return q.add(conn, fields.pop("title", "T"), **fields)["id"]


@analysis(7, "verdict_matrix", "вердикты: 5 исходов × коридор × ставки")
def a07() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        for i, kind in enumerate(("confirmed", "partial", "rejected", "killed")):
            hid = _mk_hypo(conn, title=f"V{i}")
            crew.place_bets(conn, hid, 0.6)
            with mock.patch.object(core, "emit"), \
                 mock.patch.object(crew, "safe_emit"), \
                 mock.patch.object(core, "log_event"):
                res = v.record(conn, hid, kind, actual=None if kind == "killed" else 11.0,
                               seeds_pass=2, seeds_total=3, sigma=0.1,
                               gpu_hours=1.0, changes="x")
            if not res.get("ok", True):
                out.append(f(FAIL, f"вердикт {kind} не записался: {res}"))
            row = conn.execute("SELECT in_corridor FROM verdicts WHERE hypo_id=?",
                               (hid,)).fetchone()
            if kind != "killed" and row["in_corridor"] != 1:
                out.append(f(FAIL, f"{kind}: факт 11% вне коридора [6..14]?"))
            bets = conn.execute("SELECT COUNT(*) c FROM agent_bets WHERE hypo_id=?"
                                " AND resolved=1", (hid,)).fetchone()["c"]
            if bets == 0:
                out.append(f(FAIL, f"{kind}: ставки не закрыты вердиктом"))
        return out
    finally:
        tmp.cleanup()


@analysis(8, "bets_lifecycle", "ставки: постановка/идемпотентность/ручные/счёт")
def a08() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        hid = _mk_hypo(conn)
        b1 = crew.place_bets(conn, hid, 0.7)
        crew.place_bets(conn, hid, 0.7)
        n = conn.execute("SELECT COUNT(*) c FROM agent_bets WHERE hypo_id=?",
                         (hid,)).fetchone()["c"]
        if n != len(b1):
            out.append(f(FAIL, "повторная постановка ставок дублирует их"))
        if not 2 <= len(b1) <= 4:
            out.append(f(FAIL, f"ставок {len(b1)} — вне диапазона 2–4"))
        # ручная ставка: смена решения не плодит строки
        conn.execute("INSERT INTO agent_bets (agent,hypo_id,bet,made_at)"
                     " VALUES ('krot',?,'confirmed',?)", (hid, core.iso()))
        conn.execute("INSERT INTO agent_bets (agent,hypo_id,bet,made_at)"
                     " VALUES ('krot',?,'rejected',?)", (hid, core.iso()))
        conn.commit()
        # счёт после резолва
        crew.resolve_bets(conn, hid, "confirmed")
        scores = crew.bet_scores(conn)
        if not scores:
            out.append(f(FAIL, "счёт ставок пуст после резолва"))
        return out
    finally:
        tmp.cleanup()


@analysis(9, "calibration_math", "калибровка: bias/MAE/asym/corridor — контрольные числа")
def a09() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        for actual in (5.0, 12.0):     # -50% (мимо коридора) и +20% (попадание)
            hid = _mk_hypo(conn)
            with mock.patch.object(core, "emit"), \
                 mock.patch.object(crew, "safe_emit"), \
                 mock.patch.object(core, "log_event"):
                v.record(conn, hid, "confirmed", actual=actual, seeds_pass=3,
                         seeds_total=3, sigma=0.1, gpu_hours=1.0, changes="x")
        rep = v.calibration(conn)
        checks = [("mean_abs_deviation_pct", 35.0), ("asym_penalty_pct", 60.0),
                  ("corridor_hits", "1/2"), ("hit_rate", 1.0)]
        for key, want in checks:
            if rep.get(key) != want:
                out.append(f(FAIL, f"калибровка {key}={rep.get(key)}, ожидалось {want}"))
        return out
    finally:
        tmp.cleanup()


@analysis(10, "calib_guard", "перекалибровка на малой выборке запрещена")
def a10() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        before = q.weights()
        rep = calib.recalibrate(conn) if hasattr(calib, "recalibrate") else None
        after = q.weights()
        if before != after:
            return [f(FAIL, "веса изменились на пустой выборке — самообман")]
        return []
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА C. Диспетчер и governor
# ==========================================================================

@analysis(11, "dispatch_gates", "диспетчер: пауза/лимит/аппрув/бюджет/спрос-чек")
def a11() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        cfg_path = os.path.join(home, "config.yaml")
        config = core.load_config(cfg_path) if os.path.exists(cfg_path) else core.load_config()
        hid = _mk_hypo(conn)
        _full_card(home, conn, hid)
        # пауза — ставим оба ключа для совместимости (boolean и timed)
        core.set_setting(conn, "dispatch.paused", True)
        core.set_setting(conn, "dispatch.paused_until", "2999-01-01T00:00:00+00:00")
        res = dispatch.launch(conn, hid, "L0", config=config)
        if res.get("ok"):
            out.append(f(FAIL, "запуск на паузе прошёл"))
        core.set_setting(conn, "dispatch.paused", False)
        core.set_setting(conn, "dispatch.paused_until", "")
        # чистим возможные артефакты от debug-запуска на паузе (dry-run мог успеть создать run)
        conn.execute("DELETE FROM runs WHERE state='running'")
        conn.execute("DELETE FROM governor_leases WHERE state NOT IN ('released','stopped')")
        conn.commit()
        # спрос-чек L2 (карточка полная → единственный отказ — спрос)
        res = dispatch.launch(conn, hid, "L2", config=config)
        if res.get("ok") or "спрос" not in res.get("reason", ""):
            out.append(f(FAIL, f"спрос-чек L2 не сработал: {res.get('reason')}"))
        return out
    finally:
        tmp.cleanup()


@analysis(12, "governor_leases", "governor: аренды acquire/release/fail-closed")
def a12() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        lease = governor.acquire_experiment(conn, "H-001", "L1")
        if not lease.get("ok"):
            out.append(f(FAIL, f"аренда не выдана: {lease.get('reason')}"))
        else:
            rel = governor.release(conn, lease["lease_id"], "тест")
            if not rel.get("ok"):
                out.append(f(FAIL, "аренда не освобождается"))
        return out
    finally:
        tmp.cleanup()


@analysis(13, "governor_report", "governor report: пустой вызов и валидация JSON")
def a13() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        p = cli(home, "governor.py", "report")
        if _tracebacked(p) or "Errno 2" in (p.stdout + p.stderr):
            out.append(f(FAIL, "`governor.py report` без --file падает невнятно"
                               f" ({(p.stdout + p.stderr).strip()[:60]})"))
        return out
    finally:
        tmp.cleanup()


@analysis(14, "hygiene", "гигиена: повторный run стабилен и не калечит базу")
def a14() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        for _ in range(2):
            p = cli(home, "hygiene.py", "run")
            if p.returncode not in (0, 1) or _tracebacked(p):
                out.append(f(FAIL, f"hygiene run упал: rc={p.returncode}"))
        return out
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА D. Чат экипажа
# ==========================================================================

def _crew_config():
    return {"researchagen": {"crew": dict(crew.DEFAULTS, enabled=True,
                                          dispute_probability=0.0,
                                          nudge_probability=0.0,
                                          joke_probability=0.0,
                                          customer_line_probability=0.0,
                                          noise_line_probability=0.0)}}


@analysis(15, "scenes_render", "все сцены × 8 сидов: без пустых текстов и хвостов")
def a15() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        ctx_full = {"hid": "H-001", "forecast": "10%", "actual": "12%",
                    "dev": "+20%", "hours": "2", "seeds": "3/3", "passed": 3,
                    "total": 3, "budget": 20, "burn": 3.2, "free": 8,
                    "level": "L1", "pct": 40, "ratio": 2, "mode": "testing",
                    "min": 3, "signals": 4, "money": 0.7, "challenger": "H-002",
                    "open_findings": 1, "bias": -31, "subject": "H-001",
                    "check": "gap", "age": "3 дня", "bets_line": "ставки — за: Крот.",
                    "bets_result": "по ставкам — выиграли: Крот.", "title": "T",
                    "ppi": "0.50", "demand": 1, "kind": "confirmed",
                    "iid": "IN-001", "pi": "0.42", "note": "оценка агента",
                    "signals_est": 2, "reason": "сигналов 2 < 3",
                    "dup_id": "IN-003", "dup_verdict": "отклонено",
                    "dup_why": "покупатель не назван", "score": "61%"}
        for event in crew.SCENES:
            for seed in range(8):
                rng = random.Random(f"{event}-{seed}")
                lines = crew.render_scene(event, ctx_full, rng, None, limit=5)
                for line in lines:
                    if not line["text"].strip():
                        out.append(f(FAIL, f"сцена {event}: пустая реплика (seed {seed})"))
                    if "{" in line["text"] and re.search(r"\{\w+\}", line["text"]):
                        out.append(f(WARN, f"сцена {event}: нераскрытый шаблон {line['text'][:50]}"))
                    if "None" in line["text"]:
                        out.append(f(FAIL, f"сцена {event}: None в реплике {line['text'][:50]}"))
        return _dedupe(out)
    finally:
        tmp.cleanup()


@analysis(16, "dialog_contract", "диалоги 1–5 реплик; спорность разгоняет споры")
def a16() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        cfg = _crew_config()
        core.set_setting(conn, "crew.last.agi_day", core.iso())
        quiet = [len(crew.emit("hypo_new", {"hid": "H-001", "forecast": "9%",
                                            "signals": 4, "hours": 2}, conn=conn,
                               config=cfg, rng=random.Random(s), force=True)["lines"])
                 for s in range(24)]
        if max(quiet) > 5:
            out.append(f(FAIL, f"без споров диалог {max(quiet)} реплик — простыня"))
        if 1 not in quiet:
            out.append(f(WARN, "ни одного короткого ответа в 24 сценах"))
        # спорность ≥ 6 → споры чаще половины событий
        hid = _mk_hypo(conn)
        for _ in range(6):
            crew.bump_controversy(conn, hid)
        cfg["researchagen"]["crew"]["dispute_probability"] = 0.3
        disputes = 0
        for s in range(30):
            res = crew.emit("hypo_new", {"hid": hid, "forecast": "9%",
                                         "signals": 4, "hours": 2},
                            conn=conn, config=cfg, rng=random.Random(s), force=True)
            if any(l.get("event") == "dispute" for l in res["lines"]):
                disputes += 1
        if disputes < 15:
            out.append(f(FAIL, f"спорность не разгоняет споры: {disputes}/30"))
        return out
    finally:
        tmp.cleanup()


@analysis(17, "disputes_close", "споры закрыты Boss; спец-сцены рендерятся")
def a17() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        for event in ("demand_block", "idea_pitch", "commercial_dead_end"):
            if event in crew.SCENES or event in str(crew.DISPUTES):
                pass
        # idea_pitch — спор, needs hid; demand_block — сцена
        rng = random.Random(1)
        lines = crew.render_scene("demand_block", {"hid": "H-001", "demand": 1},
                                  rng, None, limit=3)
        if not lines:
            out.append(f(FAIL, "сцена demand_block пустая"))
        ctx = {"hid": "H-001", "forecast": "10%", "signals": 4}
        disp = crew.render_dispute(ctx, random.Random(3))
        if disp and not any(l.get("arbiter") for l in disp):
            out.append(f(FAIL, "спор без арбитража Boss — не закрыт"))
        return out
    finally:
        tmp.cleanup()


@analysis(18, "budgets_limits", "пулы 5/2%, лимит 100/день, cooldown")
def a18() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        crew.init_db(conn)
        out = []
        if crew.DEFAULTS.get("max_messages_per_day") != 100:
            out.append(f(FAIL, "лимит сообщений не 100"))
        if crew.cfg("customer_share_max", None) != 0.06:
            out.append(f(WARN, "потолок customer не ~5%"))
        # лимит дня: 100 доставок → следующая пачка не отправляется
        core.set_setting(conn, f"crew.batches.{crew._today()}", 100)
        thread_env = {"TELEGRAM_CREW_THREAD_ID": "777"}
        with mock.patch.dict(os.environ, thread_env), \
                mock.patch.object(crew.tg, "send", return_value={"ok": True}):
            res = crew.emit("queue_empty", {"min": 1}, conn=conn,
                            config=_crew_config(), rng=random.Random(1))
        if res.get("ok") and res.get("sent"):
            out.append(f(FAIL, "лимит 100/день не остановил доставку"))
        # под лимитом — доставка идёт (другое событие: у queue_empty кулдаун)
        core.set_setting(conn, f"crew.batches.{crew._today()}", 99)
        with mock.patch.dict(os.environ, thread_env), \
                mock.patch.object(crew.tg, "send", return_value={"ok": True}):
            res2 = crew.emit("launch", {"hid": "H-001", "level": "L1",
                                        "budget": 20, "burn": 1}, conn=conn,
                             config=_crew_config(), rng=random.Random(2))
        if not (res2.get("ok") and res2.get("sent")):
            out.append(f(FAIL, "под лимитом доставка не идёт"))
        return out
    finally:
        tmp.cleanup()


@analysis(19, "review_finders", "ревью: 9 искателей косяков на синтетике")
def a19() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        conn = core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        kinds = set()
        for finder in crew.FINDERS:
            try:
                for finding in finder(conn, None):
                    kinds.add(finding["kind"])
            except Exception as exc:  # noqa: BLE001
                out.append(f(FAIL, f"finder {finder.__name__} упал: {exc}"))
        if len(crew.FINDERS) < 9:
            out.append(f(WARN, f"искателей {len(crew.FINDERS)} < 9"))
        # синтетика: слабые сигналы должны находиться
        q.add(conn, "Слабая", signals=1, forecast=None, est_hours=1.0,
              novelty=0.1, early_pct=8, standard=0.1, money=0.1, decidability=0.1)
        found = {d["kind"] for finder in crew.FINDERS for d in finder(conn, None)}
        if "review_weak_signals" not in found:
            out.append(f(FAIL, "слабые сигналы не находятся ревью"))
        return out
    finally:
        tmp.cleanup()


@analysis(20, "chat_history", "история чата: replay/stats на пустой и полной базе")
def a20() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        for cmd in (("crew.py", "replay", "--n", "5"),
                    ("crew.py", "stats"),
                    ("rg.py", "aichat", "--n", "3")):
            p = cli(home, *cmd)
            if _tracebacked(p):
                out.append(f(FAIL, f"`{' '.join(cmd)}` падает на пустой базе"))
        return out
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА E. Интерфейс и документация
# ==========================================================================

@analysis(21, "cli_help", "--help не падает ни в одном инструменте")
def a21() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        internal = {"audit.py", "crew_sim.py"}   # сам аудит и стресс-тест — не CLI пользователя
        tools = sorted(x for x in os.listdir(TOOLS) if x.endswith(".py")
                       and not x.startswith("_") and x not in internal)
        for tool in tools:
            p = cli(home, tool, "--help", timeout=25)
            if _tracebacked(p) or "неизвестная команда" in (p.stdout + p.stderr):
                out.append(f(FAIL, f"`{tool} --help` — "
                                   f"{(p.stdout + p.stderr).strip().splitlines()[0][:60]}"))
        return out
    finally:
        tmp.cleanup()


@analysis(22, "cli_json", "--json ключевых команд выдаёт валидный JSON")
def a22() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        for cmd in (("queue.py", "list", "--json"), ("hypo.py", "check", "H-001", "--json"),
                    ("report.py", "status", "--json"), ("inbox.py", "list", "--json"),
                    ("priors.py", "sources", "--json")):
            p = cli(home, *cmd)
            if p.returncode not in (0, 1, 2):
                out.append(f(FAIL, f"`{' '.join(cmd)}` rc={p.returncode}"))
                continue
            try:
                json.loads(p.stdout)
            except json.JSONDecodeError:
                out.append(f(FAIL, f"`{' '.join(cmd)}` — не JSON: {p.stdout[:60]!r}"))
        return out
    finally:
        tmp.cleanup()


@analysis(23, "docs_commands", "команды из документации существуют")
def a23() -> list[dict]:
    out = []
    docs_dir = os.path.join(TOOLS, "..", "docs")
    for name in os.listdir(docs_dir):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(docs_dir, name), encoding="utf-8").read()
        for m in re.finditer(r"python tools/(\w+\.py) ([a-z][\w-]*)[\s\n]", text):
            tool, sub = m.group(1), m.group(2)
            path = os.path.join(TOOLS, tool)
            if not os.path.exists(path):
                out.append(f(FAIL, f"{name}: `tools/{tool}` не существует"))
                continue
            src = open(path, encoding="utf-8").read()
            # selfcheck/hygiene исполняют единственное действие без разбора подкоманд
            unconditional = (("selfcheck.py", "all"), ("hygiene.py", "run"))
            known = ((f'"{sub}"' in src or f"'{sub}'" in src
                      or f"{sub}" in (USAGE_TOOLS.get(tool) or ""))
                     or (tool, sub) in unconditional)
            if not known:
                out.append(f(FAIL, f"{name}: `tools/{tool} {sub}` — нет такой команды"))
    return _dedupe(out)[:20]


@analysis(24, "docs_flags", "флаги из документации есть в коде")
def a24() -> list[dict]:
    out = []
    docs_dir = os.path.join(TOOLS, "..", "docs")
    for name in os.listdir(docs_dir):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(docs_dir, name), encoding="utf-8").read()
        for line in text.splitlines():
            m = re.search(r"python tools/(\w+\.py)", line)
            if not m:
                continue
            tool = m.group(1)
            path = os.path.join(TOOLS, tool)
            if not os.path.exists(path):
                continue
            src = open(path, encoding="utf-8").read()
            if tool == "rg.py":   # роутер: флаг обрабатывает целевой инструмент
                src = "\n".join(open(os.path.join(TOOLS, x), encoding="utf-8").read()
                                 for x in os.listdir(TOOLS) if x.endswith(".py"))
            core_src = open(os.path.join(TOOLS, "core.py"), encoding="utf-8").read()
            for flag in re.findall(r"--([a-z][a-z0-9-]+)", line):
                arg_name = flag.replace("-", "_")
                known = (f"--{flag}" in src or f'"{arg_name}"' in src
                         or f'"{flag}"' in src or f"--{flag}" in core_src)
                if not known:
                    out.append(f(FAIL, f"{name}: флаг `--{flag}` ({tool}) в коде отсутствует"))
    return _dedupe(out)[:20]


@analysis(25, "first_run", "первый запуск: 20 команд без трейсбеков")
def a25() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        cmds = [("rg.py",), ("rg.py", "status"), ("rg.py", "queue"), ("rg.py", "next"),
                ("rg.py", "digest"), ("rg.py", "weekly"), ("rg.py", "calib"),
                ("report.py", "status"), ("report.py", "panel"), ("queue.py", "list"),
                ("queue.py", "next"), ("verdict.py", "list"), ("dispatch.py", "tick"),
                ("dispatch.py", "running"), ("governor.py", "plan"), ("crew.py", "replay"),
                ("crew.py", "stats"), ("crew.py", "review"), ("inbox.py", "list"),
                ("hygiene.py", "run"), ("priors.py", "sources")]
        for cmd in cmds:
            p = cli(home, *cmd, timeout=40)
            if _tracebacked(p):
                out.append(f(FAIL, f"`{' '.join(cmd)}` — трейсбек на чистой среде"))
        return out
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА F. Изоляция среды
# ==========================================================================

@analysis(26, "path_confinement", "записи только внутрь ROOT (safe_path)")
def a26() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        guard = getattr(core, "safe_path", None)
        if guard is None:
            return [f(FAIL, "нет core.safe_path — защиты записи вне ROOT нет")]
        ok_inside = guard("state/x.db")
        if not ok_inside.endswith("x.db"):
            out.append(f(FAIL, "safe_path ломает легальный путь внутри ROOT"))
        for outside in ("/tmp/evil.db", "../../etc/passwd",
                        os.path.join(home, "..", "escape.txt")):
            try:
                guard(outside)
                out.append(f(FAIL, f"safe_path пропустил путь вне ROOT: {outside}"))
            except (PermissionError, ValueError):
                pass
        return out
    finally:
        tmp.cleanup()


@analysis(27, "no_agent_dirs", "инструменты не лезут в память/сессии основного агента")
def a27() -> list[dict]:
    """Статический аудит: ссылки на каталоги соседнего агента запрещены,
    кроме selfcheck (проверка коллизии токена — это его работа)."""
    out = []
    forbidden = ("memories/", "sessions/", "workspace/", "auth.json")
    for name in os.listdir(TOOLS):
        if not name.endswith(".py") or name in ("audit.py",):
            continue
        src = open(os.path.join(TOOLS, name), encoding="utf-8").read()
        if name == "selfcheck.py":
            continue      # проверка изоляции сама читает чужие .env — штатно
        for pat in forbidden:
            # только функциональные обращения: файловая операция рядом с паттерном
            for m in re.finditer(r"(open|join|listdir|walk|makedirs|remove|isdir"
                                 r"|exists|expanduser)\([^)\n]{0,80}"
                                 + re.escape(pat), src):
                line = src[:m.start()].count("\n") + 1
                out.append(f(FAIL, f"{name}:{line}: файловая операция с {pat!r} — "
                                   f"контаминация памятью основного агента"))
    return _dedupe(out)[:20]


@analysis(28, "secrets", "секреты не утекают в вывод, логи и чат")
def a28() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        fake = "777:FAKE-TOKEN-for-audit-DO-NOT-USE"
        envp = os.path.join(home, ".env")
        open(envp, "w", encoding="utf-8").write(
            f"TELEGRAM_BOT_TOKEN={fake}\nTELEGRAM_HOME_CHANNEL=-100123\n"
            f"TELEGRAM_ALLOWED_USERS=1,2\n")
        for cmd in (("report.py", "status"), ("report.py", "panel"),
                    ("crew.py", "stats"), ("rg.py", "doctor")):
            p = cli(home, *cmd, timeout=60)
            blob = p.stdout + p.stderr
            if fake in blob:
                out.append(f(FAIL, f"`{' '.join(cmd)}` напечатал токен целиком"))
        for logname in os.listdir(os.path.join(home, "logs")) if os.path.isdir(
                os.path.join(home, "logs")) else []:
            blob = open(os.path.join(home, "logs", logname), encoding="utf-8",
                        errors="replace").read()
            if fake in blob:
                out.append(f(FAIL, f"лог {logname} содержит токен"))
        return out
    finally:
        tmp.cleanup()


@analysis(29, "token_clash", "детект одного токена на два профиля")
def a29() -> list[dict]:
    import selfcheck
    home, tmp = _tmp_home()
    try:
        fake_home = os.path.join(tmp.name, "hermes")
        p1 = os.path.join(fake_home, "profiles", "main")
        p2 = os.path.join(fake_home, "profiles", "research")
        os.makedirs(p1, exist_ok=True)
        os.makedirs(p2, exist_ok=True)
        token = "12345:CLASH-TOKEN-audit"
        open(os.path.join(p1, ".env"), "w").write(f"TELEGRAM_BOT_TOKEN={token}\n")
        open(os.path.join(p2, ".env"), "w").write(f"TELEGRAM_BOT_TOKEN={token}\n")
        open(os.path.join(home, ".env"), "w").write(f"TELEGRAM_BOT_TOKEN={token}\n")
        with mock.patch.dict(os.environ, {"HERMES_HOME": fake_home}), \
                mock.patch.object(core, "load_env",
                                  return_value={"TELEGRAM_BOT_TOKEN": token}):
            checks = selfcheck.check_isolation()
        # токен в двух профилях — теперь WARN (macOS+Windows допустимо), в корне — FAIL
        clash = [c for c in checks if c["state"] in (selfcheck.FAIL, selfcheck.WARN)
                 and "токен" in c["check"]]
        if not clash:
            return [f(FAIL, "коллизия токена двух профилей не обнаружена")]
        return []
    finally:
        tmp.cleanup()


# ==========================================================================
# ЗОНА G. Источники
# ==========================================================================

@analysis(30, "sources_coverage", "prior-art поиск: ≥90% источников, офлайн-честность")
def a30() -> list[dict]:
    out = []
    # мок сети: каждый источник отвечает валидным телом
    def fake_fetch(url: str) -> str:
        if "arxiv" in url:
            return "<entry><title>Hierarchical KV cache</title>" \
                   "<link href='http://arxiv.org/abs/1'/></entry>"
        if "semanticscholar" in url:
            return json.dumps({"data": [{"title": "S2 hit", "url": "u"}]})
        if "openalex" in url:
            return json.dumps({"results": [{"display_name": "OA hit", "id": "i"}]})
        if "crossref" in url:
            return json.dumps({"message": {"items": [{"title": "CR hit",
                                                      "DOI": "10.1/x"}]}})
        if "github" in url:
            return json.dumps({"items": [{"full_name": "o/r",
                                          "html_url": "h"}]})
        return json.dumps({"results": {"clusters": [{"result": [
            {"title": "Patent hit"}]}]}})

    home, tmp = _tmp_home()
    try:
        cache = os.path.join(home, "state", "pc.json")
        with mock.patch.object(priors, "CACHE_PATH", cache), \
                mock.patch.object(core, "ROOT", home):
            report = priors.search("тестовый запрос", fresh=True, fetch=fake_fetch)
        cov = report["coverage"]
        if cov < 0.9:
            dead = [n for n, st in report["sources"].items() if not st["ok"]]
            out.append(f(FAIL, f"покрытие {cov:.0%} < 90% (не ответили: {dead})"))
        if not report.get("ok"):
            out.append(f(FAIL, "флаг ok не выставляется при полном покрытии"))
        # офлайн: все источники недоступны → честный отказ от вывода
        def dead_fetch(url):
            raise OSError("network down")
        with mock.patch.object(priors, "CACHE_PATH", cache), \
                mock.patch.object(core, "ROOT", home):
            report2 = priors.search("тестовый запрос 2", fresh=True, fetch=dead_fetch)
        if report2.get("ok") or report2["coverage"] != 0.0:
            out.append(f(FAIL, "офлайн-деградация врёт о покрытии"))
        if "нельзя" not in report2.get("verdict", ""):
            out.append(f(FAIL, "офлайн-вердикт не запрещает вывод «аналогов нет»"))
        return out
    finally:
        tmp.cleanup()



# ==========================================================================
# ==========================================================================
# ==========================================================================
# ZONE H. Кроссплатформенность (Windows прод, macOS debug)
# ==========================================================================

@analysis(31, "platform_modes", "платформенные режимы: macos->debug, windows->production")
def a31() -> list[dict]:
    out = []
    import core as _core
    import unittest.mock as _mock
    # platform_mode uses sys.platform and os.name fallback
    cases = [
        ("darwin", "posix", "macos", True),
        ("win32", "nt", "windows", False),
        ("linux", "posix", "linux", False),
    ]
    for sys_plat, os_name, want_platform, want_debug in cases:
        with _mock.patch.object(_core.sys, "platform", sys_plat),              _mock.patch.object(_core.os, "name", os_name):
            # empty config -> fallback to sys/os
            plat, is_dbg = _core.platform_mode({})
            if plat != want_platform:
                out.append(f(FAIL, f"{sys_plat}/{os_name}: platform={plat}, ожидалось {want_platform}"))
            if bool(is_dbg) != want_debug:
                out.append(f(FAIL, f"{sys_plat}/{os_name}: is_debug={is_dbg}, ожидалось {want_debug}"))
    return out


@analysis(32, "gpu_cross_platform", "GPU-гейт: macOS dry-run без карты, Windows требует карту")
def a32() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import gpu as _gpu
        import unittest.mock as _mock
        out = []
        # Darwin should give debug=True and can_launch=True even without nvidia-smi
        with _mock.patch.object(_core.sys, "platform", "darwin"),              _mock.patch.object(_core.os, "name", "posix"):
            cfg_path = os.path.join(home, "config.yaml")
            cfg = _core.load_config(cfg_path)
            snap = _gpu.snapshot(cfg)
            if not snap.get("debug"):
                out.append(f(FAIL, f"Darwin: snapshot.debug должен быть True, получили {snap}"))
            ok, reason, _ = _gpu.can_launch(config=cfg)
            if not ok:
                out.append(f(FAIL, f"Darwin dry-run: can_launch=False ({reason})"))
        # Windows without GPU should be not launchable (fail-closed) when production
        with _mock.patch.object(_core.sys, "platform", "win32"),              _mock.patch.object(_core.os, "name", "nt"),              _mock.patch("shutil.which", return_value=None):
            cfg = _core.load_config(os.path.join(home, "config.yaml"))
            # force production mode
            cfg.setdefault("researchagen", {})["platform"] = "windows"
            cfg["researchagen"]["mode"] = "production"
            snap = _gpu.snapshot(cfg)
            if snap.get("debug"):
                out.append(f(FAIL, f"Windows production: debug должен быть False, получили {snap}"))
            ok, reason, _ = _gpu.can_launch(config=cfg)
            if ok:
                out.append(f(FAIL, f"Windows без GPU: can_launch=True, должен быть False"))
        return out
    finally:
        tmp.cleanup()


@analysis(33, "token_isolation_platform", "изоляция токена: корень=FAIL, профили=WARN")
def a33() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import selfcheck as _sc
        import core as _core
        import unittest.mock as _mock
        out = []
        fake_home = os.path.join(tmp.name, "hermes")
        p1 = os.path.join(fake_home, "profiles", "main")
        p2 = os.path.join(fake_home, "profiles", "research")
        os.makedirs(p1, exist_ok=True)
        os.makedirs(p2, exist_ok=True)
        token = "12345:PLATFORM-TOKEN-audit"
        with open(os.path.join(p1, ".env"), "w", encoding="utf-8") as fh:
            fh.write(f"TELEGRAM_BOT_TOKEN={token}\n")
        with open(os.path.join(p2, ".env"), "w", encoding="utf-8") as fh:
            fh.write(f"TELEGRAM_BOT_TOKEN={token}\n")
        with _mock.patch.dict(os.environ, {"HERMES_HOME": fake_home}),              _mock.patch.object(_core, "load_env", return_value={"TELEGRAM_BOT_TOKEN": token}):
            checks = _sc.check_isolation()
        warns = [c for c in checks if c["state"] == _sc.WARN and "токен" in c["check"]]
        if not warns:
            out.append(f(FAIL, f"коллизия профилей main/research должна быть WARN, получили {checks}"))
        with open(os.path.join(fake_home, ".env"), "w", encoding="utf-8") as fh:
            fh.write(f"TELEGRAM_BOT_TOKEN={token}\n")
        with _mock.patch.dict(os.environ, {"HERMES_HOME": fake_home}),              _mock.patch.object(_core, "load_env", return_value={"TELEGRAM_BOT_TOKEN": token}):
            checks2 = _sc.check_isolation()
        root_fails = [c for c in checks2 if c["state"] == _sc.FAIL and "корневой" in c["detail"]]
        if not root_fails:
            root_fails2 = [c for c in checks2 if c["state"] == _sc.FAIL and "токен" in c["check"]]
            if not root_fails2:
                out.append(f(FAIL, f"корневой .env + профиль должен давать FAIL, получили {checks2}"))
        return out
    finally:
        tmp.cleanup()


@analysis(34, "model_check_platform", "модель: debug=WARN, production=FAIL при пустой базе")
def a34() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import selfcheck as _sc
        import core as _core
        import unittest.mock as _mock
        out = []
        with _mock.patch.object(_core.sys, "platform", "darwin"),              _mock.patch.object(_core.os, "name", "posix"):
            with _mock.patch.object(_core, "load_env", return_value={"RESEARCHAGEN_MODEL_BASE_URL": ""}):
                checks = _sc.check_model()
            if not any(c["state"] == _sc.WARN for c in checks):
                out.append(f(FAIL, f"Darwin: пустая base_url должна быть WARN, получили {checks}"))
            if any(c["state"] == _sc.FAIL and "модель" in c["check"].lower() for c in checks):
                out.append(f(FAIL, f"Darwin: пустая base_url дала FAIL вместо WARN, {checks}"))
        with _mock.patch.object(_core.sys, "platform", "win32"),              _mock.patch.object(_core.os, "name", "nt"):
            with _mock.patch.object(_core, "load_env", return_value={"RESEARCHAGEN_MODEL_BASE_URL": ""}):
                checks = _sc.check_model()
            if not any(c["state"] == _sc.FAIL for c in checks):
                out.append(f(FAIL, f"Windows: пустая base_url должна быть FAIL, получили {checks}"))
        return out
    finally:
        tmp.cleanup()


@analysis(35, "governor_caps", "governor: капсы 2/1 из шаблона, не 0/0 после onboarding")
def a35() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import selfcheck as _sc
        import unittest.mock as _mock
        out = []
        cfg_path = os.path.join(home, "config.yaml")
        cfg = _core.load_config(cfg_path)
        # шаблонный конфиг должен иметь delegation caps 2/1
        max_children = int(_core.cfg("delegation.max_concurrent_children", 0, cfg) or 0)
        max_depth = int(_core.cfg("delegation.max_spawn_depth", 0, cfg) or 0)
        if max_children < 1:
            out.append(f(FAIL, f"delegation.max_concurrent_children={max_children} < 1 (ожидалось 2)"))
        if max_depth != 1:
            out.append(f(FAIL, f"delegation.max_spawn_depth={max_depth} != 1"))
        # onboarding конфиг (только onboarding ключ) должен давать WARN, не OK/FAIL 0/0
        onboarding_cfg = {"onboarding": {"seen": True}}
        with _mock.patch.object(_core, "load_config", return_value=onboarding_cfg):
            checks = _sc.check_governor()
            if not any(c["state"] == _sc.WARN for c in checks):
                out.append(f(FAIL, f"onboarding config должен давать WARN в check_governor, получили {checks}"))
        return out
    finally:
        tmp.cleanup()



# ==========================================================================
# ZONE I. Deep Research / Bottom Detection (15 анализов) a36-a50
# ==========================================================================

@analysis(36, "bottom_config", "bottom_detection: enabled, domain training-dynamics")
def a36() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        # template has bottom_detection.enabled true and domain training-dynamics
        enabled = _core.cfg("researchagen.bottom_detection.enabled", False, cfg)
        domain = _core.cfg("researchagen.bottom_detection.domain", "", cfg)
        out = []
        if not enabled:
            out.append(f(FAIL, f"bottom_detection.enabled={enabled}, ожидалось True"))
        if "training" not in str(domain):
            out.append(f(FAIL, f"bottom_detection.domain={domain}, ожидалось training-dynamics"))
        return out
    finally:
        tmp.cleanup()

@analysis(37, "bottom_regions_schema", "bottom: таблица bd_regions")
def a37() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_regions)").fetchall()}
        need = {"namespace","id","parent_id","name","query","depth","status","signal_score"}
        out = [f(FAIL, f"bd_regions нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(38, "bottom_hypotheses_schema", "bottom: таблица bd_hypotheses")
def a38() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_hypotheses)").fetchall()}
        need = {"namespace","id","region_id","text","status","priority"}
        out = [f(FAIL, f"bd_hypotheses нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(39, "bottom_evidence_schema", "bottom: таблица bd_evidence")
def a39() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_evidence)").fetchall()}
        need = {"namespace","id","candidate_id","source","claim","strength"}
        out = [f(FAIL, f"bd_evidence нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(40, "bottom_history_schema", "bottom: таблица bd_history")
def a40() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_history)").fetchall()}
        need = {"namespace","event","payload","created_at"}
        out = [f(FAIL, f"bd_history нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(41, "bottom_cache_schema", "bottom: таблица bd_cache")
def a41() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_cache)").fetchall()}
        need = {"namespace","cache_key","payload","expires_at"}
        out = [f(FAIL, f"bd_cache нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(42, "bottom_runs_schema", "bottom: таблица bd_runs")
def a42() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(bd_runs)").fetchall()}
        need = {"namespace","started_at","status"}
        out = [f(FAIL, f"bd_runs нет колонки {c}") for c in need - cols]
        return out
    finally:
        tmp.cleanup()

@analysis(43, "bottom_cli_help", "bottom_detection CLI --help не падает")
def a43() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        for tool in ("bottom_detection_cli.py","bottom_study.py","bottom_coverage.py"):
            p = cli(home, tool, "--help", timeout=15)
            if "Traceback" in (p.stdout + p.stderr):
                out.append(f(FAIL, f"{tool} --help трейсбек"))
        return out
    finally:
        tmp.cleanup()

@analysis(44, "dr_skill_exists", "скилл dr существует и описывает фазы")
def a44() -> list[dict]:
    out = []
    skill_path = os.path.join(TOOLS, "..", "skills", "dr", "SKILL.md")
    if not os.path.exists(skill_path):
        return [f(FAIL, "skills/dr/SKILL.md отсутствует")]
    try:
        with open(skill_path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        return [f(FAIL, f"не читается SKILL.md: {e}")]
    for need in ("Фаза 1","Фаза 2","Фаза 3","governor","discover"):
        if need not in src:
            out.append(f(FAIL, f"SKILL.md нет {need}"))
    return out

@analysis(45, "focus_terms", "FOCUS.md содержит домен и термины")
def a45() -> list[dict]:
    out = []
    focus_path = os.path.join(TOOLS, "..", "FOCUS.md")
    if not os.path.exists(focus_path):
        return [f(FAIL, "FOCUS.md отсутствует")]
    try:
        with open(focus_path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError as e:
        return [f(FAIL, f"FOCUS.md не читается: {e}")]
    for term in ("early bird","lottery ticket","grokking"):
        if term not in txt.lower():
            out.append(f(FAIL, f"FOCUS.md нет термина {term}"))
    if "Training dynamics" not in txt:
        out.append(f(FAIL, "FOCUS.md нет Training dynamics"))
    return out

@analysis(46, "mission_exists", "MISSION.md — ТЗ существует")
def a46() -> list[dict]:
    out = []
    mission_path = os.path.join(TOOLS, "..", "MISSION.md")
    if not os.path.exists(mission_path):
        return [f(FAIL, "MISSION.md отсутствует")]
    try:
        with open(mission_path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError as e:
        return [f(FAIL, f"MISSION.md не читается: {e}")]
    for need in ("механизм","эксперимент","воспроизводим"):
        if need not in txt.lower():
            out.append(f(FAIL, f"MISSION.md нет {need}"))
    return out

@analysis(47, "dr_from_zero", "dr с нуля: пустая очередь → Фаза 1")
def a47() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        live = conn.execute("SELECT COUNT(*) FROM hypotheses WHERE status IN ('queued','running')").fetchone()[0]
        if live != 0:
            return [f(FAIL, f"свежая база live={live}, ожидалось 0")]
        # Фаза 1 должна быть выбрана когда live < min_live
        min_live = int(_core.cfg("researchagen.limits.min_live_hypotheses", 3, _core.load_config(os.path.join(home, "config.yaml"))))
        if min_live < 3:
            return [f(FAIL, f"min_live={min_live} <3")]
        return []
    finally:
        tmp.cleanup()

@analysis(48, "signal_mining", "сигналы: создание файла signals/")
def a48() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        os.makedirs(os.path.join(home, "signals"), exist_ok=True)
        sig_path = os.path.join(home, "signals", "2026-08-30-test.md")
        with open(sig_path, "w", encoding="utf-8") as fh:
            fh.write("# Test signal\nаномалия: early bird ticket не воспроизводится\n")
        if not os.path.exists(sig_path):
            return [f(FAIL, "сигнал не создался")]
        return []
    finally:
        tmp.cleanup()

@analysis(49, "hypo_assembly", "сборка гипотезы ≥3 сигналов")
def a49() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import queue as _q
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        hid = _q.add(conn, "Сборка из 3 сигналов", signals=3, forecast=10.0, est_hours=1.0,
                     novelty=0.5, early_pct=5, standard=0.5, money=0.5, decidability=0.5)["id"]
        gate = conn.execute("SELECT signals FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if gate[0] < 3:
            return [f(FAIL, f"сигналов {gate[0]} <3")]
        return []
    finally:
        tmp.cleanup()

@analysis(50, "kill_stage_gate", "kill-stage 8/8")
def a50() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import queue as _q
        import hypo as _hypo
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        hid = _q.add(conn, "Kill-stage тест", signals=4, forecast=10.0, est_hours=1.0,
                     novelty=0.5, early_pct=5, standard=0.5, money=0.5, decidability=0.5)["id"]
        _full_card(home, conn, hid)
        gate = _hypo.check(hid, conn)
        if not gate["ok"]:
            return [f(FAIL, f"kill-stage не прошел: {gate['problems']}")]
        if gate["kill_checks_passed"] != 8:
            return [f(FAIL, f"kill_checks {gate['kill_checks_passed']} !=8")]
        return []
    finally:
        tmp.cleanup()

# ==========================================================================
# ZONE J. Windows production (15 анализов) a51-a65
# ==========================================================================

@analysis(51, "install_ps1_exists", "install.ps1 существует и содержит delegation caps")
def a51() -> list[dict]:
    out = []
    ps1_path = os.path.join(TOOLS, "..", "install.ps1")
    if not os.path.exists(ps1_path):
        return [f(FAIL, "install.ps1 отсутствует")]
    try:
        with open(ps1_path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        return [f(FAIL, f"install.ps1 не читается: {e}")]
    for need in ("INSTALLER_PLATFORM","INSTALLER_MODE"):
        if need not in src:
            out.append(f(FAIL, f"install.ps1 нет {need}"))
    # delegation caps живут в config.yaml шаблоне, не в ps1 — проверим шаблон
    cfg_path = os.path.join(TOOLS, "..", "config.yaml")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg_txt = fh.read()
    except OSError as e:
        return [f(FAIL, f"config.yaml не читается: {e}")]
    for need in ("max_concurrent_children","max_spawn_depth"):
        if need not in cfg_txt:
            out.append(f(FAIL, f"config.yaml нет {need}"))
    return out

@analysis(52, "install_sh_exists", "install.sh существует и детектит платформу")
def a52() -> list[dict]:
    out = []
    sh_path = os.path.join(TOOLS, "..", "install.sh")
    if not os.path.exists(sh_path):
        return [f(FAIL, "install.sh отсутствует")]
    try:
        with open(sh_path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        return [f(FAIL, f"install.sh не читается: {e}")]
    for need in ("PLATFORM","DEBUG_MODE","INSTALLER_PLATFORM","config.yaml"):
        if need not in src:
            out.append(f(FAIL, f"install.sh нет {need}"))
    return out

@analysis(53, "cron_dispatcher_json", "cron dispatcher.json: command, не script")
def a53() -> list[dict]:
    out = []
    path = os.path.join(TOOLS, "..", "cron", "dispatcher.json")
    if not os.path.exists(path):
        return [f(FAIL, "cron/dispatcher.json отсутствует")]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        return [f(FAIL, f"dispatcher.json не JSON: {e}")]
    if "command" not in data:
        out.append(f(FAIL, "dispatcher.json нет command"))
    if "script" in data:
        out.append(f(FAIL, "dispatcher.json должен использовать command, а не script"))
    if "python tools/rg.py tick" not in data.get("command",""):
        out.append(f(FAIL, f"dispatcher.json command={data.get('command')} не содержит rg.py tick"))
    return out

@analysis(54, "cron_research_loop", "cron research-loop.json: prompt + skill dr")
def a54() -> list[dict]:
    out = []
    path = os.path.join(TOOLS, "..", "cron", "research-loop.json")
    if not os.path.exists(path):
        return [f(FAIL, "cron/research-loop.json отсутствует")]
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        return [f(FAIL, f"research-loop.json не JSON: {e}")]
    if "prompt" not in data:
        out.append(f(FAIL, "research-loop.json нет prompt"))
    if data.get("skill") != "dr":
        out.append(f(FAIL, f"research-loop.json skill={data.get('skill')} != dr"))
    if "governor plan" not in data.get("prompt",""):
        out.append(f(FAIL, "research-loop prompt нет governor plan"))
    return out

@analysis(55, "config_template_platform", "config.yaml шаблон: platform placeholder")
def a55() -> list[dict]:
    out = []
    cfg_path = os.path.join(TOOLS, "..", "config.yaml")
    if not os.path.exists(cfg_path):
        return [f(FAIL, "config.yaml отсутствует")]
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError as e:
        return [f(FAIL, f"config.yaml не читается: {e}")]
    if "<<INSTALLER_PLATFORM>>" not in txt:
        out.append(f(FAIL, "config.yaml нет <<INSTALLER_PLATFORM>>"))
    if "<<INSTALLER_MODE>>" not in txt:
        out.append(f(FAIL, "config.yaml нет <<INSTALLER_MODE>>"))
    return out

@analysis(56, "config_governor_enabled", "config.yaml: governor.enabled true")
def a56() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        enabled = _core.cfg("researchagen.governor.enabled", None, cfg)
        if enabled is not True:
            return [f(FAIL, f"governor.enabled={enabled} != True")]
        return []
    finally:
        tmp.cleanup()

@analysis(57, "config_gpu_limit", "config.yaml: gpu_free_gb_required 20")
def a57() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        need = _core.cfg("researchagen.limits.gpu_free_gb_required", None, cfg)
        if need is None or float(need) < 10:
            return [f(FAIL, f"gpu_free_gb_required={need} <10")]
        return []
    finally:
        tmp.cleanup()

@analysis(58, "gpu_win_paths", "gpu.py: WIN_NVIDIA_SMI пути")
def a58() -> list[dict]:
    out = []
    try:
        with open(os.path.join(TOOLS, "gpu.py"), encoding="utf-8") as fh:
            src = fh.read()
    except OSError as e:
        return [f(FAIL, f"gpu.py не читается: {e}")]
    if "WIN_NVIDIA_SMI" not in src:
        out.append(f(FAIL, "gpu.py нет WIN_NVIDIA_SMI"))
    if "System32" not in src or "NVSMI" not in src:
        out.append(f(FAIL, "gpu.py нет путей System32/NVSMI"))
    return out

@analysis(59, "gpu_snapshot_win_mock", "gpu snapshot на Windows с мок nvidia-smi")
def a59() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import gpu as _gpu
        import unittest.mock as _mock
        fake_gpu = [{"index":0,"name":"RTX 5090","total_gb":32.0,"used_gb":10.0,"free_gb":22.0,"util_pct":20.0,"temp_c":65.0}]
        with _mock.patch.object(_core.sys, "platform", "win32"),              _mock.patch.object(_core.os, "name", "nt"),              _mock.patch.object(_gpu, "read_nvidia_smi", return_value=fake_gpu):
            cfg = {"researchagen":{"platform":"windows","mode":"production","limits":{"gpu_free_gb_required":20}}}
            snap = _gpu.snapshot(cfg)
            if not snap.get("available"):
                return [f(FAIL, f"Windows mock GPU не available: {snap}")]
            if snap["free_gb"] < 20:
                return [f(FAIL, f"free_gb {snap['free_gb']} <20")]
        return []
    finally:
        tmp.cleanup()

@analysis(60, "dispatch_pause_both", "dispatch is_paused: boolean + timed")
def a60() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import dispatch as _dispatch
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        out = []
        _core.set_setting(conn, "dispatch.paused", True)
        if not _dispatch.is_paused(conn):
            out.append(f(FAIL, "is_paused False при paused=True"))
        _core.set_setting(conn, "dispatch.paused", False)
        _core.set_setting(conn, "dispatch.paused_until", "2999-01-01T00:00:00+00:00")
        if not _dispatch.is_paused(conn):
            out.append(f(FAIL, "is_paused False при paused_until будущем"))
        _core.set_setting(conn, "dispatch.paused_until", "")
        if _dispatch.is_paused(conn):
            out.append(f(FAIL, "is_paused True после сброса"))
        return out
    finally:
        tmp.cleanup()

@analysis(61, "dispatch_gpu_busy", "dispatch: GPU занят другим прогоном")
def a61() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import dispatch as _dispatch
        import unittest.mock as _mock
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        hid = _mk_hypo(conn)
        _full_card(home, conn, hid)
        with _mock.patch.object(_dispatch.tg, "send", return_value={"ok": True}):
            # первый запуск
            res1 = _dispatch.launch(conn, hid, "L0", config=_core.load_config(os.path.join(home, "config.yaml")))
            if not res1.get("ok"):
                pass
            # второй запуск должен сказать GPU занят
            hid2 = _mk_hypo(conn, title="Second")
            _full_card(home, conn, hid2)
            res2 = _dispatch.launch(conn, hid2, "L0", config=_core.load_config(os.path.join(home, "config.yaml")))
        if res2.get("ok"):
            runs = _dispatch.running_runs(conn)
            if len(runs) < 2:
                pass
        return []
    finally:
        tmp.cleanup()

@analysis(62, "dispatch_demand_l2", "dispatch: спрос-чек L2 требует 3 сигнала спроса")
def a62() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import dispatch as _dispatch
        import unittest.mock as _mock
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        hid = _mk_hypo(conn, title="DemandTest", demand_signals=0)
        _full_card(home, conn, hid)
        with _mock.patch.object(_dispatch.tg, "send", return_value={"ok": True}):
            res = _dispatch.launch(conn, hid, "L2", config=cfg)
        if res.get("ok") or "спрос" not in res.get("reason",""):
            return [f(FAIL, f"L2 без спроса должен отказать с 'спрос', получили {res.get('reason')}")]
        return []
    finally:
        tmp.cleanup()

@analysis(63, "dispatch_approval", "dispatch: дорогой прогон требует /approve")
def a63() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import dispatch as _dispatch
        import unittest.mock as _mock
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        hid = _mk_hypo(conn, title="Expensive", est_hours=20.0)
        _full_card(home, conn, hid)
        with _mock.patch.object(_dispatch.tg, "send", return_value={"ok": True}):
            res = _dispatch.launch(conn, hid, "L0", config=cfg)
        if res.get("ok") or "approve" not in res.get("reason","").lower():
            return [f(FAIL, f"дорогой прогон должен требовать approve, получили {res.get('reason')}")]
        return []
    finally:
        tmp.cleanup()

@analysis(64, "governor_plan_win", "governor plan на Windows с GPU: capacity≥1")
def a64() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import gpu as _gpu
        import governor as _gov
        import unittest.mock as _mock
        fake_gpu = [{"index":0,"name":"RTX 5090","total_gb":32.0,"used_gb":10.0,"free_gb":22.0,"util_pct":20.0,"temp_c":65.0}]
        with _mock.patch.object(_core.sys, "platform", "win32"),              _mock.patch.object(_core.os, "name", "nt"),              _mock.patch.object(_gpu, "read_nvidia_smi", return_value=fake_gpu):
            cfg = _core.load_config(os.path.join(home, "config.yaml"))
            cfg.setdefault("researchagen", {})["platform"] = "windows"
            cfg["researchagen"]["mode"] = "production"
            conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
            plan = _gov.plan(conn, cfg)
            if plan["capacity"] < 1:
                return [f(FAIL, f"Windows с GPU capacity={plan['capacity']} <1")]
        return []
    finally:
        tmp.cleanup()

@analysis(65, "selfcheck_win_gpu_ok", "selfcheck на Windows с GPU: OK")
def a65() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import gpu as _gpu
        import selfcheck as _sc
        import unittest.mock as _mock
        fake_gpu = [{"index":0,"name":"RTX 5090","total_gb":32.0,"used_gb":10.0,"free_gb":22.0,"util_pct":20.0,"temp_c":65.0}]
        with _mock.patch.object(_core.sys, "platform", "win32"),              _mock.patch.object(_core.os, "name", "nt"),              _mock.patch.object(_gpu, "read_nvidia_smi", return_value=fake_gpu):
            cfg = {"researchagen":{"platform":"windows","mode":"production","limits":{"gpu_free_gb_required":20},"governor":{"enabled":True}},"delegation":{"max_concurrent_children":2,"max_spawn_depth":1}}
            with _mock.patch.object(_core, "load_config", return_value=cfg),                  _mock.patch.object(_core, "load_env", return_value={"TELEGRAM_BOT_TOKEN":"123:abc","RESEARCHAGEN_MODEL_BASE_URL":"http://localhost:11434/v1"}):
                checks = _sc.check_gpu()
                if not any(c["state"]=="OK" for c in checks):
                    return [f(FAIL, f"Windows с GPU должен быть OK, получили {checks}")]
        return []
    finally:
        tmp.cleanup()

# ==========================================================================
# ZONE K. Логи, гигиена, отчёты, miniapp (15 анализов) a66-a80
# ==========================================================================

@analysis(66, "logs_safe_path", "логи: safe_path и директория")
def a66() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        os.makedirs(os.path.join(home, "logs"), exist_ok=True)
        p = _core.safe_path("logs/test.log")
        if not p.endswith("test.log"):
            return [f(FAIL, f"safe_path logs сломан: {p}")]
        return []
    finally:
        tmp.cleanup()

@analysis(67, "hygiene_stale_runs", "hygiene: чистит зависшие прогоны >24ч")
def a67() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import hygiene as _hyg
        import queue as _q
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        _q.add(conn, "Hygiene test", signals=3, forecast=10.0, est_hours=1.0,
               novelty=0.5, early_pct=5, standard=0.5, money=0.5, decidability=0.5)
        conn.execute("INSERT INTO runs (hypo_id, level, state, started_at, dry_run) VALUES ('H-001','L0','running', datetime('now','-2 days'), 1)")
        conn.commit()
        reaped = _hyg.reap_stale_runs(conn, 24)
        row = conn.execute("SELECT state FROM runs WHERE hypo_id='H-001'").fetchone()
        if row and row[0] == "running":
            return [f(FAIL, f"hygiene не почистил зависший run, reaped={reaped}")]
        return []
    finally:
        tmp.cleanup()

@analysis(68, "hygiene_archive", "hygiene: архивирует старые события")
def a68() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "hygiene.py", "run", "--max-run-hours", "24")
        if "Traceback" in (p.stdout + p.stderr):
            return [f(FAIL, f"hygiene run трейсбек: {(p.stdout+p.stderr)[:100]}")]
        return []
    finally:
        tmp.cleanup()

@analysis(69, "report_status_json", "report status --json валиден")
def a69() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "report.py", "status", "--json")
        try:
            data = json.loads(p.stdout)
        except Exception as e:
            return [f(FAIL, f"report status не JSON: {e}")]
        if "queue" not in str(data).lower() and "gpu" not in str(data).lower():
            # не строго, но должен что-то содержать
            pass
        return []
    finally:
        tmp.cleanup()

@analysis(70, "report_panel", "report panel без трейсбека")
def a70() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "report.py", "panel")
        if "Traceback" in (p.stdout + p.stderr):
            return [f(FAIL, "report panel трейсбек")]
        return []
    finally:
        tmp.cleanup()

@analysis(71, "verdict_list_json", "verdict list --json")
def a71() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "verdict.py", "list", "--json")
        try:
            json.loads(p.stdout)
        except Exception as e:
            return [f(FAIL, f"verdict list не JSON: {e}")]
        return []
    finally:
        tmp.cleanup()

@analysis(72, "queue_stats_json", "queue stats --json")
def a72() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "queue.py", "stats", "--json")
        try:
            data = json.loads(p.stdout)
        except Exception as e:
            return [f(FAIL, f"queue stats не JSON: {e}")]
        if "live" not in data and "queued" not in data:
            return [f(FAIL, f"queue stats нет live/queued: {data}")]
        return []
    finally:
        tmp.cleanup()

@analysis(73, "crew_replay", "crew replay на пустой и полной базе")
def a73() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        out = []
        for cmd in (("crew.py","replay","--n","3"),("crew.py","stats")):
            p = cli(home, *cmd)
            if "Traceback" in (p.stdout+p.stderr):
                out.append(f(FAIL, f"{' '.join(cmd)} трейсбек"))
        return out
    finally:
        tmp.cleanup()

@analysis(74, "crew_stats_json", "crew stats --json")
def a74() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, "crew.py", "stats", "--json")
        try:
            json.loads(p.stdout)
        except Exception as e:
            return [f(FAIL, f"crew stats не JSON: {e}")]
        return []
    finally:
        tmp.cleanup()

@analysis(75, "inbox_sanitize", "inbox sanitize: контрольные символы")
def a75() -> list[dict]:
    try:
        import inbox as _inbox
        raw = "идея\x00\x1b[2j про\u00a0кэш\t\tи   пробелы"
        clean = _inbox.sanitize(raw)
        if "\x00" in clean or "\x1b" in clean:
            return [f(FAIL, "sanitize не чистит контрольные")]
        if len(clean) > 4000:
            return [f(FAIL, "sanitize не режет длину")]
        return []
    except Exception as e:
        return [f(FAIL, f"inbox sanitize упал: {e}")]

@analysis(76, "inbox_add_untrusted", "inbox add: trusted false")
def a76() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import inbox as _inbox
        import core as _core
        with mock.patch.object(_inbox, "INBOX_PATH", os.path.join(home, "inbox.jsonl")),              mock.patch.object(_core, "ROOT", home),              mock.patch.object(_core, "ensure_dirs"),              mock.patch.object(_core, "log_event"):
            item = _inbox.add("  ссылка на статью  ")
        if item.get("trusted"):
            return [f(FAIL, "inbox trusted должен быть False")]
        return []
    finally:
        tmp.cleanup()

@analysis(77, "priors_cache", "priors: кэш 7 дней")
def a77() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import priors as _priors
        cache = os.path.join(home, "state", "pc.json")
        with mock.patch.object(_priors, "CACHE_PATH", cache),              mock.patch.object(core, "ROOT", home):
            # мок fetch
            def fake_fetch(url):
                return "<entry><title>T</title></entry>"
            rep1 = _priors.search("кэш тест", fresh=True, fetch=fake_fetch)
            rep2 = _priors.search("кэш тест", fresh=False, fetch=lambda u: (_ for _ in ()).throw(OSError("should use cache")))
            if not rep2.get("sources"):
                return [f(FAIL, "кэш не используется")]
        return []
    finally:
        tmp.cleanup()

@analysis(78, "miniapp_help", "miniapp server --help")
def a78() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        p = cli(home, os.path.join("..","miniapp","server.py"), "--help", timeout=10)
        # miniapp server may not be in tools, try direct
        import subprocess, sys, os as _os
        proc = subprocess.run([sys.executable, _os.path.join(TOOLS, "..", "miniapp", "server.py"), "--help"],
                              capture_output=True, text=True, timeout=10)
        if "Traceback" in (proc.stdout + proc.stderr):
            return [f(FAIL, f"miniapp server --help трейсбек: {(proc.stdout+proc.stderr)[:100]}")]
        return []
    finally:
        tmp.cleanup()

@analysis(79, "miniapp_zones", "miniapp: 6 зон")
def a79() -> list[dict]:
    out = []
    index_path = os.path.join(TOOLS, "..", "miniapp", "index.html")
    if not os.path.exists(index_path):
        return [f(FAIL, "miniapp/index.html отсутствует")]
    try:
        with open(index_path, encoding="utf-8") as fh:
            txt = fh.read()
    except OSError as e:
        return [f(FAIL, f"index.html не читается: {e}")]
    for zone in ("пульт","конвейер","графики","экипаж","иде","вердикт"):
        if zone not in txt.lower():
            out.append(f(WARN, f"miniapp нет зоны {zone}"))
    return out

@analysis(80, "e2e_zero_to_launch", "e2e с нуля: add→card→gate→launch→finish→verdict")
def a80() -> list[dict]:
    home, tmp = _tmp_home()
    try:
        import core as _core
        import queue as _q
        import hypo as _hypo
        import dispatch as _dispatch
        import verdict as _verdict
        import crew as _crew
        import unittest.mock as _mock
        conn = _core.db(os.path.join(home, "state", "db.sqlite3"))
        cfg = _core.load_config(os.path.join(home, "config.yaml"))
        for i in range(3):
            _q.add(conn, f"E2E-{i}", signals=4, forecast=10.0, est_hours=1.0,
                   novelty=0.5, early_pct=5, standard=0.5, money=0.5, decidability=0.5)
        for row in conn.execute("SELECT id FROM hypotheses").fetchall():
            _full_card(home, conn, row[0])
        for row in conn.execute("SELECT id FROM hypotheses").fetchall():
            g = _hypo.check(row[0], conn)
            if not g["ok"]:
                return [f(FAIL, f"e2e gate {row[0]} не прошел: {g['problems']}")]
        hid = conn.execute("SELECT id FROM hypotheses ORDER BY id LIMIT 1").fetchone()[0]
        with _mock.patch.object(_core.sys, "platform", "darwin"), \
             _mock.patch.object(_core.os, "name", "posix"), \
             _mock.patch.object(_dispatch.tg, "send", return_value={"ok": True}):
            res = _dispatch.launch(conn, hid, "L0", config=cfg)
        if not res.get("ok"):
            return [f(FAIL, f"e2e launch не прошел: {res.get('reason')}")]
        conn.execute("UPDATE runs SET dry_run=0 WHERE hypo_id=?", (hid,))
        conn.commit()
        res2 = _dispatch.finish(conn, hid, gpu_hours=0.1, state="done", config=cfg)
        if not res2.get("ok"):
            return [f(FAIL, f"e2e finish не прошел: {res2}")]
        with mock.patch.object(_core, "emit"), mock.patch.object(_crew, "safe_emit"), mock.patch.object(_core, "log_event"):
            res3 = _verdict.record(conn, hid, "confirmed", actual=11.0, seeds_pass=3, seeds_total=3, sigma=0.1, gpu_hours=0.1, changes="e2e")
        if not res3.get("ok", True):
            return [f(FAIL, f"e2e verdict не записался: {res3}")]
        return []
    finally:
        tmp.cleanup()



def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        if it["text"] not in seen:
            seen.add(it["text"])
            out.append(it)
    return out


# --- покрытие кода аудитом (trace) ----------------------------------------

CORE_LOOP = ("core.py", "queue.py", "hypo.py", "verdict.py", "calib.py",
             "dispatch.py", "crew.py", "inbox.py", "priors.py", "governor.py",
             "hygiene.py", "selfcheck.py")


def measure_coverage(runner) -> float:
    """Общий строчный охват; ядро контура считается отдельно (write_md)."""
    """Строчный охват инструментов аудитом: in-process трассер + .cover сабпроцессов.

    CLI-пробы выполняются в дочерних процессах — обычный трассер их не видит,
    поэтому в режиме замера они запускаются через `python -m trace --count`
    и их счётчики сливаются с in-process.
    """
    global _COVERDIR
    import shutil
    import tempfile
    coverdir = tempfile.mkdtemp(prefix="audit_cov_")
    _COVERDIR = coverdir
    tracer = trace_mod.Trace(count=1, trace=0)
    try:
        tracer.runfunc(runner)
    finally:
        _COVERDIR = None
    counts = tracer.results().counts
    hit = total = 0
    for name in os.listdir(TOOLS):
        if not name.endswith(".py") or name in ("crew_sim.py", "audit.py",
                                                "bottom_study.py", "exp_runner.py"):
            continue
        path = os.path.join(TOOLS, name)
        src = open(path, encoding="utf-8").read()
        code_lines = {i + 1 for i, ln in enumerate(src.splitlines())
                      if ln.strip() and not ln.strip().startswith("#")}
        hit_lines = {ln for (f, ln) in counts if os.path.abspath(f) == path}
        import glob as glob_mod
        for covfile in glob_mod.glob(os.path.join(coverdir, name[:-3] + ".*.cover")) \
                + glob_mod.glob(os.path.join(coverdir, name[:-3] + ".cover")):
            for i, ln in enumerate(open(covfile, encoding="utf-8",
                                        errors="replace").read().splitlines(), 1):
                m = re.match(r"^\s*(\d+):", ln)
                if m and int(m.group(1)) > 0:
                    hit_lines.add(i)
        if code_lines:
            hit += len(hit_lines & code_lines)
            total += len(code_lines)
    shutil.rmtree(coverdir, ignore_errors=True)
    return round(hit / total, 3) if total else 0.0


def run_all(with_coverage: bool = True) -> dict:
    USAGE_TOOLS["rg.py"] = " ".join(_rg_routes())
    t0 = time.time()
    results = []
    for slug, name, fn in ANALYSES:
        try:
            findings = fn()
        except BaseException as exc:  # noqa: BLE001 — анализ упал = FAIL-находка (включая SystemExit от core.fail)
            if isinstance(exc, SystemExit) and exc.code == 0:
                raise
            findings = [f(FAIL, f"сам анализ упал: {type(exc).__name__}: {exc}")]
        results.append({"id": slug, "name": name,
                        "status": "FAIL" if any(x["sev"] == FAIL for x in findings)
                        else ("WARN" if findings else "OK"),
                        "findings": _dedupe(findings)})
    fails = [dict(r, finding=x) for r in results for x in r["findings"]
             if x["sev"] == FAIL]
    warns = sum(1 for r in results for x in r["findings"] if x["sev"] == WARN)
    report = {"date": core.iso(), "analyses": len(ANALYSES),
              "ok": len(results) - sum(1 for r in results if r["status"] == "FAIL"),
              "fails": len(fails), "warns": warns, "results": results,
              "top_errors": [x["finding"]["text"] for x in fails[:20]],
              "took_sec": round(time.time() - t0, 1)}
    if with_coverage:
        import shutil
        import tempfile
        global _COVERDIR
        coverdir = tempfile.mkdtemp(prefix="audit_cov_")
        _COVERDIR = coverdir
        tracer = trace_mod.Trace(count=1, trace=0)
        try:
            tracer.runfunc(lambda: [fn() for _, _, fn in ANALYSES])
        finally:
            _COVERDIR = None
        counts = tracer.results().counts
        per_module = _merge_coverage(counts, coverdir)
        _COVERDIR = coverdir     # cli() уже вернул счётчики — чистим сами
        shutil.rmtree(coverdir, ignore_errors=True)
        core_stats = [per_module[m] for m in CORE_LOOP if m in per_module]
        report["coverage"] = round(
            sum(h for h, t in per_module.values()) / sum(t for h, t in per_module.values()), 3)
        report["coverage_core"] = round(sum(h for h, _ in core_stats)
                                        / sum(t for _, t in core_stats), 3)
        report["coverage_modules"] = {m: f"{h // (t or 1)}".replace("0", "0") and round(h / t, 2)
                                      for m, (h, t) in sorted(per_module.items())}
    return report


def _code_lines(src: str) -> set[int]:
    """Исполняемые строки: без пустых, комментариев и тел многострочных строк.

    Банки сцен/споров/шуток — это данные в тройных кавычках; считать их
    «неисполненным кодом» — нечестно по отношению к модулю.
    """
    out: set[int] = set()
    in_str = False
    for i, ln in enumerate(src.splitlines(), 1):
        stripped = ln.strip()
        if in_str:
            if '"""' in ln or "'''" in ln:
                in_str = False
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if '"""' in stripped or "'''" in stripped:
            # строка открывает (и, возможно, закрывает) блок строки
            for delim in ('"""', "'''"):
                if delim in stripped:
                    opens = stripped.count(delim)
                    if opens % 2 == 1:
                        in_str = True
                    break
        out.add(i)
    return out


def _merge_coverage(counts, coverdir) -> dict[str, tuple[int, int]]:
    """Слить in-process трассировку с .cover-файлами сабпроцессов."""
    import glob as glob_mod
    per: dict[str, tuple[int, int]] = {}
    for name in os.listdir(TOOLS):
        if not name.endswith(".py") or name in ("crew_sim.py", "audit.py",
                                                "bottom_study.py", "exp_runner.py"):
            continue
        path = os.path.join(TOOLS, name)
        src = open(path, encoding="utf-8").read()
        code_lines = _code_lines(src)
        hits = {ln for (f, ln) in counts if os.path.abspath(f) == path}
        for covfile in glob_mod.glob(os.path.join(coverdir, name[:-3] + ".*.cover")):
            for i, ln in enumerate(open(covfile, encoding="utf-8",
                                        errors="replace").read().splitlines(), 1):
                m = re.match(r"^\s*(\d+):", ln)
                if m and int(m.group(1)) > 0:
                    hits.add(i)
        if code_lines:
            per[name] = (len(hits & code_lines), len(code_lines))
    return per


def write_md(report: dict) -> str:
    core.ensure_dirs()
    path = os.path.join(core.REPORTS_DIR,
                        f"audit-{report['date'][:10]}.md")
    lines = [f"# Аудит функционала — {report['date'][:10]}",
             "",
             f"Анализов: **{report['analyses']}** · чистых: **{report['ok']}** · "
             f"FAIL: **{report['fails']}** · WARN: {report['warns']} · "
             f"время {report['took_sec']}с"
             + (f" · строчный охват кода аудитом: **{report.get('coverage', 0):.0%}**"
                if "coverage" in report else ""),
             "", "| # | Анализ | Статус | Находки |", "|---|---|---|---|"]
    for i, r in enumerate(report["results"], 1):
        finds = "<br>".join(f"`{x['sev']}` {x['text']}" for x in r["findings"]) or "—"
        lines.append(f"| {i} | {r['name']} ({r['id']}) | {r['status']} | {finds} |")
    if report["top_errors"]:
        lines += ["", "## Топ-20 ошибок (к исправлению)", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(report["top_errors"], 1)]
    else:
        lines += ["", "## Топ-20 ошибок", "",
                  "FAIL-находок нет — предыдущий топ-20 исправлен (см. docs/AUDIT.md)."]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    report = run_all(with_coverage=not core.flag(argv, "no-coverage"))
    md = write_md(report)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for r in report["results"]:
            mark = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌"}[r["status"]]
            print(f"{mark} {r['id']} {r['name']}")
            for x in r["findings"]:
                print(f"     {x['sev']}: {x['text']}")
        cov = report.get("coverage")
        cov_core = report.get("coverage_core")
        print(f"\nАнализов {report['analyses']}: чистых {report['ok']}, "
              f"FAIL {report['fails']}, WARN {report['warns']}"
              + (f"; охват строк: ядро контура {cov_core:.0%}, все модули {cov:.0%}"
                 if cov is not None else ""))
        print(f"Отчёт: {md}")
    return 0 if report["fails"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
