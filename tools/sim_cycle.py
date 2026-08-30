#!/usr/bin/env python3
"""Симуляция 30 исследовательских итераций: «весь экипаж работает».

Каждая итерация проходит путь из скилла /dr и docs/ARCHITECTURE.md — теми же
штатными функциями контура, которыми работала бы модель по инструкциям:

  Фаза 4  идеи → ideas.submit (dup-check) → ideas.triage (PI/PPI, корзина)
  Фаза 2/3 карточка → kill-стадия (слабые снимаются hypo.kill — успех контура)
  прогон  governor.acquire_experiment → run → dispatch.finish (checkpoint)
  Фаза 5  verdict.record → analyze-барьер закрыт → discover

Всё в изолированной временной базе (патчи как в tests/). Отчёт: какие задачи
сформировались, артефакты по зонам агентов, governor-фазы, NEXT по PPI,
калибровка. Запуск: python3 tools/sim_cycle.py [--json]
"""
from __future__ import annotations

import os
import random
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core          # noqa: E402
import crew          # noqa: E402
import dispatch      # noqa: E402
import governor      # noqa: E402
import hypo          # noqa: E402
import ideas         # noqa: E402
import inbox         # noqa: E402
import queue as q    # noqa: E402
import tg            # noqa: E402
import verdict as v  # noqa: E402

IDEAS = [  # (текст, сигналы, часы, ранний %, слабая ли — не переживёт kill-стадию)
    ("градиентный шум растёт за две эпохи до переобучения", 5, 2, 4, False),
    ("норма весов как дешёвый фильтр слабых прогонов", 4, 1.5, 3, False),
    ("sharpness минимум предсказывает лучший сид", 3, 3, 6, False),
    ("доля мёртвых нейронов коррелирует с обобщением", 3, 2.5, 5, False),
    ("batch noise маскирует grokking переход", 4, 4, 8, False),
    ("lr warmup можно обрезать по кривуре нормы градиента", 3, 1, 2, False),
    ("веса консолидируются до падения train loss", 5, 3, 5, False),
    ("спектр гессиана сжимается раньше метрики", 3, 6, 7, True),
    ("уравновешивание классов ломает neural collapse", 4, 2, 4, False),
    ("ранний stop по кривой effective rank", 4, 1.5, 3, False),
    ("momentum помогает только в первой фазе", 3, 2, 6, False),
    ("чистка признаков по активациям экономит треть эпох", 4, 2.5, 4, False),
    ("переход на половинную точность сдвигает оптимум", 3, 5, 9, True),
    ("дубли сигналов в датасете тянут ложную уверенность", 5, 1, 2, False),
    ("walker-расстояние между чекпойнтами как метрика сходимости", 4, 2, 5, False),
    ("стохастическая глубина ускоряет выход на плато", 3, 3, 6, False),
    ("кластеризация ошибок предсказывает hard-примеры", 4, 1.5, 4, False),
    ("эластичность весов меняет знак при grokking", 3, 2.5, 5, False),
    ("уровень regуляризации читается по рангу фич", 4, 2, 4, False),
    ("кривая обучения сама предсказывает свой перегиб", 5, 2.5, 3, False),
    ("fine-tune живёт дольше с малым lr первого слоя", 3, 1.5, 6, False),
    ("ширина сети маскирует ранние сигналы", 3, 2, 7, True),
    ("осцилляции градиента — предвестник нестабильности", 4, 1, 3, False),
    ("temperature логитов растёт при переобучении", 4, 2, 5, False),
    ("transfer лучше с заморозкой первых трети слоёв", 3, 3, 6, False),
    ("пакетная норма скрывает эффект batch size", 3, 2, 8, True),
    ("обрезка по важности безопаснее на ранних эпохах", 4, 2.5, 4, False),
    ("дрейф признаков между доменами виден по рангу", 5, 3, 5, False),
    ("контрастивный pretrain сужает пространство быстрее", 4, 1.5, 3, False),
    ("loss-ландшафт читается по траектории норм градиента", 4, 2, 5, False),
]

KINDS = ("confirmed", "partial", "rejected")


def fill_card(path: str, rng: random.Random) -> None:
    """Фаза 2/3 по скиллу: заполнить секции и честно закрыть kill-чеки."""
    text = open(path, encoding="utf-8").read()
    fill = {
        "    claim: \"\"": "    claim: \"аналогия наблюдается в двух базах и нашем smoke-прогоне\"",
        "    source: \"\"": "    source: \"arXiv + собственный прогон\"",
        "mechanism: |": "mechanism: |\n  причинная цепочка: консолидация направления градиентов уменьшает\n  разброс обновлений и раньше проявляется в косвенных метриках",
        "why_missed: |": "why_missed: |\n  эффект маскировался шумом больших датасетов и усреднением по эпохам",
        "  metric: \"\"": "  metric: \"корреляция Спирмена ранней метрики с итоговым качеством\"",
        "  pass_if: \"\"": "  pass_if: \"rho >= 0.6\"",
        "  fail_if: \"\"": "  fail_if: \"rho < 0.4\"",
        "falsification: |": "falsification: |\n  контроль с перемешанными метками: корреляция обязана исчезнуть",
        "industry_usecase: |": "industry_usecase: |\n  что меняет: ранний критерий остановки. у кого: HPO-платформы.\n  как измерят: доля сэкономленных GPU-ч скрининга",
        "forecast: null": "forecast: 12",
        "forecast_low: null": "forecast_low: 8",
        "forecast_high: null": "forecast_high: 16",
        "p_repro: null": "p_repro: 0.5",
        "base_rate: null": "base_rate: 0.35",
        "forecast: None": "forecast: 12",
        "forecast_low: None": "forecast_low: 8",
        "forecast_high: None": "forecast_high: 16",
        "p_repro: None": "p_repro: 0.5",
        "base_rate: None": "base_rate: 0.35",
        "impact:": "impact:",
    }
    for old, new in fill.items():
        text = text.replace(old, new, 1)
    text = text.replace("passed: false", "passed: true")
    text = re.sub(r'evidence: ""', 'evidence: "проверено по двум базам и контролю"', text)
    open(path, "w", encoding="utf-8").write(text)


def main() -> int:
    as_json = "--json" in sys.argv
    tmp = tempfile.TemporaryDirectory()
    core.allow_root(tmp.name)
    db_path = os.path.join(tmp.name, "sim.sqlite3")
    rng = random.Random(7)
    config = core.load_config()
    config["researchagen"] = dict(config.get("researchagen") or {})
    config["researchagen"]["mode"] = "debug"   # dry-run: GPU не нужен
    config["researchagen"]["crew"] = dict(config["researchagen"].get("crew") or {},
                                          dispute_probability=0.35,
                                          joke_probability=0.3)

    log = []            # (итерация, фаза/зона, артефакт)
    zones = {"stazhor": 0, "skif": 0, "krot": 0, "morg": 0, "gayka": 0,
             "hronik": 0, "shef": 0}
    gov_phases = []
    verds = []
    real_db = core.db  # до патча: патченный core.db не должен звать сам себя

    def _db(path=None):
        return real_db(path or db_path)

    with mock.patch.object(core, "db", side_effect=_db), \
         mock.patch.object(inbox, "INBOX_PATH", os.path.join(tmp.name, "inbox.jsonl")), \
         mock.patch.object(core, "HYPO_DIR", os.path.join(tmp.name, "hypo")), \
         mock.patch.object(crew.tg, "send", return_value={"ok": True}), \
         mock.patch.object(tg, "send", return_value={"ok": True}), \
         mock.patch.object(tg, "throttled_progress", return_value=None):
        os.makedirs(os.path.join(tmp.name, "hypo"), exist_ok=True)
        conn = core.db(db_path)

        for i, (text, signals, hours, early, weak) in enumerate(IDEAS[:30], 1):
            # Фаза 4: идея → inbox (dup-check внутри)
            r = ideas.submit(f"если {text}, это видно по ранним метрикам", source="telegram")
            zones["stazhor"] += 1
            if r.get("duplicate"):
                log.append((i, "дубль на входе", text[:40])); continue
            log.append((i, "inbox", r["inbox_id"]))
            # triage: PI/PPI, корзина — или честный отказ (factors = данные,
            # которые модель приносит из фазы 1: сигналы, novelty, деньги)
            t = ideas.triage(r["inbox_id"], factors={
                "signals": signals, "novelty": 0.7, "early_pct": early,
                "standard": 0.5, "money": 0.6 if not weak else 0.3,
                "decidability": 0.8, "est_hours": hours})
            zones["krot"] += 1; zones["skif"] += 1
            if t.get("verdict") != "queued":
                log.append((i, "отклонён на разборе", str(t.get("reason"))[:40]))
                zones["hronik"] += 1
                continue
            hid = t.get("hid")
            log.append((i, "в очереди", f"{hid} PPI {t.get('ppi')}"))
            # Фаза 2/3: карточка + kill-стадия
            row = conn.execute("SELECT card_path FROM hypotheses WHERE id=?",
                               (hid,)).fetchone()
            if row and row["card_path"] and os.path.exists(row["card_path"]):
                fill_card(row["card_path"], rng)
                # прогноз фиксируется в базе ДО запуска (единственный запрет
                # контура: не править после факта)
                q.update_fields(conn, hid, forecast=12, forecast_low=8,
                                forecast_high=16)
                zones["morg"] += 1
                if weak and rng.random() < 0.75:
                    # слабая — снимается ДО GPU (успех контура по скиллу)
                    q.set_status(conn, hid, "killed")
                    q.update_fields(conn, hid, notes="killed: контроль не выдержан")
                    core.log_event(conn, "hypo.killed", hid,
                                   reason="контрольное условие не выдержано",
                                   lesson="проверять контроль до карточки")
                    crew.safe_emit("kill", conn=conn, ctx={"hid": hid})
                    log.append((i, "снят до GPU", hid)); zones["morg"] += 1
                    continue
                gate = hypo.check(hid, conn)
                if not gate.get("ok"):
                    log.append((i, "гейт не пройден", f"{hid}: {gate['problems'][:1]}"))
                    continue
            # прогон: governor lease → run → finish (dry-run контур)
            lease = governor.acquire_experiment(conn, hid, "L0", config=config)
            if lease.get("ok"):
                conn.execute(
                    "INSERT INTO runs (hypo_id, level, state, started_at, dry_run,"
                    " pid, log_path) VALUES (?,?,?,?,?,?,?)",
                    (hid, "L0", "running", core.iso(), 0, 0, "sim"))
                conn.commit()
                dispatch.finish(conn, hid, round(hours * 0.3, 2), "done", config)
                zones["gayka"] += 1
                log.append((i, "прогон dry-run", f"{hid} checkpoint"))
                # Фаза 5: вердикт → analyze закрыт
                kind = KINDS[i % 3] if not weak else "rejected"
                actual = {"confirmed": 11, "partial": 17, "rejected": 3}[kind]
                vr = v.record(conn, hid, kind, actual=actual, seeds_pass=2,
                              seeds_total=3, sigma=3.0, gpu_hours=round(hours * .3, 2),
                              changes="секция теста дополнена контролем", config=config)
                if vr.get("ok") is not False:
                    zones["hronik"] += 1; zones["shef"] += 1
                    verds.append(kind)
                    log.append((i, "вердикт", f"{hid} {kind}"))
        gov_phases.append(governor._effective_mode(conn, config))

        # сверка «как планировалось»: NEXT = лучший PPI, фазы, калибровка
        nxt = q.pick_next(conn, config)
        stats = conn.execute(
            "SELECT status, COUNT(*) c FROM hypotheses GROUP BY status").fetchall()
        calib = conn.execute(
            "SELECT COUNT(*) c, AVG(CASE WHEN kind='confirmed' THEN 1.0 ELSE 0 END) wr"
            " FROM verdicts").fetchone()
        chat = conn.execute("SELECT COUNT(*) c FROM crew_chat").fetchone()
        balance = dict(conn.execute(
            "SELECT agent, COUNT(*) c FROM crew_chat GROUP BY agent").fetchall())
        report_txt = conn.execute(
            "SELECT COUNT(*) c FROM crew_chat WHERE event='banter'").fetchone()

    out = {
        "iterations": 30,
        "log": log,
        "zones": zones,
        "statuses": {r["status"]: r["c"] for r in stats},
        "verdicts": {k: verds.count(k) for k in KINDS},
        "next_after_sim": None if nxt is None else {"id": nxt["id"], "ppi": nxt.get("ppi")},
        "chat_lines": chat["c"], "chat_balance": balance,
        "banter_lines": report_txt["c"],
        "win_rate": None if not calib["c"] else round(calib["wr"], 3),
    }
    if as_json:
        core.emit(out, True)
    else:
        print("=== 30 итераций: путь каждой идеи по инструкциям ===")
        for i, ph, art in log:
            print(f"  {i:>2}. {ph:<20} {art}")
        print("\nстатусы гипотез:", out["statuses"])
        print("вердикты:", out["verdicts"], "| win-rate:", out["win_rate"])
        print("NEXT после цикла:", out["next_after_sim"])
        print("реплик чата:", out["chat_lines"], "| баланс:",
              {crew.AGENTS[a]["name"]: c for a, c in sorted(balance.items())})
        print("governor-фазы по итерациям:", " ".join(gov_phases) or "—")
        print("артефакты по зонам:", {crew.AGENTS[a]["name"]: n for a, n in zones.items()})
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
