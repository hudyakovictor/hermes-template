#!/usr/bin/env python3
"""researchagen — симулятор чата экипажа (aichat) и взаимного ревью.

Прогоняет N независимых «рабочих дней» экипажа на временной SQLite-базе:
случайный поток событий (гипотезы, гейты, запуски, вердикты, ревью, дайджесты,
мути, сбои доставки) с детерминированным зерном. После каждого шага проверяет
инварианты. Это стресс-тест: он ловит шаблоны, которые падают на пустых
данных, ломают баланс 85/15, бюджет или формат сообщений.

CLI:
  python tools/crew_sim.py [--sims 20] [--seed 1] [--json] [--verbose]
Выход: 0 — все симуляции чистые; 1 — были ошибки (список напечатан).
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
from datetime import timedelta
from unittest import mock

import core
import crew

# События и веса «рабочего дня»: рабочее — часто, «шёпот» — сам просится.
EVENTS = [
    ("hypo_new", 12), ("customer_lead", 8), ("gate_pass", 6), ("gate_fail", 8),
    ("launch", 10), ("finish_ok", 6), ("finish_fail", 4), ("preempt", 3),
    ("verdict_confirmed", 5), ("verdict_rejected", 7), ("verdict_partial", 4),
    ("kill", 4), ("queue_empty", 5), ("digest", 3), ("weekly", 2),
    ("budget_burn", 3), ("mode_change", 2), ("agi_day", 1), ("мусорное", 2),
]

CTX_KEYS = ["hid", "forecast", "actual", "dev", "hours", "seeds", "passed",
            "total", "budget", "burn", "free", "level", "pct", "ratio", "mode",
            "min", "signals", "money", "challenger", "open_findings", "bias"]


def make_state(conn, rng, tmp):
    """Случайное «наследие» в базе: косяки для ревью + нормальные объекты."""
    created = core.iso(core.now() - timedelta(days=rng.randint(0, 12)))
    inserted = []
    for i in range(rng.randint(0, 6)):
        hid = f"H-{900 + i}"
        inserted.append(hid)
        status = rng.choice(["queued", "queued", "confirmed", "rejected"])
        signals = rng.choice([1, 2, 3, 5])
        forecast = rng.choice([None, 5.0, 12.0])
        money = rng.choice([0.2, 0.5, 0.7])
        card = ""
        if rng.random() < 0.5:  # часть карточек с ложной галочкой
            card = os.path.join(tmp, f"{hid}.yaml")
            with open(card, "w", encoding="utf-8") as fh:
                fh.write('kill_checks:\n  - check: "simple explanation"\n'
                         '    passed: true\n    evidence: ""\n')
        conn.execute(
            "INSERT OR IGNORE INTO hypotheses (id, title, status, level, signals,"
            " novelty, early_pct, standard, money, decidability, est_hours,"
            " forecast, kill_checks_passed, source, card_path, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (hid, "sim " + hid, status, "L0", signals, 0.5, 10.0, 0.4, money,
             0.5, 2.0, forecast, 0, "dr", card or None, created, created))
    if inserted and rng.random() < 0.4:  # зависший прогон (FK: гипотеза есть)
        stale = core.iso(core.now() - timedelta(days=2))
        conn.execute(
            "INSERT INTO runs (hypo_id, level, state, started_at, gpu_hours)"
            " VALUES (?,'L1','running',?,0.5)", (inserted[0], stale))
    for _ in range(rng.randint(0, 8)):  # вердикты для калибровки
        if not inserted:
            break
        conn.execute(
            "INSERT INTO verdicts (hypo_id, level, kind, forecast, actual,"
            " deviation, seeds_pass, seeds_total, gpu_hours, what_changes,"
            " created_at) VALUES (?,'L1','rejected',10,?, ?, 1, 3, 0.4,"
            " 'sim', ?)",
            (inserted[0], -rng.uniform(1, 9), -rng.uniform(20, 60), core.iso()))
    # сигналы, иногда дубли
    os.makedirs(core.SIGNALS_DIR, exist_ok=True)
    first_line = None
    for i in range(rng.randint(0, 4)):
        fname = os.path.join(core.SIGNALS_DIR, f"S-{i:03d}.md")
        line = first_line or f"signal {rng.randint(0, 99)}"
        with open(fname, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if first_line is None:
            first_line = line
    conn.commit()


def random_ctx(rng) -> dict:
    ctx = {}
    for key in rng.sample(CTX_KEYS, rng.randint(0, 8)):
        if key in ("hid", "level", "mode", "challenger"):
            ctx[key] = rng.choice(["H-001", "H-042", None])
        elif key in ("forecast", "actual", "dev", "hours", "budget", "burn",
                     "free", "pct", "ratio", "min", "money", "bias",
                     "open_findings"):
            ctx[key] = rng.choice([0, 3, 12.5, -4.9, 20, "0.4", "+41%", None])
        else:
            ctx[key] = rng.choice([0, 3, 7, "3/3", None])
    return ctx


class Invariants:
    def __init__(self, config):
        self.config = config
        self.failures: list[str] = []

    def check_lines(self, lines, where):
        names = {a["name"] for a in crew.AGENTS.values()}
        for line in lines:
            text = line.get("text", "")
            if not str(text).strip():
                self.failures.append(f"{where}: пустая реплика {line}")
            if "{" in text or "}" in text:
                self.failures.append(f"{where}: незамкнутый шаблон: {text!r}")
            if "None" in text:
                self.failures.append(f"{where}: None в тексте: {text!r}")
            if len(str(text)) > 200:
                self.failures.append(f"{where}: реплика длиннее 200: {text!r}")
            if line["agent"] not in crew.AGENTS:
                self.failures.append(f"{where}: неизвестный агент {line['agent']}")
            msg = crew.compose_message([line])
            if not any(f"*{n}:*" in msg for n in names):
                self.failures.append(f"{where}: битый формат ника: {msg!r}")
        dispute = [l for l in lines if l.get("dispute_id")]
        if dispute and dispute[-1]["agent"] != "shef":
            self.failures.append(f"{where}: спор не закрыт Boss-арбитражем")
        if dispute and not dispute[-1].get("arbiter"):
            self.failures.append(f"{where}: арбитраж не помечен")

    def check_db(self, conn, where, budget_cap):
        try:
            for pool, slack in (("customer", 0.03), ("noise", 0.03)):
                share = crew._side_share(conn, pool)
                cap = float(crew.cfg(f"{pool}_share_max", self.config))
                if share > cap + slack:
                    rows = conn.execute(
                        "SELECT event, agent, kind FROM crew_chat "
                        "ORDER BY msg_id DESC LIMIT 25").fetchall()
                    tail = ", ".join(f"{r['event']}/{r['kind'][0]}" for r in reversed(rows))
                    self.failures.append(
                        f"{where}: пул {pool} {share:.0%} выше потолка {cap:.0%}; хвост: {tail}")
                    return
            batches = crew.sent_today(conn)
            if batches > budget_cap:
                self.failures.append(
                    f"{where}: бюджет доставки превышен: {batches}>{budget_cap}")
            for row in conn.execute(
                    "SELECT agent, name FROM crew_chat").fetchall():
                if row["agent"] not in crew.AGENTS:
                    self.failures.append(f"{where}: мусорный агент в базе")
                    break
            broken = conn.execute(
                "SELECT COUNT(*) c FROM crew_chat WHERE text LIKE '%{%'",).fetchone()
            if broken["c"]:
                self.failures.append(f"{where}: шаблонные скобки в базе")
            zombie = conn.execute(
                "SELECT COUNT(*) c FROM crew_findings WHERE status NOT IN"
                " ('open','fixed')").fetchone()
            if zombie["c"]:
                self.failures.append(f"{where}: мусорный статус замечания")
        except Exception as exc:  # noqa: BLE001
            self.failures.append(f"{where}: инварианты упали: {exc}")


def run_sim(index: int, seed: int, verbose: bool = False) -> list[str]:
    rng = random.Random(seed + index)
    tmp_ctx = tempfile.TemporaryDirectory()
    # сигналы пишем в песочницу, не в репозиторий
    signals_sandbox = os.path.join(tmp_ctx.name, "signals")
    os.makedirs(signals_sandbox, exist_ok=True)
    core_signals_dir, core.SIGNALS_DIR = core.SIGNALS_DIR, signals_sandbox
    conn = core.db(os.path.join(tmp_ctx.name, "state.sqlite3"))
    config = {
        "researchagen": {
            "platform": "macos", "mode": "debug",
            "limits": {"daily_gpu_hours_budget": 20},
            "crew": {
                "enabled": True, "max_messages_per_day": 30,
                "max_lines_per_event": 5,
                "dispute_probability": rng.choice([0.1, 0.35, 0.9]),
                "nudge_probability": rng.choice([0.0, 0.2, 0.6]),
                "customer_share_max": 0.06, "noise_share_max": 0.03,
                "customer_line_probability": rng.choice([0.0, 0.25, 0.8]),
                "noise_line_probability": rng.choice([0.0, 0.1, 0.5]),
                "quiet_hours": "", "agi_arrival": "2030-05-01",
            },
        }
    }
    budget_cap = int(config["researchagen"]["crew"]["max_messages_per_day"])
    inv = Invariants(config)

    make_state(conn, rng, tmp_ctx.name)

    delivery_modes = [
        {"TELEGRAM_AICHAT_THREAD_ID": "777"},            # топик есть
        {"TELEGRAM_CHAT_THREAD_ID": "555"},              # старая переменная
        {},                                               # топика нет
    ]
    log = []
    with mock.patch.object(crew.tg, "send") as tg_send:
        tg_send.return_value = {"ok": True}
        if rng.random() < 0.2:
            tg_send.side_effect = RuntimeError("network down")   # сбой доставки
        for step in range(rng.randint(40, 120)):
            with mock.patch.dict(os.environ, rng.choice(delivery_modes), clear=False):
                event = rng.choices([e for e, _ in EVENTS],
                                    weights=[w for _, w in EVENTS])[0]
                if event == "мусорное":
                    event = rng.choice(["нет такого события", "climate_collapse"])
                try:
                    res = crew.emit(event, random_ctx(rng), conn=conn,
                                    config=config, rng=random.Random(seed + index * 1000 + step))
                    if res["ok"]:
                        inv.check_lines(res["lines"], f"сим{index} шаг{step} {event}")
                        if "None" in crew.compose_message(res["lines"]):
                            inv.failures.append(f"сим{index} шаг{step}: None в сообщении")
                except Exception as exc:  # noqa: BLE001
                    inv.failures.append(f"сим{index} шаг{step} {event}: исключение {exc!r}")
            if rng.random() < 0.25:
                try:
                    crew.run_review(conn, config)
                    crew.safe_review(conn, config)
                except Exception as exc:  # noqa: BLE001
                    inv.failures.append(f"сим{index} шаг{step}: ревью упало {exc!r}")
            if rng.random() < 0.1:
                try:
                    crew.set_mute(conn, rng.choice(["2h", "30m", "off"]))
                except Exception as exc:  # noqa: BLE001
                    inv.failures.append(f"сим{index} шаг{step}: мьют упал {exc!r}")
            inv.check_db(conn, f"сим{index} шаг{step}", budget_cap)
        # финальные проверки: replay/stats/панель чтения
        try:
            items = crew.replay(conn, 200)
            text = crew.replay_text(items)
            if items and ("{" in text or "None" in text):
                inv.failures.append(f"сим{index}: битый replay")
            data = crew.stats(conn, config)
            if data["cost"] != {"gpu_hours": 0.0, "tokens": 0}:
                inv.failures.append(f"сим{index}: ненулевая цена чата")
        except Exception as exc:  # noqa: BLE001
            inv.failures.append(f"сим{index}: чтение упало {exc!r}")
    log += inv.failures
    if verbose:
        n = conn.execute("SELECT COUNT(*) c FROM crew_chat").fetchone()["c"]
        print(f"  сим {index:02d}: шагов {step + 1}, реплик {n}, "
              f"ошибок {len(inv.failures)}")
    conn.close()
    core.SIGNALS_DIR = core_signals_dir
    tmp_ctx.cleanup()
    return log


def main(argv: list[str]) -> int:
    sims = int(core.arg(argv, "sims", 20) or 20)
    seed = int(core.arg(argv, "seed", 1) or 1)
    as_json = core.wants_json(argv)
    verbose = core.flag(argv, "verbose")

    print(f"Симулятор aichat: {sims} симуляций, зерно {seed}")
    all_failures: list[str] = []
    for i in range(sims):
        all_failures += run_sim(i, seed, verbose)
    summary = {"sims": sims, "failures": len(all_failures),
               "unique": sorted(set(all_failures))}
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        if all_failures:
            print(f"ОШИБКИ ({len(all_failures)}), уникальные:")
            for f in summary["unique"]:
                print("  •", f)
        else:
            print(f"OK: {sims}/{sims} симуляций чистые. Инварианты держатся:")
            print("  • формат «Ник:» без мусора и None;")
            print("  • споры закрыты арбитражем Boss;")
            print("  • пулы customer ~5% и noise ~2% в пределах потолков;")
            print("  • бюджет доставок и статусы замечаний целы.")
    return 1 if all_failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
