#!/usr/bin/env python3
"""researchagen — Telegram Mini App: пульт автономной ИИ-лаборатории.

Что это:
  * статика Mini App (index.html / app.css / js/*);
  * ``/api/state``  — единая картина: GPU, governor, очередь, прогоны,
    экипаж, вердикты, статистика;
  * ``/api/action`` — воздействия человека: пауза, подтверждение дорогого
    прогона, снятие задачи, приоритет, ручная проверка, подача идеи, голос
    в споре экипажа;
  * режим **live** читает живой профиль через штатные CLI инструментов
    (``tools/queue.py list --json`` и др.). Если живых данных нет/они пусты —
    сервер честно уходит в **demo**: детерминированная по seed симуляция
    лаборатории, чтобы интерфейс можно было оценивать целиком.

Философия профиля сохранена: только Python stdlib, ноль внешних пакетов,
SQLite — один источник правды (в demo — её точная имитация в памяти).

Запуск:
  python miniapp/server.py --port 8787           # авто: live при наличии state/
  python miniapp/server.py --port 8787 --demo    # принудительная симуляция
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ константы контура
# Сверено с config.yaml → researchagen.* (веса PI пересчитывает calib.py)
PI_WEIGHTS = {"signals": 0.22, "novelty": 0.16, "early": 0.12,
              "standard": 0.14, "money": 0.14, "decidability": 0.22}
AGING_PER_DAY, AGING_CAP = 0.05, 0.30
SIGNAL_SCALE = {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.50, 4: 0.67, 5: 0.84}
BINS = (("P1", 4.0), ("P2", 12.0), ("P3", 48.0))          # P4 — всё, что дороже
APPROVAL_HOURS = 12          # researchagen.limits.approval_gpu_hours
DAILY_HOURS = 20             # researchagen.limits.daily_gpu_hours_budget
DAILY_TASKS = 12             # researchagen.daily_research_task_budget
PREEMPT_RATIO = 2.0          # researchagen.limits.preempt_ratio

CREW = [
    {"id": "shef",    "name": "Boss",     "zone": "начальник: план, бюджет, арбитраж, приёмка",        "short": "начальник"},
    {"id": "skif",    "name": "Скиф",     "zone": "добыча: широкий проход по источникам, дедупликация", "short": "добыча"},
    {"id": "krot",    "name": "Аналитег", "zone": "добыча: синтез сигналов, оценка силы",              "short": "синтез"},
    {"id": "morg",    "name": "Морг",     "zone": "kill-stage: проверки и контраргументы",             "short": "kill-stage"},
    {"id": "gayka",   "name": "Гайка",    "zone": "эксперименты L0–L3: скрипты, seeds, чекпойнты",     "short": "эксперименты"},
    {"id": "hronik",  "name": "Хроник",   "zone": "память: калибровка, архив, патенты, коммерция",    "short": "память"},
    {"id": "stazhor", "name": "iВасёк",   "zone": "inbox, карточки, зачистка замечаний",               "short": "inbox"},
]
AGENT_NAME = {a["id"]: a["name"] for a in CREW}

KILL_CHECKS = [
    "Простое объяснение (lr / init / batch / метрика) не объясняет эффект",
    "Публикационный gap: прямого аналога нет",
    "Утечка данных / перекрытие train-test исключены",
    "Эффект не сводится к шуму seeds",
    "Есть контрольное условие, где эффект обязан исчезнуть",
    "Метрика читаема дёшево, без полного обучения",
    "PASS/FAIL сформулированы числами до запуска",
    "Назван покупатель или сценарий экономии",
]

BANNED_WORDS = ("перспективно", "многообещающе", "возможно улучшение",
                "выглядит интересно", "promising")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def bin_of(hours: float) -> str:
    for name, bound in BINS:
        if hours <= bound:
            return name
    return "P4"


def compute_pi(signals: int, novelty: float, early_pct: float, standard: float,
               money: float, decidability: float, age_days: float = 0.0) -> dict:
    """Формула из tools/queue.py — та же арифметика, что решает очередь."""
    s = SIGNAL_SCALE.get(signals, 1.0 if signals >= 6 else 0.0)
    n = max(0.0, min(1.0, novelty))
    e = max(0.0, min(1.0, 1.0 - (max(0.0, early_pct) - 1.0) / 9.0)) if early_pct >= 1 else 1.0
    q, m, d = (max(0.0, min(1.0, v)) for v in (standard, money, decidability))
    parts = {"S": PI_WEIGHTS["signals"] * s, "N": PI_WEIGHTS["novelty"] * n,
             "E": PI_WEIGHTS["early"] * e, "Q": PI_WEIGHTS["standard"] * q,
             "M": PI_WEIGHTS["money"] * m, "D": PI_WEIGHTS["decidability"] * d}
    aging = min(AGING_CAP, AGING_PER_DAY * max(0.0, age_days))
    pi = sum(parts.values()) + aging
    return {"pi": round(pi, 3), "parts": {k: round(v, 3) for k, v in parts.items()},
            "aging": round(aging, 3)}


# ------------------------------------------------------------------ демо-лаборатория
class DemoLab:
    """Детерминированная по seed симуляция контура: GPU, диспетчер, экипаж.

    Продвигается по wall-clock при каждом запросе — живая телеметрия без
    фоновых потоков. Никакого состояния, которого нет в настоящем профиле.
    """

    def __init__(self):
        self.rnd = random.Random(77)
        self.t0 = time.time()
        self.t_last = self.t0
        self.next_hid = 51
        self.msg_seq = 0
        self.run_seq = 118
        self.mode = "testing"
        self.autostart = True
        self.gpu = {"name": "NVIDIA GeForce RTX 5090", "total_gb": 31.9,
                    "model_gb": 20.4, "used_gb": 27.1, "util": 96, "temp": 69,
                    "critical_free": 2, "low_free": 6}
        self.budget = {"hours_used": 9.4, "tasks_used": 7}
        self.util_smooth, self.temp_smooth = 96.0, 69.0

        self.queue = self._seed_queue()
        self.runs, self.history = [], []
        self.chat: list[dict] = []
        self.disputes = {}
        self.remarks = self._seed_remarks()
        self.verdicts = self._seed_verdicts()
        self.closed_ideas = self._seed_closed_ideas()
        self.approvals = []
        self.user_votes = {}
        self._pc = []          # отложенные развязки ручных kill-проверок
        self._seed_chat()
        self._seed_run()
        self._seed_approvals()
        # отложенная развязка проверки H-048, которая «уже идёт» на старте демо
        self._pc.append((self.t0 + 15, "H-048", 1))

        # план реплик: (относительное время с последней, агент, текст)
        self.chat_plan = self._build_chat_plan()
        self.chat_timer = 12.0
        self.next_start_delay = 0.0

    # ------------------------------------------------------------ демо-данные
    def _hyp(self, hid, title, **kw):
        h = {"id": hid, "title": title, "status": "queued", "level": kw.get("done_level", "—"),
             "bin": bin_of(kw["hours"]), "est_hours": kw["hours"], "signals": kw.get("signals", 4),
             "novelty": kw.get("novelty", 0.8), "early_pct": kw.get("early_pct", 2),
             "standard": kw.get("standard", 0.6), "money": kw.get("money", 0.5),
             "decidability": kw.get("decidability", 0.8), "source": kw.get("source", "skif"),
             "created": iso(self.t0 - kw.get("age_days", 0.5) * 86400),
             "age_days": kw.get("age_days", 0.5),
             "forecast": kw["forecast"], "forecast_low": kw.get("fl", -10.0),
             "forecast_high": kw.get("fh", -6.0), "unit": kw.get("unit", "% val loss"),
             "checks": [{"i": i, "s": "pass"} for i in range(8)],
             "bets": kw.get("bets", {"up": [], "down": []}), "seeds": kw.get("seeds", 3),
             "approved": True, "note": kw.get("note", "")}
        for i, st in kw.get("checks_state", {}).items():
            h["checks"][i]["s"] = st
        h["checks_pass"] = sum(1 for c in h["checks"] if c["s"] == "pass")
        sc = compute_pi(h["signals"], h["novelty"], h["early_pct"], h["standard"],
                        h["money"], h["decidability"], h["age_days"])
        h["pi"], h["ppi"] = sc["pi"], round(sc["pi"] / max(0.25, h["est_hours"]), 2)
        return h

    def _seed_queue(self):
        hs = [
            self._hyp("H-041", "Sign-stability к 2% обучения как триггер ранней остановки",
                      hours=2.0, signals=5, novelty=0.9, early_pct=2, standard=0.7,
                      money=0.6, decidability=0.9, forecast="−18% val loss к 10% обучения",
                      fl=-22.0, fh=-14.0, source="skif", age_days=0.3,
                      checks_state={7: "run"}),
            self._hyp("H-042", "Gradient disparity отделяет grokking от запоминания до перехода",
                      hours=3.5, signals=4, novelty=0.85, early_pct=5, standard=0.5,
                      money=0.4, decidability=0.7, forecast="−12% шага перехода",
                      fl=-16.0, fh=-8.0, source="krot", age_days=0.8,
                      bets={"up": ["skif", "gayka"], "down": ["morg"]},
                      checks_state={4: "fail"},
                      note="kill-check 5: контрольного условия нет — Морг заблокировал"),
            self._hyp("H-043", "Effective rank весов > 0.6·max к 5% хода предсказывает обобщение",
                      hours=1.5, signals=4, novelty=0.7, early_pct=5, standard=0.8,
                      money=0.5, decidability=0.85, forecast="+0.03 avg точность",
                      fl=0.02, fh=0.05, source="krot", age_days=1.1),
            self._hyp("H-044", "Iterative magnitude pruning × early-bird: rewind с 3% хода",
                      hours=13.5, signals=4, novelty=0.75, early_pct=3, standard=0.6,
                      money=0.8, decidability=0.7, forecast="−25% compute при том же качестве",
                      fl=-30.0, fh=-20.0, source="hronik", age_days=0.2,
                      note="L2 13.5 GPU-ч > порога 12 ч — ждёт подтверждения человека",
                      approved=False),
            self._hyp("H-045", "Layerwise freezing по sharpness: заморозка ранних слоёв",
                      hours=6.0, signals=3, novelty=0.8, early_pct=8, standard=0.4,
                      money=0.5, decidability=0.6, forecast="−15% времени эпохи",
                      fl=-18.0, fh=-12.0, source="skif", age_days=1.4),
            self._hyp("H-047", "Low-rank extraction: SVD-проекция контура после condensation",
                      hours=2.5, signals=3, novelty=0.6, early_pct=4, standard=0.3,
                      money=0.7, decidability=0.9, forecast="−20% параметров при −1% качества",
                      fl=-25.0, fh=-15.0, source="stazhor", age_days=0.6,
                      checks_state={5: "fail"},
                      note="kill-check 6: метрика требует полного обучения"),
            self._hyp("H-048", "Neural collapse на мини-пакетах как дешёвый прокс контура",
                      hours=1.0, signals=3, novelty=0.55, early_pct=6, standard=0.5,
                      money=0.3, decidability=0.7, forecast="+0.02 avg точность",
                      fl=0.01, fh=0.03, source="human", age_days=0.1,
                      bets={"up": ["hronik"], "down": ["krot"]},
                      checks_state={1: "run", 6: "wait"}),
            self._hyp("H-049", "Spectral bias как объяснение провалов L2 у ранних сигналов",
                      hours=52.0, signals=4, novelty=0.5, early_pct=10, standard=0.2,
                      money=0.2, decidability=0.4, forecast="карта, не эффект: ±0%",
                      fl=-3.0, fh=3.0, source="skif", age_days=6.2,
                      note="стареет в очереди: P4, эффект размазан"),
        ]
        by = {h["id"]: h for h in hs}
        running = by["H-041"]; running["status"] = "running"; running["level"] = "L1"
        by["H-042"]["status"] = "blocked"
        by["H-047"]["status"] = "blocked"
        return hs

    def _seed_remarks(self):
        return [
            {"id": "RM-4", "from": "morg", "to": "gayka", "hid": "H-039",
             "text": "kill-check без доказательства: галочка стоит, ссылки на прогон нет",
             "status": "open", "ts": iso(self.t0 - 3800)},
            {"id": "RM-3", "from": "skif", "to": "stazhor", "hid": None,
             "text": "фильтр дублей работает по старому кэшу — A и C из одной статьи прошли как независимые",
             "status": "closed", "ts": iso(self.t0 - 26000)},
            {"id": "RM-2", "from": "krot", "to": "skif", "hid": "H-042",
             "text": "прогноз не зафиксирован до запуска L0 — верни карточку",
             "status": "closed", "ts": iso(self.t0 - 45000)},
            {"id": "RM-1", "from": "hronik", "to": "skif", "hid": "H-049",
             "text": "гипотеза гниёт в очереди 6 дней — обнови aging или сними с очереди",
             "status": "open", "ts": iso(self.t0 - 90000)},
        ]

    def _seed_verdicts(self):
        v = [
            {"id": "V-33", "hid": "H-036", "kind": "confirmed",
             "title": "Weight-norm ranking коррелирует с устойчивостью к pruning",
             "checked": "Ранг нормы весов на 5% обучения против чувствительности к имп-прунингу (CIFAR-10, ResNet-20/Tiny-Transformer)",
             "forecast": -12.0, "actual": -11.4, "unit": "% val loss",
             "seeds_pass": 3, "seeds_total": 3, "sigma": 2.8, "gpu_hours": 0.8,
             "changes": "L2 разрешён; добавлена абляция по знакам градиента",
             "next": "Проверить на WideResNet и 5 seeds", "commercial": 0.7,
             "patent": {"ready": True, "title": "Способ ранней оценки устойчивости нейросети к прореживанию по ранжиру норм весов",
                        "claims": 3, "status": "черновик заявки"},
             "ts": iso(self.t0 - 86400 * 1.2)},
            {"id": "V-32", "hid": "H-035", "kind": "partial",
             "title": "Loss curvature сжимается до перехода grokking",
             "checked": "Sharpness (σ_max Hessian proxy) до и после перехода на Modular Arithmetic",
             "forecast": -25.0, "actual": -17.0, "unit": "% sharpness",
             "seeds_pass": 2, "seeds_total": 3, "sigma": 4.1, "gpu_hours": 2.3,
             "changes": "Гипотеза сужена: эффект только при weight decay > 1e-2",
             "next": "Переформулировать в H-045 (freezing по sharpness)", "commercial": 0.3,
             "patent": None, "ts": iso(self.t0 - 86400 * 2.4)},
            {"id": "V-31", "hid": "H-037", "kind": "rejected",
             "title": "Condensation предшествует grokking на всех масштабах",
             "checked": "Время конденсации против времени перехода, 3 seeds × 2 архитектуры",
             "forecast": -25.0, "actual": -2.0, "unit": "% шага перехода",
             "seeds_pass": 1, "seeds_total": 3, "sigma": 6.0, "gpu_hours": 4.1,
             "changes": "Сигнал не пережил масштаб: только Tiny-Transformer",
             "next": "Идея закрыта, дубли поставлены в лог неэффективных", "commercial": 0.1,
             "patent": None, "ts": iso(self.t0 - 86400 * 3.1)},
            {"id": "V-30", "hid": "H-034", "kind": "killed",
             "title": "Information bottleneck phase transition измерим дёшево",
             "checked": "Снята до эксперимента: kill-check 6 — метрика требует полного обучения",
             "forecast": None, "actual": None, "unit": "",
             "seeds_pass": 0, "seeds_total": 0, "sigma": None, "gpu_hours": 0.1,
             "changes": "Убита за 8 минут — бюджет сохранён",
             "next": "Ждать дешёвого прокс-измерителя MI", "commercial": 0.0,
             "patent": None, "ts": iso(self.t0 - 86400 * 4.0)},
            {"id": "V-29", "hid": "H-033", "kind": "confirmed",
             "title": "Iterative pruning + rewinding на 3% хода сохраняет билет",
             "checked": "IMP с rewind на 3% против 20% базового: итоговое качество при равном бюджете",
             "forecast": -20.0, "actual": -23.0, "unit": "% compute",
             "seeds_pass": 3, "seeds_total": 3, "sigma": 1.9, "gpu_hours": 6.2,
             "changes": "Метод закреплён; считается кандидатурой на патент",
             "next": "Расширить на 5 seeds + WideResNet (L3)", "commercial": 0.8,
             "patent": {"ready": True, "title": "Способ выделения выигравшего билета нейросети по фрагменту начального обучения",
                        "claims": 4, "status": "проверка новизны"},
             "ts": iso(self.t0 - 86400 * 5.7)},
            {"id": "V-28", "hid": "H-031", "kind": "rejected",
             "title": "Критический период: маска lr-прогрева заменяет расписание",
             "checked": "Фиксация lr на прогреве против cosine, 3 seeds",
             "forecast": -10.0, "actual": 3.0, "unit": "% val loss",
             "seeds_pass": 0, "seeds_total": 3, "sigma": 3.3, "gpu_hours": 3.0,
             "changes": "Эффект обратный прогнозу — гипотеза закрыта",
             "next": "Найти причину: возможно, warmup слишком короткий", "commercial": 0.1,
             "patent": None, "ts": iso(self.t0 - 86400 * 6.9)},
        ]
        for x in v:
            f, a = x["forecast"], x["actual"]
            x["deviation"] = round((a - f) / abs(f) * 100.0, 1) if (f not in (None, "") and a is not None) else None
        return v

    def _seed_closed_ideas(self):
        return [
            {"id": "H-037", "title": "Condensation предшествует grokking на всех масштабах", "why": "опровергнута: не пережила масштаб"},
            {"id": "H-034", "title": "Information bottleneck phase transition измерим дёшево", "why": "снята: метрика дорогая"},
            {"id": "H-031", "title": "Маска lr-прогрева заменяет расписание (критический период)", "why": "опровергнута: эффект обратный"},
            {"id": "H-029", "title": "Ранняя конденсация градиентов как признак обобщения", "why": "дубль сигнала condensation"},
            {"id": "H-027", "title": "Раннее прореживание по величине весов без потери качества", "why": "слишком близко к early-bird ticket (нет gap)"},
            {"id": "H-024", "title": "Спектральный сдвиг Hessian как ранний стоп", "why": "дубль sharpness-семейства"},
        ]

    def _msg(self, agent, text, kind="work", hid=None, ts=None, dispute=None):
        self.msg_seq += 1
        return {"id": f"M{self.msg_seq}", "agent": agent, "text": text, "kind": kind,
                "hid": hid, "ts": iso(ts if ts is not None else time.time()),
                "dispute": dispute}

    def _dispute(self, q, options, closed=False, boss=None):
        self.msg_seq += 1
        return {"id": f"D{self.msg_seq}", "q": q,
                "options": [{"id": o[0], "label": o[1], "votes": o[2]} for o in options],
                "closed": closed, "boss": boss}

    def _seed_chat(self):
        t = self.t0
        self.chat = [
            self._msg("boss", "смена: 7 из 12 задач дня, 9.4 из 20 GPU-ч. после H-041 остаётся на один L2 — второй попрошу у человека.", ts=t - 7200),
            self._msg("gayka", "H-041, L1, сид 1/3. loss ниже базовой кривой на 3.7%. чекпойнт каждые 10 минут.", hid="H-041", ts=t - 5400),
            self._msg("morg", "ниже базовой — это ещё не эффект. где разброс по сидам?", hid="H-041", ts=t - 5100),
            self._msg("gayka", "±0.4% по сидам, сею три. к вечеру закрою L1.", hid="H-041", ts=t - 4800),
            self._msg("krot", "синтезировал 4 сигнала по gradient disparity. B и C подозрительно похожи — считаю их зависимыми.", hid="H-042", ts=t - 3600),
            self._msg("skif", "независимы. разные группы, разные кодовые базы, разные метрики. спорь дальше.", ts=t - 3400),
        ]
        d1 = self._dispute("Считать ли сигналы B и C независимыми для H-042?",
                           [("a", "Независимые (Скиф)", 2), ("b", "Зависимые (Аналитег)", 1)])
        self.disputes[d1["id"]] = d1
        self.chat.append(self._msg("boss", "спор открыт. голосуйте — человек тоже, вес ×2.", "dispute", "H-042", ts=t - 3300, dispute=d1))
        self.chat += [
            self._msg("hronik", "если H-041 подтвердится — патент на метод ранней остановки. рынок: все, кто тренит свои модели.", hid="H-041", ts=t - 2400),
            self._msg("krot", "а рынок готов платить? или опять «технологии будущего»?", ts=t - 2300),
            self._msg("stazhor", "завёл карточку H-048 от заказчика. правит орфографию в карточках, молодец.", hid="H-048", ts=t - 1500),
            self._msg("morg", "H-047, kill-check 6: метрика требует полного обучения. это не дешёвый признак, это перепись населения.", hid="H-047", ts=t - 1200),
            self._msg("skif", "iВасёк, у тебя фильтр дублей по старому кэшу работает. перезапусти.", ts=t - 900),
            self._msg("stazhor", "ке-ек. починил.", ts=t - 880),
        ]

    def _build_chat_plan(self):
        d1 = self._dispute("Считать ли сигналы B и C независимыми для H-042?",
                           [("a", "Независимые (Скиф)", 2), ("b", "Зависимые (Аналитег)", 1)])
        self.disputes[d1["id"]] = d1
        plan = [
            ("shef", "H-042: арбитраж числом — у Скифа аргумент про кодовые базы. считаем 4 сигнала, но контраргументы Морга остаются в силе.", "work", "H-042"),
            ("gayka", "H-041: сид 2/3 пошёл. стабильность знаков выходит на плато 0.78 — как в прогнозе.", "work", "H-041"),
            ("stazhor", "замечание RM-4 закрыл: ссылка на прогон добавлена в карточку H-039.", "review", None),
            ("krot", "по H-043 ранг растёт быстрее у обобщающих конфигураций. сигнал слабее, чем у sign-stability, но дешевый.", "work", "H-043"),
            ("morg", "слабый сигнал + дешёвая метрика = L0 и только. не позволю ещё один L2 вслепую.", "work", "H-043"),
            ("hronik", "черновик заявки по H-036 готов: 3 пункта формулы. заказчик сможет лицензировать метод.", "work", "H-036"),
            ("shef", "H-044 ждёт человека: 13.5 GPU-ч. напоминаю, а не решаю за него.", "work", "H-044"),
            ("gayka", "лол, а я чекпойнты на всякий случай снимаю.", "work", None),
        ]
        return plan

    def _seed_run(self):
        self.run_seq += 1
        base, run = [], []
        steps, dt0 = 95, 0.052
        for k in range(1, 42):
            x = k * 1000
            base.append([x, 0.55 * math.exp(-x / 30000) + 0.115 + 0.004 * math.sin(x / 4000)])
            run.append([x, 0.52 * math.exp(-x / 26000) + 0.096 + 0.003 * math.sin(x / 4200)])
        rank = [[k * 1000, 12 + 26 * (1 - math.exp(-k / 16))] for k in range(1, 42)]
        stab = []
        for s in range(3):
            stab.append([[k * 1000, max(0.05, 0.82 - 0.09 * s - 0.75 * math.exp(-k / 9.5) + 0.02 * math.sin(k / 3 + s))] for k in range(1, 42)])
        self.runs = [{
            "id": f"R-{self.run_seq}", "hid": "H-041",
            "title": "Sign-stability к 2% обучения как триггер ранней остановки",
            "level": "L1", "status": "running", "seed": 1, "seeds_total": 3,
            "steps_total": 95000, "steps_done": 41000, "progress": 0.43,
            "started_ts": iso(time.time() - 51 * 60), "elapsed_min": 51.0, "eta_min": 67.0,
            "series": {"loss_base": base, "loss_run": run, "rank": rank, "stab": stab},
        }]
        self.history = [
            {"id": "R-116", "hid": "H-041", "title": "Sign-stability, L0", "level": "L0",
             "final": 0.31, "base": 0.35, "minutes": 6, "kind": "done", "delta": -11.4},
            {"id": "R-112", "hid": "H-042", "title": "Gradient disparity, L0", "level": "L0",
             "final": 0.42, "base": 0.44, "minutes": 5, "kind": "done", "delta": -4.5},
            {"id": "R-108", "hid": "H-036", "title": "Weight-norm ranking, L1", "level": "L1",
             "final": 0.38, "base": 0.43, "minutes": 74, "kind": "verdict", "delta": -11.4},
        ]
        # компактные кривые для сравнения прогонов (детерминированные)
        for idx, item in enumerate(self.history):
            steps = 12000 if item["level"] == "L0" else 95000
            item["series"] = [
                [int(steps * i / 35),
                 round(item["base"] + (0.62 - item["base"]) * (1 - i / 35) ** 1.7
                       + math.sin(i / 4 + idx) * 0.004, 4)]
                for i in range(36)]
            item["series_run"] = [
                [int(steps * i / 35),
                 round(item["final"] + (0.60 - item["final"]) * (1 - i / 35) ** 1.5
                       + math.sin(i / 3 + idx) * 0.003, 4)]
                for i in range(36)]

    def _seed_approvals(self):
        self.approvals = [{
            "id": "APR-1", "hid": "H-044",
            "title": "Iterative magnitude pruning × early-bird: rewind с 3% хода",
            "level": "L2", "hours": 13.5, "ppi": 0.52, "bin": "P3",
            "note": "13.5 GPU-ч > порога 12 ч", "ts": iso(self.t0 - 600),
        }]

    # ------------------------------------------------------------ симуляция
    def advance(self):
        now = time.time()
        dt = max(0.0, now - self.t_last)
        self.t_last = now
        run = next((r for r in self.runs if r["status"] == "running"), None)
        active = run is not None and self.autostart

        # GPU: util/temp сглаженно следуют за активностью
        target_util = 93 + self.rnd.random() * 6 if active else (3 + self.rnd.random() * 4)
        target_temp = 71 if active else 41
        self.util_smooth += (target_util - self.util_smooth) * min(1.0, dt / 4)
        self.temp_smooth += (target_temp - self.temp_smooth) * min(1.0, dt / 30)
        self.gpu["util"], self.gpu["temp"] = round(self.util_smooth), round(self.temp_smooth)
        used = self.gpu["model_gb"] + (6.7 if active else 0.0) + (self.rnd.random() - 0.5) * 0.2
        self.gpu["used_gb"] = round(min(self.gpu["total_gb"], used), 1)

        if active and run:
            # телеметрия: точка каждые 2 с, не длиннее 240 точек
            run["elapsed_min"] += dt / 60
            speed = run["steps_total"] / (run["eta_min"] + run["elapsed_min"]) / 60.0
            run["steps_done"] = min(run["steps_total"], run["steps_done"] + speed * dt)
            run["progress"] = run["steps_done"] / run["steps_total"]
            run["eta_min"] = max(0.0, run["eta_min"] - dt / 60)
            self.budget["hours_used"] += dt / 3600
            n_pts = int(dt / 2)
            for _ in range(max(0, n_pts)):
                x = int(run["steps_done"])
                k = x / 1000
                lb = 0.55 * math.exp(-x / 30000) + 0.115 + 0.004 * math.sin(x / 4000)
                lr_ = 0.52 * math.exp(-x / 26000) + 0.096 + 0.003 * math.sin(x / 4200)
                run["series"]["loss_base"].append([x, lb])
                run["series"]["loss_run"].append([x, lr_ + (self.rnd.random() - 0.5) * 0.003])
                run["series"]["rank"].append([x, 12 + 26 * (1 - math.exp(-k / 16)) + (self.rnd.random() - 0.5) * 0.4])
                s = run["seed"] - 1
                run["series"]["stab"][s].append([x, max(0.05, 0.82 - 0.09 * s - 0.75 * math.exp(-k / 9.5) + 0.02 * math.sin(k / 3 + s)) + (self.rnd.random() - 0.5) * 0.01])
                for key in ("loss_base", "loss_run", "rank"):
                    if len(run["series"][key]) > 240:
                        run["series"][key].pop(0)
                for srow in run["series"]["stab"]:
                    if len(srow) > 240:
                        srow.pop(0)
            if run["progress"] >= 1.0:
                self._finish_run(run)

        # авто-старт следующей задачи
        if self.autostart and not any(r["status"] == "running" for r in self.runs):
            self.next_start_delay -= dt
            if self.next_start_delay <= 0:
                self._start_next()

        # чат: живая лента
        self.chat_timer -= dt
        if self.chat_timer <= 0 and self.chat_plan:
            agent, text, kind, hid = self.chat_plan.pop(0)
            self.chat.append(self._msg(agent, text, kind, hid))
            self.chat_timer = 20 + self.rnd.random() * 18
        self.chat = self.chat[-48:]

    def _finish_run(self, run):
        run["status"] = "done"
        run["eta_min"] = 0.0
        hid = run["hid"]
        h = next((x for x in self.queue if x["id"] == hid), None)
        self.history.insert(0, {"id": run["id"], "hid": hid,
                                "title": (h or {}).get("title", hid), "level": run["level"],
                                "final": 0.31, "base": 0.35, "minutes": round(run["elapsed_min"]),
                                "kind": "done", "delta": -11.4})
        self.chat.append(self._msg("gayka", f"{hid}: {run['level']} закрыт за {round(run['elapsed_min'])} мин. числа в карточке, чекпойнт снят.", "work", hid))
        if h:
            if run["level"] == "L1":
                h["status"], h["level"] = "queued", "L2"
                self.chat.append(self._msg("morg", f"{hid}: L1 — PASS по фиксированным критериям. L2 разрешаю, если часы уложатся в 12.", "work", hid))
            else:
                h["level"] = "L1"
        self.runs = [r for r in self.runs if r["status"] == "running"]
        self.next_start_delay = 25.0
        self.budget["tasks_used"] = min(DAILY_TASKS, self.budget["tasks_used"] + 1)

    def _start_next(self):
        if any(r["status"] in ("running", "paused") for r in self.runs):
            return          # GPU — один ресурс: пауза держит блокировку
        ready = [h for h in self.queue
                 if h["status"] == "queued" and h["approved"]
                 and h["est_hours"] + self.budget["hours_used"] <= DAILY_HOURS]
        if not ready:
            self.mode = "discover"
            return
        ready.sort(key=lambda h: (-h["ppi"], -h["pi"]))
        h = ready[0]
        h["status"] = "running"
        level = "L1" if h["level"] in ("—", "L0") else h["level"]
        h["level"] = level
        self.run_seq += 1
        self.mode = "testing"
        minutes = {"L0": 6, "L1": 70, "L2": 240, "L3": 600}.get(level, 60)
        base, run = [], []
        self.runs = [{
            "id": f"R-{self.run_seq}", "hid": h["id"], "title": h["title"], "level": level,
            "status": "running", "seed": 1, "seeds_total": h.get("seeds", 3),
            "steps_total": 95000, "steps_done": 0, "progress": 0.0,
            "started_ts": iso(time.time()), "elapsed_min": 0.0, "eta_min": float(minutes),
            "series": {"loss_base": base, "loss_run": run, "rank": [], "stab": [[] for _ in range(3)]},
        }]
        self.chat.append(self._msg("gayka", f"{h['id']}: взял на GPU, {level}, 3 seeds, критерии до запуска зафиксированы.", "work", h["id"]))

    # ------------------------------------------------------------ действия
    def act(self, body: dict) -> dict:
        self.advance()
        t = body.get("type")
        if t == "pause":
            self.autostart = False
            self.mode = "paused"
            run = next((r for r in self.runs if r["status"] == "running"), None)
            if run:
                run["status"] = "paused"
                h = next((x for x in self.queue if x["id"] == run["hid"]), None)
                if h:
                    h["status"] = "paused_checkpoint"
                self.chat.append(self._msg("shef", f"{run['hid']}: пауза по команде человека. чекпойнт снят, GPU освобождаем после flush.", "work", run["hid"]))
            else:
                self.chat.append(self._msg("shef", "автозапуск остановлен по команде человека. очередь заморожена.", "work", None))
            return {"ok": True, "autostart": False}
        if t == "resume":
            self.autostart = True
            self.mode = "testing"
            for r in self.runs:
                if r["status"] == "paused":
                    r["status"] = "running"
                    h = next((x for x in self.queue if x["id"] == r["hid"]), None)
                    if h:
                        h["status"] = "running"
            self.chat.append(self._msg("shef", "автозапуск возвращён. продолжаем с чекпойнта.", "work", None))
            self._start_next()
            return {"ok": True, "autostart": True}
        if t == "kill_task":
            hid = body.get("hid")
            run = next((r for r in self.runs if r["hid"] == hid), None)
            if run:
                run["status"] = "killed"
                self.runs = [r for r in self.runs if r["status"] == "running"]
                h = next((x for x in self.queue if x["id"] == hid), None)
                if h:
                    h["status"] = "blocked"
                self.chat.append(self._msg("morg", f"{hid}: снята с GPU человеком. некролог: прогресс {round(run['progress']*100)}% заморожен в чекпойнте.", "necro", hid))
                self.next_start_delay = 15.0
                return {"ok": True}
            return {"ok": False, "err": "задача не найдена"}
        if t == "approve":
            aid = body.get("id")
            ok = bool(body.get("ok", False))
            ap = next((a for a in self.approvals if a["id"] == aid), None)
            if not ap:
                return {"ok": False, "err": "нет такой заявки"}
            self.approvals.remove(ap)
            h = next((x for x in self.queue if x["id"] == ap["hid"]), None)
            if ok and h:
                h["approved"] = True
                self.chat.append(self._msg("shef", f"{ap['hid']}: человек подтвердил {ap['level']} ({ap['hours']} GPU-ч). ставим в план.", "work", ap["hid"]))
            elif h:
                h["status"] = "killed"
                h["approved"] = False
                self.chat.append(self._msg("shef", f"{ap['hid']}: человек отклонил дорогой прогон. гипотеза закрыта до GPU.", "necro", ap["hid"]))
            return {"ok": True}
        if t == "boost":
            hid = body.get("hid")
            h = next((x for x in self.queue if x["id"] == hid), None)
            if h:
                h["age_days"] = h.get("age_days", 0) + 2.0
                sc = compute_pi(h["signals"], h["novelty"], h["early_pct"], h["standard"],
                                h["money"], h["decidability"], h["age_days"])
                h["pi"], h["ppi"] = sc["pi"], round(sc["pi"] / max(0.25, h["est_hours"]), 2)
                self.chat.append(self._msg("stazhor", f"{hid}: приоритет поднят человеком — aging +2 дня эквивалентом.", "work", hid))
                return {"ok": True, "pi": h["pi"], "ppi": h["ppi"]}
            return {"ok": False, "err": "нет такой гипотезы"}
        if t == "run_check":
            hid, i = body.get("hid"), int(body.get("i", -1))
            h = next((x for x in self.queue if x["id"] == hid), None)
            if h and 0 <= i < len(h["checks"]):
                h["checks"][i]["s"] = "run"
                h["checks_pass"] = sum(1 for c in h["checks"] if c["s"] == "pass")
                self.chat.append(self._msg("morg", f"{hid}: проверка {i+1} запущена вручную. контраргументы — в карточку.", "work", hid))
                # быстрая развязка через ~8 с (по следующему advance)
                self._pc.append((time.time() + 8, hid, i))
                return {"ok": True}
            return {"ok": False, "err": "проверка не найдена"}
        if t == "run_level":
            hid, level = body.get("hid"), body.get("level")
            h = next((x for x in self.queue if x["id"] == hid), None)
            if not h:
                return {"ok": False, "err": "нет такой гипотезы"}
            hours = {"L0": 0.1, "L1": 2.0, "L2": 13.5, "L3": 30.0}.get(level, 1.0)
            if hours > APPROVAL_HOURS and not h.get("approved"):
                self.approvals.append({"id": f"APR-{len(self.approvals)+1}", "hid": hid,
                                       "title": h["title"], "level": level, "hours": hours,
                                       "ppi": h["ppi"], "bin": h["bin"],
                                       "note": f"{hours} GPU-ч > порога {APPROVAL_HOURS} ч",
                                       "ts": iso(time.time())})
                return {"ok": True, "approval": True}
            h["status"], h["level"] = "queued", level
            if not any(r["status"] == "running" for r in self.runs):
                self._start_next()
            return {"ok": True, "approval": False}
        if t == "idea_check":
            return {"ok": True, **self._idea_check(body)}
        if t == "submit_idea":
            return {"ok": True, **self._submit_idea(body)}
        if t == "vote":
            did, opt = body.get("dispute_id"), body.get("option")
            d = self.disputes.get(did)
            if d and opt in {o["id"] for o in d["options"]}:
                if self.user_votes.get(did) is None:
                    for o in d["options"]:
                        if o["id"] == opt:
                            o["votes"] += 1
                    self.user_votes[did] = opt
                    self.chat.append(self._msg("shef", "человек проголосовал в споре. учту как вес 2, но решает база.", "work", None, dispute=None))
                return {"ok": True, "voted": opt}
            return {"ok": False, "err": "спор не найден"}
        return {"ok": False, "err": f"неизвестное действие: {t}"}

    def _resolve_checks(self):
        now = time.time()
        keep = []
        for due, hid, i in self._pc:
            if now >= due:
                h = next((x for x in self.queue if x["id"] == hid), None)
                if h:
                    fail = self.rnd.random() < 0.3
                    h["checks"][i]["s"] = "fail" if fail else "pass"
                    h["checks_pass"] = sum(1 for c in h["checks"] if c["s"] == "pass")
                    if fail:
                        self.chat.append(self._msg("morg", f"{hid}: проверка {i+1} — FAIL. контраргумент в карточке.", "review", hid))
            else:
                keep.append((due, hid, i))
        self._pc = keep

    # ------------------------------------------------------------ идея: дубликаты и симуляция PPI
    def _idea_similarity(self, text: str, title: str) -> float:
        stop = {"и", "в", "на", "с", "по", "как", "что", "это", "для", "при", "не", "до", "из", "за", "от"}
        words = {w for w in "".join(c if c.isalnum() else " " for c in (text or "").lower()).split()
                 if len(w) > 3 and w not in stop}
        tw = {w for w in "".join(c if c.isalnum() else " " for c in (title or "").lower()).split()
              if len(w) > 3 and w not in stop}
        if not words or not tw:
            return 0.0
        return round(len(words & tw) / max(8, min(len(words), len(tw)) + 4), 2)

    def _idea_check(self, body: dict) -> dict:
        text = body.get("text", "")
        matches = []
        for c in self.closed_ideas + [{"id": h["id"], "title": h["title"], "why": "в очереди"} for h in self.queue]:
            sim = self._idea_similarity(text, c["title"])
            if sim >= 0.18:
                matches.append({"id": c["id"], "title": c["title"], "why": c.get("why", ""), "sim": sim})
        matches.sort(key=lambda m: -m["sim"])
        quality = 0
        notes = []
        if len(text) > 60:
            quality += 25
        else:
            notes.append("слишком коротко: механизм не виден")
        if any(ch.isdigit() for ch in text):
            quality += 25
        else:
            notes.append("нет чисел: PASS/FAIL не сформулировать")
        mech_words = ("если", "то", "потому", "механизм", "вызывает", "предсказывает", "коррелирует", "приводит")
        if any(w in text.lower() for w in mech_words):
            quality += 25
        else:
            notes.append("нет причинной связки «если X, то Y»")
        bad = [w for w in BANNED_WORDS if w in text.lower()]
        if bad:
            notes.append(f"запрещённые слова: {', '.join(bad)} — вердикт такое не примет")
        else:
            quality += 25
        return {"matches": matches[:4], "quality": min(quality, 100), "notes": notes}

    def _submit_idea(self, body: dict) -> dict:
        text = (body.get("text") or "").strip()[:180]
        hours = max(0.25, min(60.0, float(body.get("hours") or 2.0)))
        early = max(0.5, min(50.0, float(body.get("early_pct") or 3.0)))
        signals = max(0, min(6, int(body.get("signals") or 3)))
        novelty = max(0.0, min(1.0, float(body.get("novelty") or 0.7)))
        standard = max(0.0, min(1.0, float(body.get("standard") or 0.5)))
        money = max(0.0, min(1.0, float(body.get("money") or 0.4)))
        decidability = max(0.0, min(1.0, float(body.get("decidability") or 0.7)))
        sc = compute_pi(signals, novelty, early, standard, money, decidability, 0.0)
        self.next_hid += 1
        hid = f"H-0{self.next_hid}" if self.next_hid < 100 else f"H-{self.next_hid}"
        h = self._hyp(hid, text[:80] or "Идея человека", hours=hours, signals=signals,
                      novelty=novelty, early_pct=early, standard=standard, money=money,
                      decidability=decidability, source="human", age_days=0.0,
                      forecast=body.get("forecast") or "прогноз зафиксирует экипаж до запуска",
                      fl=-12.0, fh=-6.0,
                      checks=[{"i": i, "s": "wait"} for i in range(8)],
                      bets={"up": [], "down": []})
        h["pi"], h["ppi"] = sc["pi"], round(sc["pi"] / max(0.25, hours), 2)
        h["note"] = "лид от человека: на разборе у экипажа"
        self.queue.append(h)
        queued = [x for x in self.queue if x["status"] == "queued"]
        queued.sort(key=lambda x: -x["ppi"])
        pos = 1 + next((idx for idx, x in enumerate(queued) if x["id"] == hid), len(queued))
        self.chat.append(self._msg("stazhor", f"{hid}: идея от человека принята в inbox. симулирую приоритет… карточку завёл.", "work", hid))
        self.chat.append(self._msg("morg", f"{hid}: до GPU — 8 kill-проверок. начну с «простого объяснения».", "work", hid))
        bet = self.rnd.sample([a["id"] for a in CREW], k=3)
        self.queue[-1]["bets"] = {"up": bet[:2], "down": bet[2:]}
        self.chat.append(self._msg("hronik", f"{hid}: ставлю «взлетит». ранние предикторы — деньги.", "bet", hid))
        return {"hid": hid, "pi": h["pi"], "ppi": h["ppi"], "position": pos,
                "of": len(queued), "parts": sc["parts"]}

    # ------------------------------------------------------------ состояние для UI
    def state(self) -> dict:
        self._resolve_checks()
        self.advance()
        run = next((r for r in self.runs if r["status"] in ("running", "paused")), None)
        cur = None
        if run:
            loss_now = run["series"]["loss_run"][-1][1] if run["series"]["loss_run"] else None
            base_now = run["series"]["loss_base"][-1][1] if run["series"]["loss_base"] else None
            cur = {"hid": run["hid"], "title": run["title"], "level": run["level"],
                   "progress": round(run["progress"], 3), "eta_min": round(run["eta_min"]),
                   "elapsed_min": round(run["elapsed_min"]), "steps": int(run["steps_done"]),
                   "steps_total": run["steps_total"], "seed": run["seed"],
                   "seeds_total": run["seeds_total"], "status": run["status"],
                   "loss_now": round(loss_now, 4) if loss_now else None,
                   "base_now": round(base_now, 4) if base_now else None}
        queue = sorted(self.queue, key=lambda h: (-h["ppi"] if h["status"] in ("queued", "blocked") else -99))
        open_bets = sum(1 for h in self.queue if h["bets"]["up"] or h["bets"]["down"])
        confirmed = [v for v in self.verdicts if v["kind"] in ("confirmed", "partial")]
        devs = [abs(v["deviation"]) for v in self.verdicts
                if v["deviation"] is not None and v["kind"] in ("confirmed", "partial")]
        killed = [v for v in self.verdicts if v["kind"] in ("killed", "rejected")]
        cal = round(100 - sum(devs) / max(1, len(devs))) if devs else None
        return {
            "mode": "demo", "ts": time.time(),
            "gpu": dict(self.gpu),
            "user_votes": dict(self.user_votes),
            "gov": {"mode": self.mode, "autostart": self.autostart,
                    "budget_hours": {"limit": DAILY_HOURS, "used": round(self.budget["hours_used"], 1)},
                    "budget_tasks": {"limit": DAILY_TASKS, "used": self.budget["tasks_used"]},
                    "preempt_ratio": PREEMPT_RATIO, "approval_hours": APPROVAL_HOURS,
                    "daily_left_h": round(DAILY_HOURS - self.budget["hours_used"], 1)},
            "approvals": self.approvals,
            "queue": queue,
            "current": cur,
            "runs": [r for r in self.runs],
            "history": self.history[:8],
            "crew": {"agents": CREW, "chat": self.chat[-40:], "remarks": self.remarks,
                     "bets": [{"hid": h["id"], "title": h["title"], "status": h["status"],
                               "up": [AGENT_NAME[a] for a in h["bets"]["up"]],
                               "down": [AGENT_NAME[a] for a in h["bets"]["down"]]}
                              for h in self.queue if (h["bets"]["up"] or h["bets"]["down"])],
                     "leaders": [
                         {"agent": "morg", "rate": 0.64, "bets": 11, "brier": 0.19, "streak": 2},
                         {"agent": "shef", "rate": 0.71, "bets": 9, "brier": 0.17, "streak": 3},
                         {"agent": "gayka", "rate": 0.58, "bets": 12, "brier": 0.22, "streak": 1},
                         {"agent": "skif", "rate": 0.55, "bets": 13, "brier": 0.24, "streak": -1},
                         {"agent": "hronik", "rate": 0.52, "bets": 11, "brier": 0.25, "streak": 1},
                         {"agent": "krot", "rate": 0.48, "bets": 12, "brier": 0.27, "streak": -2},
                         {"agent": "stazhor", "rate": 0.40, "bets": 10, "brier": 0.30, "streak": 0},
                     ]},
            "verdicts": self.verdicts,
            "stats": {"calibration": cal,
                      "win_rate": round(100 * len(confirmed) / max(1, len(self.verdicts))),
                      "gpu_saved_h": 41, "killed_early": len(killed),
                      "open_bets": open_bets, "queue_len": sum(1 for h in self.queue if h["status"] in ("queued", "blocked"))},
            "checks": KILL_CHECKS,
        }


# ------------------------------------------------------------------ live-адаптер (лучшее усилие)
class LiveAdapter:
    """Читает живой профиль через штатные CLI инструментов.

    Любой сбой → None → сервер уходит в demo. Менять состояние live-профиля
    Mini App по дизайну не должен: воздействия идут через штатный шлюз Hermes.
    """

    def __init__(self):
        self.py = sys.executable or "python3"

    def _cli(self, *args):
        cmd = [self.py] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                              check=False, cwd=ROOT)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200])
        return json.loads(proc.stdout or "null")

    def state(self):
        q = self._cli("tools/queue.py", "list", "--top", "20", "--all", "--json")
        rows = q if isinstance(q, list) else q.get("items", [])
        if not rows:
            return None
        v = self._cli("tools/verdict.py", "list", "--limit", "12", "--json")
        verdicts = v if isinstance(v, list) else v.get("items", [])
        gpu = None
        try:
            gpu = self._cli("tools/gpu.py", "show", "--json")
        except Exception:
            pass
        return {"queue_raw": rows, "verdicts_raw": verdicts, "gpu_raw": gpu}


# ------------------------------------------------------------------ HTTP
LAB = None
LIVE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".json": "application/json"}.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def do_GET(self):
        global LAB
        if self.path.startswith("/api/state"):
            if LIVE is not None:
                live = None
                try:
                    live = LIVE.state()
                except Exception:
                    live = None
                if live:
                    self._json({"mode": "live", "live": live})
                    return
                if LAB is None:
                    LAB = DemoLab()
            elif LAB is None:
                LAB = DemoLab()
            self._json(LAB.state())
            return
        if self.path.startswith("/api/ping"):
            self._json({"ok": True, "mode": "live" if LIVE else "demo", "ts": time.time()})
            return
        name = self.path.split("?")[0].lstrip("/") or "index.html"
        self._file(name)

    def do_POST(self):
        global LAB
        if not self.path.startswith("/api/action"):
            self._json({"err": "not found"}, 404)
            return
        if LAB is None:
            LAB = DemoLab()
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "err": "bad json"}, 400)
            return
        with threading.Lock():
            try:
                self._json(LAB.act(body))
            except Exception as exc:  # демо не должен ронять сервер
                self._json({"ok": False, "err": str(exc)}, 500)


def main():
    global LIVE
    ap = argparse.ArgumentParser(description="Telegram Mini App — researchagen")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--demo", action="store_true", help="принудительная симуляция")
    ap.add_argument("--open", action="store_true", help="открыть браузер")
    args = ap.parse_args()

    if not args.demo:
        LIVE = LiveAdapter()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"[miniapp] {url}  ({'demo' if LIVE is None else 'live→demo fallback'})")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[miniapp] стоп")


if __name__ == "__main__":
    main()
