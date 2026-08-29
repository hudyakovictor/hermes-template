#!/usr/bin/env python3
"""researchagen — Telegram Mini App: пульт автономной ИИ-лаборатории.

Только живые данные профиля. Демо-симуляции нет: каждый экран собирается
из вывода штатных CLI (`tools/*.py`) и read-only чтения `state/researchagen.sqlite3`.
Воздействия выполняются теми же командами, что и слеш-команды бота.

Чтение (GET /api/state):
  report.status  → governor, бюджет, текущие прогоны, калибровка
  queue list     → очередь гипотез (PI/PPI, корзины, kill-чеки)
  verdict list   → вердикты «обещали vs получили»
  gpu show       → VRAM/util/температура (или честное «недоступен»)
  crew replay    → живой чат экипажа (включая споры)
  crew review    → замечания взаимного ревью
  crew stats     → статистика чата и счёт ставок
  rg bets        → открытые/закрытые ставки агентов

Действия (POST /api/action) — те же CLI, что у бота:
  pause/resume → rg.py pause|resume       approve → rg.py approve H-XXX
  kill_task    → rg.py kill H-XXX         run_level → rg.py launch H-XXX --level L
  run_check    → rg.py check H-XXX (гейт) submit_idea → rg.py idea "текст"

Запуск: python miniapp/server.py --port 8787
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))

PY = sys.executable or "python3"
DB_PATH = os.path.join(ROOT, "state", "researchagen.sqlite3")

APPROVAL_HOURS = 12.0     # researchagen.limits.approval_gpu_hours
DAILY_HOURS = 20.0        # researchagen.limits.daily_gpu_hours_budget
LIVE_STATUSES = ("queued", "running", "paused_checkpoint", "blocked")


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "tools", f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:                                    # реальный список проверок и экипажа из кода профиля
    _hypo = _load_tool("hypo")
    KILL_CHECKS = list(_hypo.KILL_CHECKS)
except Exception:                       # pragma: no cover
    KILL_CHECKS = []
try:
    _crew = _load_tool("crew")
    AGENTS = [{"id": k, "name": v["name"], "zone": v["zone"],
               "short": v["zone"].split(":")[0]} for k, v in _crew.AGENTS.items()]
except Exception:                       # pragma: no cover
    AGENTS = []


def cli(*args: str, timeout: int = 30):
    """Штатный CLI профиля → JSON. Любой сбой = исключение (наверху решаем)."""
    proc = subprocess.run([PY, *args], capture_output=True, text=True,
                          timeout=timeout, check=False, cwd=ROOT)
    out = (proc.stdout or "").strip()
    if not out:
        return None
    if out[0] in "[{":
        return json.loads(out)
    return out


def db_setting(key: str, default=None):
    """Мелкие флаги читаем прямо из SQLite (read-only), не дёргая CLI."""
    if not os.path.exists(DB_PATH):
        return default
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default
        finally:
            conn.close()
    except Exception:
        return default


def db_today_launches() -> int | None:
    if not os.path.exists(DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
        try:
            day = time.strftime("%Y-%m-%d")
            n = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind='dispatch.launched' "
                "AND created_at LIKE ?", (day + "%",)).fetchone()[0]
            return int(n)
        finally:
            conn.close()
    except Exception:
        return None


HID_RE = re.compile(r"\b(H-\d{3,4})\b")


def age_days(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
    except Exception:
        return 0.0


def map_queue_item(it: dict, approved: list) -> dict:
    return {
        "id": it["id"], "title": it["title"], "status": it["status"],
        "level": it.get("level") or "—", "bin": it.get("bin") or "",
        "est_hours": it.get("est_hours") or 0, "signals": it.get("signals") or 0,
        "pi": round(it.get("pi") or 0, 3), "ppi": round(it.get("ppi") or 0, 2),
        "checks_pass": it.get("kill_checks_passed") or 0,
        "source": it.get("source") or "dr", "age_days": round(age_days(it.get("created_at")), 1),
        "forecast": it.get("forecast"), "forecast_low": it.get("forecast_low"),
        "forecast_high": it.get("forecast_high"), "unit": "",
        "approved": it["id"] in (approved or []),
        "note": it.get("notes") or "",
        "card": bool(it.get("card_path")),
    }


def map_verdict(v: dict) -> dict:
    dev = v.get("deviation")
    return {
        "id": f"V-{v.get('verdict_id')}", "hid": v.get("hypo_id"),
        "kind": v.get("kind"), "title": v.get("title") or v.get("hypo_id"),
        "level": v.get("level"), "forecast": v.get("forecast"),
        "actual": v.get("actual"), "deviation": dev,
        "seeds_pass": v.get("seeds_pass") or 0, "seeds_total": v.get("seeds_total") or 0,
        "sigma": v.get("sigma"), "gpu_hours": v.get("gpu_hours") or 0,
        "changes": v.get("what_changes") or "", "ts": v.get("created_at"),
        "unit": "", "commercial": None, "patent": None, "next": "",
    }


class LiveLab:
    """Собирает состояние Mini App из живого профиля."""

    def __init__(self):
        self._lock = threading.Lock()

    # -------------------------------------------------------------- чтение
    def state(self) -> dict:
        with self._lock:
            return self._state()

    def _state(self) -> dict:
        st = cli("tools/rg.py", "status", "--json") or {}
        q = cli("tools/queue.py", "list", "--all", "--json") or {"items": []}
        verdicts_raw = cli("tools/verdict.py", "list", "--limit", "40", "--json") or []
        gpu_raw = cli("tools/gpu.py", "show", "--json")
        chat_raw = cli("tools/crew.py", "replay", "-n", "60", "--json") or []
        review_raw = cli("tools/crew.py", "review", "--json") or {}
        stats_raw = cli("tools/crew.py", "stats", "--json") or {}
        bets_raw = cli("tools/rg.py", "bets", "--json") or {"open": [], "resolved": []}
        ideas_log = cli("tools/rg.py", "ideas", "--json") or []

        approved = db_setting("dispatch.approved", []) or []
        tasks_used = db_today_launches()

        # ---- GPU: честное «недоступен», если карты нет
        gpu = {"available": False, "name": "GPU", "total_gb": 0, "used_gb": 0,
               "free_gb": 0, "util": 0, "temp": 0}
        if isinstance(gpu_raw, list) and gpu_raw:
            g0 = gpu_raw[0]
            gpu = {"available": True,
                   "name": g0.get("name") or "GPU",
                   "total_gb": g0.get("memory_total") or 0,
                   "used_gb": g0.get("memory_used") or 0,
                   "free_gb": g0.get("memory_free") or 0,
                   "util": g0.get("utilization") or 0, "temp": g0.get("temperature") or 0}
        elif isinstance(gpu_raw, dict) and gpu_raw.get("gpus"):
            g0 = gpu_raw["gpus"][0]
            gpu = {"available": True, "name": g0.get("name") or "GPU",
                   "total_gb": g0.get("memory_total") or g0.get("total_gb") or 0,
                   "used_gb": g0.get("memory_used") or g0.get("used_gb") or 0,
                   "free_gb": g0.get("memory_free") or g0.get("free_gb") or 0,
                   "util": g0.get("utilization") or g0.get("util") or 0,
                   "temp": g0.get("temperature") or g0.get("temp") or 0}

        # ---- очередь и approvals (R7: дороже порога и не подтверждено)
        queue = [map_queue_item(i, approved) for i in q.get("items", [])]
        approvals = [{
            "id": a["id"], "hid": a["id"], "title": a["title"], "level": a["level"],
            "hours": a["est_hours"], "ppi": a["ppi"], "bin": a["bin"],
            "note": f"{a['est_hours']} GPU-ч > порога {APPROVAL_HOURS:.0f} ч (/approve)",
        } for a in queue if a["status"] == "queued"
            and a["est_hours"] > APPROVAL_HOURS and not a["approved"]]

        # ---- текущий прогон: только факты из experiments
        cur = None
        runs = st.get("running") or []
        if runs:
            r0 = runs[0]
            elapsed = None
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(r0["started_at"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            except Exception:
                pass
            cur = {"hid": r0.get("hypo_id"), "level": r0.get("level"),
                   "status": "running", "elapsed_min": round(elapsed or 0),
                   "dry_run": bool(r0.get("dry_run")), "progress": None,
                   "eta_min": None, "steps": None, "steps_total": None,
                   "seed": None, "seeds_total": None,
                   "loss_now": None, "base_now": None}

        # ---- экипаж
        chat = []
        for m in chat_raw:
            chat.append({
                "agent": m.get("agent"), "name": m.get("name"),
                "text": m.get("text") or "", "kind": m.get("kind") or "work",
                "ts": m.get("ts"), "hid": (HID_RE.search(m.get("text") or "") or [None])[1]
                if HID_RE.search(m.get("text") or "") else None,
                "dispute_id": m.get("dispute_id"),
            })
        remarks = []
        KIND_RU = {
            "review_patent_candidate": ("hronik", "патентный кандидат без заявки: деньги лежат на столе"),
            "review_fake_check": ("morg", "галочка kill-check без доказательства"),
            "review_no_forecast": ("krot", "прогноз не зафиксирован до запуска"),
            "review_stale": ("shef", "гипотеза гниёт в очереди без движения"),
            "review_dup_signal": ("skif", "дубль сигнала: зависимые источники прошли как независимые"),
            "review_calibration": ("hronik", "сдвиг калибровки прогнозов"),
        }
        for status_key in ("fresh", "open"):
            for f in review_raw.get(status_key) or []:
                kind = f.get("kind") or ""
                who, txt = KIND_RU.get(kind, ("morg", kind.replace("review_", "").replace("_", " ")))
                remarks.append({
                    "id": f.get("id") or "", "from": who, "to": "экипаж",
                    "hid": f.get("subject") or (f.get("details") or {}).get("hid"),
                    "text": txt + (f" — {f.get('subject')}" if f.get("subject") else ""),
                    "status": "open", "ts": f.get("ts"),
                })
        for f in review_raw.get("resolved") or []:
            kind = f.get("kind") or ""
            who, txt = KIND_RU.get(kind, ("morg", kind.replace("review_", "").replace("_", " ")))
            remarks.append({
                "id": f.get("id") or "", "from": who, "to": "экипаж",
                "hid": f.get("subject") or (f.get("details") or {}).get("hid"),
                "text": txt + " — закрыто", "status": "closed", "ts": f.get("ts"),
            })
        # ставки: открытые группируем по гипотезе, закрытые дают рейтинг точности
        by_id = {h["id"]: h for h in queue}
        name_of = {a["id"]: a["name"] for a in AGENTS}
        open_bets: dict[str, dict] = {}
        for b in bets_raw.get("open") or []:
            hid = b.get("hypo_id")
            g = open_bets.setdefault(hid, {"hid": hid, "title": by_id.get(hid, {}).get("title") or hid,
                                           "status": by_id.get(hid, {}).get("status") or "queued",
                                           "up": [], "down": []})
            (g["up"] if b.get("bet") in ("confirmed", "partial") else g["down"]).append(
                b.get("name") or name_of.get(b.get("agent")) or b.get("agent"))
        leaders = []
        for b in bets_raw.get("resolved") or []:
            leaders.append({"agent": b.get("agent"), "rate": b.get("hit_rate") or 0,
                            "bets": b.get("bets") or 0, "brier": b.get("brier"),
                            "streak": 0})

        verdicts = [map_verdict(v) for v in verdicts_raw]
        cal = st.get("calibration") or {}

        return {
            "mode": "live", "ts": time.time(),
            "gpu": gpu,
            "gov": {
                "autostart": not st.get("paused"),
                "budget_hours": {"limit": st.get("gpu_hours_budget") or DAILY_HOURS,
                                 "used": st.get("gpu_hours_today") or 0.0},
                "budget_tasks": {"limit": 12, "used": tasks_used if tasks_used is not None else 0},
                "has_tasks_counter": tasks_used is not None,
                "approval_hours": APPROVAL_HOURS,
                "platform": st.get("platform"), "debug": st.get("debug"),
            },
            "approvals": approvals,
            "queue": queue,
            "current": cur,
            "runs": [],
            "history": [],
            "crew": {"agents": AGENTS, "chat": chat, "remarks": remarks,
                     "leaders": leaders, "bets": list(open_bets.values()),
                     "chat_total": stats_raw.get("total_lines") or 0},
            "verdicts": verdicts,
            "stats": {
                "calibration": (100 - cal["mean_abs_deviation_pct"])
                if isinstance(cal.get("mean_abs_deviation_pct"), (int, float)) else None,
                "win_rate": cal.get("hit_rate"),
                "verdicts_total": cal.get("verdicts") or len(verdicts),
                "queue_len": sum(1 for h in queue if h["status"] in LIVE_STATUSES),
                "open_remarks": sum(1 for r in remarks if r["status"] == "open"),
            },
            "checks": KILL_CHECKS,
            "ideas_log": ideas_log,
        }

    # -------------------------------------------------------------- действия
    def act(self, body: dict) -> dict:
        t = body.get("type")
        with self._lock:
            if t == "pause":
                return self._ok(cli("tools/rg.py", "pause", "--json"))
            if t == "resume":
                return self._ok(cli("tools/rg.py", "resume", "--json"))
            if t == "kill_task":
                hid = body.get("hid") or ""
                return self._ok(cli("tools/rg.py", "kill", hid, "--json"))
            if t == "approve":
                hid = body.get("hid") or ""
                if body.get("ok"):
                    return self._ok(cli("tools/rg.py", "approve", hid, "--json"))
                return self._ok(cli("tools/queue.py", "archive", hid, "--json"))
            if t == "run_level":
                hid, level = body.get("hid") or "", body.get("level") or "L0"
                res = cli("tools/rg.py", "launch", hid, "--level", level, "--json")
                out = self._ok(res)
                if isinstance(res, dict) and res.get("ok") is False:
                    reason = str(res.get("reason") or "")
                    if "approve" in reason.lower() or "час" in reason:
                        out["approval"] = True
                return out
            if t == "run_check":
                hid = body.get("hid") or ""
                res = cli("tools/rg.py", "check", hid, "--json")
                out = self._ok(res)
                if isinstance(res, dict):
                    out["problems"] = res.get("problems") or []
                return out
            if t == "idea_check":
                return {"ok": True, **self._idea_check(body.get("text") or "")}
            if t == "submit_idea":
                return self._ok(cli("tools/rg.py", "idea", body.get("text") or "", "--json"))
            if t == "vote":
                return {"ok": False,
                        "err": "в живом контуре споры закрывает арбитраж Boss (числом из базы); голосование человека не предусмотрено"}
        return {"ok": False, "err": f"неизвестное действие: {t}"}

    @staticmethod
    def _ok(res):
        if isinstance(res, dict):
            return res
        return {"ok": True, "raw": str(res)[:400]}

    def _idea_check(self, text: str) -> dict:
        """Дубликаты: сравнение с живой очередью и логом идей (та же логика, что /idea)."""
        stop = {"и", "в", "на", "с", "по", "как", "что", "это", "для", "при", "не",
                "до", "из", "за", "от", "об", "или", "то"}

        def words(s):
            return {w for w in re.sub(r"[^\w\s]", " ", (s or "").lower()).split()
                    if len(w) > 3 and w not in stop}

        tw = words(text)
        matches = []
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
            rows = conn.execute(
                "SELECT id, title, status FROM hypotheses").fetchall()
            conn.close()
        except Exception:
            rows = []
        for hid, title, status in rows:
            sim = len(tw & words(title)) / max(8, min(len(tw), len(words(title))) + 4) if tw and title else 0
            if sim >= 0.18:
                matches.append({"id": hid, "title": title,
                                "why": status or "", "sim": round(sim, 2)})
        matches.sort(key=lambda m: -m["sim"])
        quality, notes = 0, []
        if len(text) > 60:
            quality += 25
        else:
            notes.append("слишком коротко: механизм не виден")
        if any(ch.isdigit() for ch in text):
            quality += 25
        else:
            notes.append("нет чисел: PASS/FAIL не сформулировать")
        mech = ("если", "то", "потому", "механизм", "вызывает", "предсказывает",
                "коррелирует", "приводит")
        if any(w in text.lower() for w in mech):
            quality += 25
        else:
            notes.append("нет причинной связки «если X, то Y»")
        banned = ("перспективно", "многообещающе", "возможно улучшение",
                  "выглядит интересно", "promising")
        if any(w in text.lower() for w in banned):
            notes.append("запрещённые слова («перспективно» и родственные) — вердикт такое не примет")
        else:
            quality += 25
        return {"matches": matches[:4], "quality": min(quality, 100), "notes": notes}


LAB = LiveLab()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _file(self, name: str):
        path = os.path.join(APP_DIR, name)
        if not os.path.isfile(path) or not os.path.abspath(path).startswith(APP_DIR):
            self._json({"err": "not found"}, 404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8"}.get(
                     os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            try:
                self._json(LAB.state())
            except Exception as exc:
                self._json({"mode": "live", "error": str(exc)[:300]}, 500)
            return
        if self.path.startswith("/api/ping"):
            self._json({"ok": True, "mode": "live", "ts": time.time()})
            return
        name = self.path.split("?")[0].lstrip("/") or "index.html"
        self._file(name)

    def do_POST(self):
        if not self.path.startswith("/api/action"):
            self._json({"err": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "err": "bad json"}, 400)
            return
        try:
            self._json(LAB.act(body))
        except Exception as exc:
            self._json({"ok": False, "err": str(exc)[:300]}, 500)


def main():
    ap = argparse.ArgumentParser(description="Telegram Mini App — researchagen (live)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"[miniapp] {url}  (live: {ROOT})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[miniapp] стоп")


if __name__ == "__main__":
    main()
