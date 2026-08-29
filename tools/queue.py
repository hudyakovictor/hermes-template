#!/usr/bin/env python3
"""researchagen — очередь гипотез и приоритетный индекс (PI / PPI / bin).

Формула (веса живут в config.yaml → researchagen.pi_weights, пересчитываются calib.py):

    PI  = w_s*S + w_n*N + w_e*E + w_q*Q + w_m*M + w_d*D  + aging
    PPI = PI / est_hours                 # очков на GPU-час
    bin = P1 (<=4ч) | P2 (<=12ч) | P3 (<=48ч) | P4 (>48ч)

  S  число независимых сигналов: 3→0.50, 4→0.67, 5→0.84, 6+→1.00 (<3 → 0)
  N  publication gap: 1.0 — прямых публикаций нет, 0 — уже опубликовано
  E  ранность: 1% обучения → 1.0, 10% и позже → 0.0 (линейно)
  Q  шанс стать стандартом    M  коммерческий потенциал    D  однозначность PASS/FAIL

aging: +per_day за сутки ожидания, но не больше cap — защита от голодания.

CLI:
  python tools/queue.py add "Название" --signals 4 --hours 6 --early 3 --novelty 0.9 \
         --standard 0.7 --money 0.7 --decidability 0.8 --forecast 12 [--source human]
  python tools/queue.py list [--top N] [--all] [--json]
  python tools/queue.py next [--json]
  python tools/queue.py show H-003 [--json]
  python tools/queue.py set H-003 --hours 8 --signals 5
  python tools/queue.py status H-003 running
  python tools/queue.py archive H-003
  python tools/queue.py stats [--json]
"""

from __future__ import annotations

import sys

import core

# ``tools/queue.py`` is intentionally the profile queue API, but it is also
# discoverable as top-level ``queue`` when ``tools`` is on sys.path.  Several
# stdlib modules (notably concurrent.futures, used by HTTPMCPTransport) require
# ``queue.SimpleQueue``.  Export the stdlib-compatible primitive to avoid a
# namespace collision without renaming the public profile module.
try:
    from _queue import Empty, SimpleQueue
except ImportError:  # pragma: no cover - CPython provides _queue
    from collections import deque
    from threading import Condition

    class Empty(Exception):
        pass

    class SimpleQueue:  # type: ignore[no-redef]
        def __init__(self):
            self._items = deque()
            self._condition = Condition()

        def put(self, item, block=True, timeout=None):
            del block, timeout
            with self._condition:
                self._items.append(item)
                self._condition.notify()

        put_nowait = put

        def get(self, block=True, timeout=None):
            with self._condition:
                if not block and not self._items:
                    raise Empty
                while not self._items:
                    self._condition.wait(timeout)
                    if timeout is not None and not self._items:
                        raise Empty
                return self._items.popleft()

        def get_nowait(self):
            return self.get(False)

        def empty(self):
            with self._condition:
                return not self._items


SIGNAL_SCALE = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.50, 4: 0.67, 5: 0.84}


def signal_score(n: int) -> float:
    if n >= 6:
        return 1.0
    return SIGNAL_SCALE.get(int(n), 0.0)


def early_score(early_pct: float) -> float:
    """1% обучения → 1.0; 10% и позже → 0.0."""
    try:
        pct = float(early_pct)
    except (TypeError, ValueError):
        return 0.0
    if pct <= 1.0:
        return 1.0
    if pct >= 10.0:
        return 0.0
    return (10.0 - pct) / 9.0


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def weights(config: dict | None = None) -> dict:
    base = {"signals": 0.22, "novelty": 0.16, "early": 0.12,
            "standard": 0.14, "money": 0.14, "decidability": 0.22}
    for key in list(base):
        base[key] = float(core.cfg(f"researchagen.pi_weights.{key}", base[key], config))
    total = sum(base.values()) or 1.0
    return {k: v / total for k, v in base.items()}  # нормализация: PI всегда в [0,1]


def aging_bonus(created_at: str | None, config: dict | None = None) -> float:
    per_day = float(core.cfg("researchagen.aging.per_day", 0.05, config))
    cap = float(core.cfg("researchagen.aging.cap", 0.30, config))
    return min(cap, per_day * core.age_days(created_at))


def pi(row, config: dict | None = None, with_aging: bool = True) -> float:
    w = weights(config)
    value = (
        w["signals"] * signal_score(row["signals"])
        + w["novelty"] * clamp01(row["novelty"])
        + w["early"] * early_score(row["early_pct"])
        + w["standard"] * clamp01(row["standard"])
        + w["money"] * clamp01(row["money"])
        + w["decidability"] * clamp01(row["decidability"])
    )
    if with_aging:
        value += aging_bonus(row["created_at"], config)
    return round(value, 4)


def ppi(row, config: dict | None = None) -> float:
    hours = max(0.25, float(row["est_hours"] or 0.25))
    return round(pi(row, config) / hours, 4)


def bin_of(est_hours: float, config: dict | None = None) -> str:
    p1 = float(core.cfg("researchagen.bins.p1_max_hours", 4, config))
    p2 = float(core.cfg("researchagen.bins.p2_max_hours", 12, config))
    p3 = float(core.cfg("researchagen.bins.p3_max_hours", 48, config))
    h = float(est_hours or 0)
    if h <= p1:
        return "P1"
    if h <= p2:
        return "P2"
    if h <= p3:
        return "P3"
    return "P4"


def scored(conn, statuses=core.LIVE_STATUSES, config: dict | None = None) -> list[dict]:
    config = config if config is not None else core.load_config()
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM hypotheses WHERE status IN ({marks})", tuple(statuses)
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["pi"] = pi(row, config)
        item["pi_raw"] = pi(row, config, with_aging=False)
        item["ppi"] = ppi(row, config)
        item["bin"] = bin_of(row["est_hours"], config)
        item["waiting_days"] = round(core.age_days(row["created_at"]), 2)
        items.append(item)
    items.sort(key=lambda i: (-i["ppi"], -i["pi"]))
    return items


def _mii(item: dict) -> float:
    """Money-Impact Index (#19): монетизируемая решаемость на единицу времени.

    При близких PPI (в пределах 10%) очередь обязана тянуть к деньгам:
    первым идёт тот, у кого выше money×decidability/est_hours.
    """
    try:
        hours = max(0.25, float(item.get("est_hours") or 1.0))
        return float(item.get("money") or 0.0) * float(item.get("decidability") or 0.0) / hours
    except (TypeError, ValueError):
        return 0.0


def pick_next(conn, config: dict | None = None) -> dict | None:
    """R3: среди P1/P2 — максимальный PPI; если их нет — максимальный PI (с aging).

    MII-tiebreak (#19): при PPI в пределах 10% друг от друга выигрывает
    гипотеза с большим money×decidability/est_hours — отбор смещён к
    монетизируемому без поломки приоритета ценности на GPU-час.
    """
    candidates = [i for i in scored(conn, ("queued", "paused_checkpoint"), config)]
    if not candidates:
        return None
    cheap = [i for i in candidates if i["bin"] in ("P1", "P2")]
    if cheap:
        cheap.sort(key=lambda i: (-i["ppi"], -i["pi"]))
        best_ppi = cheap[0]["ppi"]
        tied = [i for i in cheap if i["ppi"] >= best_ppi * 0.9] or cheap
        if len(tied) > 1:
            tied.sort(key=_mii, reverse=True)
            chosen = tied[0]
            chosen["reason"] = (f"MII-tiebreak: PPI ~равны (≥0.9×{best_ppi:.2f}), "
                                f"первым — money×decidability/ч = {_mii(chosen):.2f}")
            return chosen
        chosen = cheap[0]
        chosen["reason"] = f"лучший PPI в дешёвых корзинах ({chosen['bin']}): {chosen['ppi']} очков/ч"
        return chosen
    candidates.sort(key=lambda i: (-i["pi"], -i["ppi"]))
    chosen = candidates[0]
    chosen["reason"] = f"P1/P2 пусты → максимальный PI с учётом aging: {chosen['pi']}"
    return chosen


def live_count(conn) -> int:
    marks = ",".join("?" * len(core.LIVE_STATUSES))
    return conn.execute(
        f"SELECT COUNT(*) FROM hypotheses WHERE status IN ({marks})",
        tuple(core.LIVE_STATUSES),
    ).fetchone()[0]


def add(conn, title: str, **kw) -> dict:
    hid = kw.get("hypo_id") or core.next_hypo_id(conn)
    now = core.iso()
    # #2: коридор по умолчанию ±40% вокруг точки — вердикт сравнивает факт
    # не только с точкой, но и с честным диапазоном
    if kw.get("forecast") not in (None, ""):
        f = float(kw["forecast"])
        if kw.get("forecast_low") in (None, ""):
            kw["forecast_low"] = round(f * 0.6, 2)
        if kw.get("forecast_high") in (None, ""):
            kw["forecast_high"] = round(f * 1.4, 2)
    conn.execute(
        "INSERT INTO hypotheses (id, title, status, level, signals, novelty, early_pct,"
        " standard, money, decidability, est_hours, forecast, forecast_low,"
        " forecast_high, p_repro, base_rate, buyer, industry_usecase,"
        " demand_signals, source, card_path,"
        " created_at, updated_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (hid, title, kw.get("status", "queued"), kw.get("level", "L0"),
         int(kw.get("signals", 0)), float(kw.get("novelty", 0.5)),
         float(kw.get("early_pct", 10.0)), float(kw.get("standard", 0.4)),
         float(kw.get("money", 0.4)), float(kw.get("decidability", 0.5)),
         float(kw.get("est_hours", 4.0)),
         None if kw.get("forecast") in (None, "") else float(kw["forecast"]),
         None if kw.get("forecast_low") in (None, "") else float(kw["forecast_low"]),
         None if kw.get("forecast_high") in (None, "") else float(kw["forecast_high"]),
         None if kw.get("p_repro") in (None, "") else float(kw["p_repro"]),
         None if kw.get("base_rate") in (None, "") else float(kw["base_rate"]),
         kw.get("buyer"), kw.get("industry_usecase"),
         int(kw.get("demand_signals", 0) or 0),
         kw.get("source", "dr"), kw.get("card_path"), now, now, kw.get("notes")),
    )
    conn.commit()
    core.log_event(conn, "queue.add", hid, title=title, source=kw.get("source", "dr"))
    return dict(conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone())


def set_status(conn, hid: str, status: str, level: str | None = None) -> None:
    if level:
        conn.execute("UPDATE hypotheses SET status=?, level=?, updated_at=? WHERE id=?",
                     (status, level, core.iso(), hid))
    else:
        conn.execute("UPDATE hypotheses SET status=?, updated_at=? WHERE id=?",
                     (status, core.iso(), hid))
    conn.commit()
    core.log_event(conn, "queue.status", hid, status=status, level=level)


NUMERIC_FIELDS = {"signals": int, "novelty": float, "early_pct": float,
                  "standard": float, "money": float, "decidability": float,
                  "est_hours": float, "forecast": float,
                  "forecast_low": float, "forecast_high": float,
                  "p_repro": float, "base_rate": float,
                  "demand_signals": int, "kill_checks_passed": int}


def update_fields(conn, hid: str, **kw) -> None:
    sets, params = [], []
    for key, caster in NUMERIC_FIELDS.items():
        if kw.get(key) not in (None, ""):
            sets.append(f"{key}=?")
            params.append(caster(kw[key]))
    for key in ("title", "notes", "card_path", "level"):
        if kw.get(key):
            sets.append(f"{key}=?")
            params.append(kw[key])
    if not sets:
        return
    params += [core.iso(), hid]
    conn.execute(f"UPDATE hypotheses SET {', '.join(sets)}, updated_at=? WHERE id=?", params)
    conn.commit()
    core.log_event(conn, "queue.update", hid, **{k: v for k, v in kw.items() if v not in (None, "")})


def render(items: list[dict], top: int | None = None) -> str:
    rows = []
    for i in (items[:top] if top else items):
        rows.append([i["bin"], f"{i['ppi']:.3f}", f"{i['pi']:.3f}",
                     f"{i['est_hours']:.1f}", i["signals"], i["level"],
                     i["status"], i["id"], i["title"][:44]])
    return core.table(rows, ["bin", "PPI", "PI", "час", "сиг", "ур", "статус", "id", "гипотеза"])


def main(argv: list[str]) -> int:
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "list"
    conn = core.db()

    if cmd == "add":
        if len(argv) < 3 or argv[2].startswith("--"):
            core.fail("нужно название: queue.py add \"текст\" --signals 3 --hours 4")
        row = add(
            conn, argv[2],
            signals=core.arg(argv, "signals", 0),
            novelty=core.arg(argv, "novelty", 0.5),
            early_pct=core.arg(argv, "early", 10.0),
            standard=core.arg(argv, "standard", 0.4),
            money=core.arg(argv, "money", 0.4),
            decidability=core.arg(argv, "decidability", 0.5),
            est_hours=core.arg(argv, "hours", 4.0),
            forecast=core.arg(argv, "forecast"),
            source=core.arg(argv, "source", "dr"),
            card_path=core.arg(argv, "card"),
            notes=core.arg(argv, "notes"),
        )
        item = dict(row)
        item["pi"] = pi(row, config)
        item["ppi"] = ppi(row, config)
        item["bin"] = bin_of(row["est_hours"], config)
        core.emit(item, as_json,
                  f"Добавлено {item['id']} — {item['title']}\n"
                  f"PI {item['pi']:.3f} | PPI {item['ppi']:.3f} | {item['bin']} | "
                  f"{item['est_hours']:.1f} ч | сигналов {item['signals']}")
        return 0

    if cmd == "list":
        statuses = core.LIVE_STATUSES + core.CLOSED_STATUSES if core.flag(argv, "all") \
            else core.LIVE_STATUSES
        items = scored(conn, statuses, config)
        top = core.arg(argv, "top")
        top = int(top) if top else None
        nxt = pick_next(conn, config)
        text = render(items, top)
        if nxt:
            text += f"\n\nNEXT → {nxt['id']} «{nxt['title']}» ({nxt['bin']}, PI {nxt['pi']:.3f}, " \
                    f"{nxt['est_hours']:.1f} ч)\nПричина: {nxt['reason']}"
        core.emit({"items": items, "next": nxt}, as_json, text)
        return 0

    if cmd == "next":
        nxt = pick_next(conn, config)
        if not nxt:
            core.emit({"next": None}, as_json, "Очередь пуста: запускать нечего.")
            return 0
        core.emit(nxt, as_json,
                  f"NEXT → {nxt['id']} «{nxt['title']}»\n"
                  f"{nxt['bin']} | PI {nxt['pi']:.3f} | PPI {nxt['ppi']:.3f} | "
                  f"{nxt['est_hours']:.1f} ч | сигналов {nxt['signals']}\nПричина: {nxt['reason']}")
        return 0

    if cmd == "show":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if row is None:
            core.fail(f"{hid} не найдена")
        item = dict(row)
        item.update(pi=pi(row, config), ppi=ppi(row, config),
                    bin=bin_of(row["est_hours"], config))
        lines = [f"{item['id']} — {item['title']}",
                 f"статус {item['status']} | уровень {item['level']} | источник {item['source']}",
                 f"PI {item['pi']:.3f} | PPI {item['ppi']:.3f} | {item['bin']} | "
                 f"оценка {item['est_hours']:.1f} ч",
                 f"сигналов {item['signals']} | ранность {item['early_pct']}% обучения",
                 f"прогноз эффекта {item['forecast']}%" if item["forecast"] is not None
                 else "прогноз не задан (запуск запрещён — сначала фиксируй прогноз)",
                 f"карточка: {item['card_path'] or 'нет'}"]
        core.emit(item, as_json, "\n".join(lines))
        return 0

    if cmd == "set":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        update_fields(
            conn, hid,
            signals=core.arg(argv, "signals"), novelty=core.arg(argv, "novelty"),
            early_pct=core.arg(argv, "early"), standard=core.arg(argv, "standard"),
            money=core.arg(argv, "money"), decidability=core.arg(argv, "decidability"),
            est_hours=core.arg(argv, "hours"), forecast=core.arg(argv, "forecast"),
            kill_checks_passed=core.arg(argv, "kill-checks"),
            title=core.arg(argv, "title"), notes=core.arg(argv, "notes"),
            card_path=core.arg(argv, "card"), level=core.arg(argv, "level"),
        )
        core.emit({"ok": True, "id": hid}, as_json, f"{hid} обновлена")
        return 0

    if cmd == "status":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        status = argv[3] if len(argv) > 3 else core.fail("нужен статус")
        set_status(conn, hid, status, core.arg(argv, "level"))
        core.emit({"ok": True, "id": hid, "status": status}, as_json, f"{hid} → {status}")
        return 0

    if cmd == "archive":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        set_status(conn, hid, "archived")
        core.emit({"ok": True, "id": hid}, as_json, f"{hid} в архиве (данные сохранены)")
        return 0

    if cmd == "stats":
        rows = conn.execute(
            "SELECT status, COUNT(*) n, ROUND(AVG(est_hours),2) h FROM hypotheses GROUP BY status"
        ).fetchall()
        payload = {r["status"]: {"count": r["n"], "avg_hours": r["h"]} for r in rows}
        payload["live"] = live_count(conn)
        text = core.table([[k, v["count"], v["avg_hours"]] for k, v in payload.items()
                           if isinstance(v, dict)], ["статус", "шт", "ср.часов"])
        text += f"\nЖивых гипотез: {payload['live']}"
        core.emit(payload, as_json, text)
        return 0

    core.fail(f"неизвестная команда {cmd!r}. См. докстринг файла.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
