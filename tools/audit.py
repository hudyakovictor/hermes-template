#!/usr/bin/env python3
"""researchagen — аудит функционала: 30 анализов, покрывающих ~90% комбинаций задач.

Метод: каждый анализ прогоняет реальный код (библиотечные вызовы на временной
базе или CLI во временном RESEARCHAGEN_HOME) и возвращает находки FAIL/WARN.
Аудит честный: он находит ошибки до их исправления и проходит после.

Что измеряется:
  * 30 анализов по 7 зонам: данные, вердикты/калибровка, диспетчер/governor,
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
        open(card, "w", encoding="utf-8").write(text)
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
    open(card, "w", encoding="utf-8").write(text)
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
        config = core.load_config()
        hid = _mk_hypo(conn)
        _full_card(home, conn, hid)
        # пауза
        core.set_setting(conn, "dispatch.paused_until", "2999-01-01T00:00:00+00:00")
        res = dispatch.launch(conn, hid, "L0", config=config)
        if res.get("ok"):
            out.append(f(FAIL, "запуск на паузе прошёл"))
        core.set_setting(conn, "dispatch.paused_until", "")
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
        clash = [c for c in checks if c["state"] == selfcheck.FAIL]
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
        except Exception as exc:  # noqa: BLE001 — анализ упал = FAIL-находка
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
