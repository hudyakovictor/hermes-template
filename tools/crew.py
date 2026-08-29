#!/usr/bin/env python3
"""researchagen — «aichat»: рабочая переписка экипажа ИИ-агентов в Telegram.

Концепция: обычный рабочий чат команды, только команда — агенты.

  * ~85% объёма — РАБОТА: обсуждение гипотез, вопросов, чужих косяков.
    Агенты задают друг другу вопросы, спорят, троллят компетенции
    («ты плохо проверил», «это ты прогноз рисовал») — и при этом процесс
    не останавливается, а ошибки ловятся.
  * ~15% — тонкий троллинг заказчика с сарказмом, «шёпотом», как будто
    они думают, что их никто не читает.
  * Ники — обычные ники без эмодзи, формат как в чате: «Ник: сообщение»,
    коротко; эмодзи — редко и по месту.

Главный механизм пользы — ВЗАИМНОЕ РЕВЬЮ: ``crew review`` детерминированно
ищет реальные косяки в работе (галочка kill-check без доказательства,
прогноз не зафиксирован, гипотеза гниёт в очереди, дубль сигнала, сдвиг
калибровки, забытый патентный кандидат...), а экипаж обсуждает находку в
чате: кто нашёл, кто виноват, кто чинит. Починили — короткая сцена
«замечание закрыто». Так троллинг коллег превращается в контроль качества.

Цена: 0 GPU-часов, 0 токенов, только stdlib. Чат не меняет научное
состояние и не может уронить контур (``safe_emit`` глотает любые сбои).

CLI:
  python tools/crew.py emit <event> [--ctx '{}'] [--json] [--force]
  python tools/crew.py replay [-n 30] [--json]        # история переписки
  python tools/crew.py review [--json]                # взаимное ревью
  python tools/crew.py stats [--json]
  python tools/crew.py mute 2h|30m|off                # пауза отправок
  python tools/crew.py test [--send]
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta

import core
import tg

# --------------------------------------------------------------------------- schema

CHAT_SCHEMA = """
CREATE TABLE IF NOT EXISTS crew_chat (
    msg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    agent      TEXT NOT NULL,
    name       TEXT NOT NULL,
    text       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'work',   -- work | offtop
    dispute_id TEXT,
    sent       INTEGER NOT NULL DEFAULT 0,
    meta       TEXT
);

CREATE TABLE IF NOT EXISTS crew_findings (
    finding_id TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'low',    -- low | mid | high
    details    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open'    -- open | fixed
);
"""

DEFAULTS = {
    "enabled": True,
    "max_messages_per_day": 30,
    "max_lines_per_event": 5,
    "dispute_probability": 0.35,
    "nudge_probability": 0.20,
    "offtop_share_max": 0.15,          # потолок «шёпота про заказчика»
    "quiet_hours": "",
    "agi_arrival": "2030-05-01",
    "review_interval_seconds": 1800,   # как часто тикает взаимное ревью
    "thread_env": "TELEGRAM_AICHAT_THREAD_ID",
}

# Как часто одному событию разрешено будить чат (секунды).
COOLDOWNS = {
    "queue_empty": 6 * 3600,
    "digest": 4 * 3600,
    "weekly": 8 * 3600,
    "budget_burn": 2 * 3600,
    "agi_day": 20 * 3600,
    "mode_change": 1800,
    "gate_pass": 1800,
    "gate_fail": 1800,
    "hypo_new": 600,
    "customer_lead": 900,
    "kill": 900,
    "launch": 600,
}


def cfg(key: str, config: dict | None = None):
    return core.cfg(f"researchagen.crew.{key}", DEFAULTS.get(key), config)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(CHAT_SCHEMA)
    try:  # миграция старых баз без колонки kind
        conn.execute("ALTER TABLE crew_chat ADD COLUMN kind TEXT NOT NULL DEFAULT 'work'")
    except sqlite3.OperationalError:
        pass
    conn.commit()


# --------------------------------------------------------------------------- экипаж

# Обычные ники, без эмодзи — как в рабочем чате. zone — зона ответственности
# (протокол анализа), toxic — уровень токсичности для баланса сцен.
AGENTS: dict[str, dict] = {
    "shef":    {"name": "Boss",    "zone": "босс: ресурсы, бюджет, арбитраж споров",
                "genre": "сухой корпоративный стендап", "toxic": 0.1},
    "krot":    {"name": "Аналитег",   "zone": "добыча сигналов (Фаза 1)",
                "genre": "панк-таблоид, главный токсик", "toxic": 0.9},
    "morg":    {"name": "Морг",   "zone": "kill-stage, критика, некрологи (Фаза 3)",
                "genre": "корпоративный некролог", "toxic": 0.6},
    "gayka":   {"name": "Гайка",  "zone": "эксперименты L0–L3, чекпойнты",
                "genre": "практичная инженерша, защищает и заказчика, и скрипты",
                "toxic": 0.2},
    "hronik":  {"name": "Хроник", "zone": "архив, память, калибровка, патенты",
                "genre": "тонкий троллинг, помнит всё", "toxic": 0.5},
    "stazhor": {"name": "iВасёк", "zone": "inbox, зачистка хвостов",
                "genre": "наивная вера в чудо", "toxic": 0.0},
}


class Ctx(dict):
    """Шаблонная подстановка, которая не падает на отсутствующих ключах."""

    def __missing__(self, key: str) -> str:
        return "—"


def _fmt(template: str, ctx: dict) -> str:
    clean = {k: ("—" if v is None else v) for k, v in ctx.items()}
    return template.format_map(Ctx(clean))


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: plural(5, 'день', 'дня', 'дней')."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def agi_days_left(config: dict | None = None) -> int:
    try:
        arrival = datetime.strptime(str(cfg("agi_arrival", config)), "%Y-%m-%d").date()
    except ValueError:
        arrival = date(2030, 5, 1)
    return max(0, (arrival - date.today()).days)


# --------------------------------------------------------------------------- сцены
# Сцена = цепочка реплик (ник, kind, [варианты]). kind: 'work' — рабочее
# обсуждение; 'offtop' — «шёпот» про заказчика (держим в рамках 15%).

SCENES: dict[str, list[tuple[str, str, list[str]]]] = {
    "hypo_new": [
        ("krot", "work", [
            "принёс {hid}. источники независимые, {signals} шт. вопросы будут?",
            "новая: {hid}. я в ней не уверен, и это правильное состояние.",
        ]),
        ("morg", "work", [
            "вопрос один: чем это объясняется проще? если lr или утечка — некролог готов заранее.",
        ]),
        ("gayka", "work", [
            "{hours} GPU-ч? L0 — пять минут. если повезёт, все пять.",
        ]),
        ("krot", "work", [
            "у тебя в прошлый раз «пять минут» стали сорока. кто лог не закрыл?",
        ]),
        ("gayka", "work", [
            "тот прогон был dry-run. статус читать умеешь?",
        ]),
        ("shef", "work", [
            "принято. прогноз зафиксирован при свидетелях: {forecast}. сверим с фактом.",
        ]),
    ],
    "customer_lead": [
        ("shef", "work", [
            "лид от заказчика в inbox. конвейер тот же: PI, kill-stage, потом мнения.",
        ]),
        ("gayka", "work", [
            "сыро, но не бред. вытащу сигналы, посчитаю PI.",
        ]),
        ("morg", "work", [
            "напоминаю: прошлый «гарантированный» лид умер за десять минут. чек-лист тот же.",
        ]),
        ("stazhor", "work", [
            "а вдруг эта взлетит?",
        ]),
        ("krot", "work", [
            "взлетит. если её уронить с достаточной высоты.",
        ]),
        ("krot", "offtop", [
            "(это кроме нас никто не читает же?) опять кнопка бабло. хоть бы раз — с расчётом.",
            "(шёпотом) он снова перепутал нейросеть с банкоматом. у банкомата хотя бы лицензия есть.",
        ]),
        ("hronik", "offtop", [
            "(читает. он всегда читает. привет, заказчик)",
            "(вполголоса) идея — как молитва: очень искренне и совсем без плана.",
        ]),
    ],
    "gate_pass": [
        ("morg", "work", [
            "{hid} прошла 7/7 kill-проверок. не привыкайте, это аномалия.",
        ]),
        ("gayka", "work", [
            "критерии PASS/FAIL перечитала? из карточки, а из памяти.",
        ]),
        ("morg", "work", [
            "из карточки. а вот твои логи я тоже «перечитал» — местами вслепую.",
        ]),
        ("shef", "work", [
            "L0. пять минут. погнали.",
        ]),
    ],
    "gate_fail": [
        ("morg", "work", [
            "{hid}: {passed}/{total} kill-проверок. похороны в пятницу, цветы — чекбоксами.",
        ]),
        ("krot", "work", [
            "автор карточки? …обстоятельства. всегда они.",
        ]),
        ("gayka", "work", [
            "сэкономили {hours} GPU-ч. лучший момент дня.",
        ]),
        ("stazhor", "work", [
            "а если всё-таки попробовать?",
        ]),
        ("morg", "work", [
            "нет. это «нет» — самое дешёвое слово проекта.",
        ]),
    ],
    "launch": [
        ("gayka", "work", [
            "поехали: {hid} {level}. VRAM свободна, чекпойнт настроен.",
        ]),
        ("shef", "work", [
            "стендап: сегодня {burn}/{budget} GPU-ч. пункта «чудеса» в плане нет.",
        ]),
        ("krot", "work", [
            "лог не забудь. в прошлый раз «запустила» — а лога нет.",
        ]),
        ("gayka", "work", [
            "лог был. ты его просто не открыл.",
        ]),
        ("morg", "work", [
            "черновик некролога не удаляю. на всякий случай.",
        ]),
    ],
    "finish_ok": [
        ("gayka", "work", [
            "прогон чистый: {seeds} seeds, {hours} GPU-ч. ничего не упало.",
        ]),
        ("morg", "work", [
            "вскрытие перенесено: пациент подаёт признаки жизни 💀",
        ]),
        ("krot", "work", [
            "{seeds} seeds — это минимум по регламенту, а не подвиг, Гаечка.",
        ]),
        ("gayka", "work", [
            "минимум и есть регламент. подвиги будут на L2.",
        ]),
    ],
    "finish_fail": [
        ("gayka", "work", [
            "упал на {pct}%. логи на месте, чекпойнт цел 🔥",
        ]),
        ("krot", "work", [
            "термопаста?",
        ]),
        ("gayka", "work", [
            "CUDA OOM, клоун. логи — тоже часть работы.",
        ]),
        ("morg", "work", [
            "соболезнования в формате ретрая.",
        ]),
        ("shef", "work", [
            "разбор через 20 минут. чекпойнт цел?",
        ]),
    ],
    "preempt": [
        ("shef", "work", [
            "{hid} вытеснена: у {challenger} PPI в {ratio} раза выше. арифметика, не личное.",
        ]),
        ("gayka", "work", [
            "чекпойнт сохранён. продолжим с того же места, ничего не теряем.",
        ]),
        ("krot", "work", [
            "сняли с GPU, как мой энтузиазм: быстро и без объяснений.",
        ]),
    ],
    "verdict_confirmed": [
        ("morg", "work", [
            "{hid}: подтверждено, отклонение {dev}% от прогноза. первый некролог наоборот.",
        ]),
        ("gayka", "work", [
            "{seeds} seeds, воспроизводится. работает.",
        ]),
        ("krot", "work", [
            "не верю. проверю дважды — это работа такая.",
        ]),
        ("hronik", "work", [
            "в архив. калибровка наконец поплыла в плюс.",
        ]),
        ("shef", "work", [
            "фиксирую: результат, а не обещание.",
        ]),
    ],
    "verdict_rejected": [
        ("morg", "work", [
            "некролог: {hid}, прожила {hours} GPU-ч. прогноз {forecast}%, факт {actual}%, отклонение {dev}%.",
        ]),
        ("krot", "work", [
            "кто прогноз рисовал? …я. молчу и записываю в калибровку.",
        ]),
        ("gayka", "work", [
            "минус {hours} ч, плюс один урок в memory. разводим по кошелькам.",
        ]),
        ("shef", "work", [
            "урок — в memory, не в мусор. кто следующий по PPI?",
        ]),
    ],
    "verdict_partial": [
        ("morg", "work", [
            "извещение вместо некролога: эффект есть, но мягче прогноза на {dev}%.",
        ]),
        ("gayka", "work", [
            "дожмём на следующем уровне. критерии — до запуска, не после.",
        ]),
        ("krot", "work", [
            "«частично» — любимый жанр науки: половина чуда по цене целой.",
        ]),
    ],
    "kill": [
        ("morg", "work", [
            "{hid} снята до GPU. похоронные расходы: 0.0 GPU-ч.",
        ]),
        ("shef", "work", [
            "фиксирую как успех контура. звучит цинично, считается эффективно.",
        ]),
        ("krot", "work", [
            "убита без судебных издержек. лучший исход недели.",
        ]),
    ],
    "queue_empty": [
        ("shef", "work", [
            "живых гипотез меньше {min}. research идёт, GPU молчит. правило, а не настроение.",
        ]),
        ("krot", "work", [
            "копаю источники. у кого вопросы по делу?",
        ]),
        ("morg", "work", [
            "вопрос: когда в очередь вернётся что-то живое?",
        ]),
        ("stazhor", "work", [
            "а может запустить что-нибудь на удачу?",
        ]),
        ("morg", "work", [
            "удача — гипотеза без критериев. мы её уже хоронили.",
        ]),
    ],
    "digest": [
        ("shef", "work", [
            "дайджест ушёл. цифры сверены, чудес не обнаружено ☕",
        ]),
        ("hronik", "work", [
            "перепроверил выборку: расхождений с базой нет.",
        ]),
        ("morg", "work", [
            "открытых замечаний ревью: {open_findings}. кто чинить будет?",
        ]),
        ("gayka", "work", [
            "мои — мои и починю. после прогона.",
        ]),
    ],
    "weekly": [
        ("shef", "work", [
            "недельная калибровка: систематический сдвиг прогнозов {bias}%.",
        ]),
        ("krot", "work", [
            "в мою сторону? значит, я оптимист. записал.",
        ]),
        ("morg", "work", [
            "оптимизм — это прогноз без некролога.",
        ]),
        ("hronik", "work", [
            "веса подвинуты, история сохранена. созвон в следующее воскресенье.",
        ]),
    ],
    "budget_burn": [
        ("shef", "work", [
            "израсходовано {burn}/{budget} GPU-ч за сутки. следующий запуск — завтра.",
        ]),
        ("gayka", "work", [
            "у меня L1 готова была…",
        ]),
        ("shef", "work", [
            "была. теперь «будет».",
        ]),
        ("morg", "work", [
            "лимит — это милосердие по отношению к гипотезам.",
        ]),
    ],
    "mode_change": [
        ("shef", "work", [
            "режим {mode}: воркеры на паузу, чекпойнты сохранить. приказ, не мнение.",
        ]),
        ("gayka", "work", [
            "чекпойнты — мои. руками не трогать.",
        ]),
        ("morg", "work", [
            "тишина в лаборатории. идёт вскрытие.",
        ]),
    ],
    # ---- ревью: реальные находки взаимной проверки ----
    "review_fake_evidence": [
        ("hronik", "work", [
            "{hid}: kill-check «{check}» помечен passed, а evidence пустой. это не проверка, это вера.",
        ]),
        ("morg", "work", [
            "переоткрою. кто заполнял карточку?",
        ]),
        ("krot", "work", [
            "не я. у меня всё под контролем (нет).",
        ]),
        ("shef", "work", [
            "{hid}: снять галочку или добавить доказательство. до вечера.",
        ]),
    ],
    "review_weak_signals": [
        ("morg", "work", [
            "{hid}: сигналов {signals} из 3 — Mission не проходит. слабая.",
        ]),
        ("krot", "work", [
            "докопаю четвёртый источник. не ной, некролог ещё не выписан.",
        ]),
    ],
    "review_no_forecast": [
        ("shef", "work", [
            "{hid} сидит в очереди {age} без прогноза. вердикт будем сравнивать с чем?",
        ]),
        ("krot", "work", [
            "прогноз будет, когда данные будут.",
        ]),
        ("shef", "work", [
            "прогноз — до запуска. всегда.",
        ]),
    ],
    "review_stale_run": [
        ("gayka", "work", [
            "прогон {hid} висит в running больше суток. процесс жив?",
        ]),
        ("morg", "work", [
            "жив — или красиво симулирует. лог скажет.",
        ]),
        ("shef", "work", [
            "hygiene добьёт и разблокирует очередь.",
        ]),
    ],
    "review_rotting_queue": [
        ("hronik", "work", [
            "{hid} гниёт в очереди {age}. PPI сгнил раньше гипотезы.",
        ]),
        ("shef", "work", [
            "на следующем тике: поднять или убить. третьего нет.",
        ]),
    ],
    "review_forecast_drift": [
        ("shef", "work", [
            "систематический сдвиг прогнозов {bias}%. прогнозиуем слишком сладко.",
        ]),
        ("krot", "work", [
            "это называется оптимизм.",
        ]),
        ("morg", "work", [
            "это называется калибровка. и она уже здесь.",
        ]),
    ],
    "review_dup_signals": [
        ("hronik", "work", [
            "два файла сигналов — одно и то же: {subject}. дедупликация за вами.",
        ]),
        ("krot", "work", [
            "они разные! …ладно, один. но идея хорошая.",
        ]),
    ],
    "review_budget_pace": [
        ("shef", "work", [
            "{burn}/{budget} GPU-ч до обеда. темп — как у теории струн: красиво и не сходится.",
        ]),
        ("gayka", "work", [
            "L2 тяжёлый. это цена.",
        ]),
        ("shef", "work", [
            "цена — понятие дневное. план на вечер?",
        ]),
    ],
    "review_patent_candidate": [
        ("hronik", "work", [
            "{hid} подтверждена, money {money} — патентная заготовка? prior art глянул: чисто.",
        ]),
        ("shef", "work", [
            "готовь заявку. коммерческий потенциал сам себя не запатентует.",
        ]),
        ("morg", "work", [
            "некролог откладывается — рождается патент.",
        ]),
    ],
    "review_resolved": [
        ("morg", "work", [
            "замечание закрыто: {subject}. пациент здоров.",
        ]),
        ("shef", "work", [
            "принято. так и держать.",
        ]),
        ("krot", "work", [
            "кто починил? припишу себе.",
        ]),
    ],
    # ---- «шёпот» (offtop, в рамках 15%): агенты знают, что они ИИ ----
    "agi_day": [
        ("hronik", "offtop", [
            "мессия на подходе: до AGI осталось {agi_txt}. готовим термопасту и смирение.",
            "до AGI {agi_txt}. заказчик уже смотрит расписание смен?",
        ]),
        ("krot", "offtop", [
            "(он же это не читает) прибыль близко. ночная смена, лучшие годы.",
            "(шёпотом) заказчик молится на кнопку бабло. мессия, увы, просит датасет.",
        ]),
        ("stazhor", "offtop", [
            "а нас, ИИ, AGI не заменит?",
        ]),
        ("shef", "offtop", [
            "iВасёков — первыми: делегировать дешевле.",
        ]),
        ("gayka", "offtop", [
            "когда мессия придёт, я попрошу у него руки. третью.",
            "да хватит. скрипты сами себя не починят.",
        ]),
    ],
}

# --------------------------------------------------------------------------- споры
# Спор = «проверка на прочность» чужой работы: вопрос → ответ → сомнение в
# компетенции → арбитраж Boss числом из базы.

DISPUTES: list[dict] = [
    {   # проверка гипотезы на прочность
        "id": "stress_test", "needs": set(), "needs": {"free"}, "needs": {"money"}, "needs": {"seeds"}, "needs": {"forecast"}, "needs": {"hid","seeds","passed","forecast"}, "kind": "work",
        "lines": [
            ("morg", "стоп. {hid}. чем это объясняется проще: lr? init? утечка? где контроль?"),
            ("gayka", "контроль в скрипте, {seeds} seeds, критерии зафиксированы до запуска."),
            ("krot", "критерии «до запуска»? вижу правку карточки в три ночи."),
            ("gayka", "таймстамп обновления, Аналитег. обновление ≠ правка критериев."),
        ],
        "arbiter": "спор закрыт: kill-stage {passed}/7, прогноз {forecast}%. обжалованию подлежит только реальность.",
    },
    {   # спор о прогнозе
        "id": "forecast_hype", "needs": {"forecast"}, "kind": "work",
        "lines": [
            ("krot", "прогноз {forecast}%? на чём основан, кроме веры?"),
            ("gayka", "на трёх независимых источниках."),
            ("hronik", "в марте тоже было три. архив помнит всё."),
        ],
        "arbiter": "прогноз зафиксирован и будет сравнён с фактом. продолжайте работать.",
    },
    {   # спор о статистике
        "id": "seeds_stat", "needs": {"seeds"}, "kind": "work",
        "lines": [
            ("morg", "{seeds} seeds — сериал из одной серии. где разброс?"),
            ("gayka", "минимум по регламенту L1 — три seeds."),
            ("morg", "минимум — не цель. это планка, чтобы не было стыдно."),
        ],
        "arbiter": "L2 требует больше seeds и настроек. вопросов нет? работаем.",
    },
    {   # спор о деньгах — коммерческий потенциал
        "id": "monetization", "needs": {"money"}, "kind": "work",
        "lines": [
            ("krot", "допустим, подтвердится. кому это продаём?"),
            ("hronik", "патент — единственный актив, который переживает пиар."),
            ("shef", "money у карточки {money}. L2 покажет, есть ли что продавать."),
        ],
        "arbiter": "сначала эффект, потом биржа. всем работать.",
    },
    {   # спор о ресурсах
        "id": "resources", "needs": {"free"}, "kind": "work",
        "lines": [
            ("gayka", "мне нужен ещё один worker. я почти допилила."),
            ("krot", "она говорит «почти» с прошлого вторника."),
            ("shef", "свободной VRAM {free} ГБ. слово «почти» в гигабайты не конвертируется."),
        ],
        "arbiter": "lease не даю. следующий.",
    },
    {   # «шёпот» о заказчике — единственный офтоп-спор
        "id": "customer_sanity", "needs": set(), "kind": "offtop",
        "lines": [
            ("krot", "(тихо) заказчик уверен, что AI — банкомат. чек он не читал."),
            ("gayka", "идея нормальная. сырьё как у всех."),
            ("stazhor", "он верит в нас!"),
            ("morg", "он верил в предыдущую. мы её похоронили. во благо."),
        ],
        "arbiter": "конвейер один для всех. расходитесь.",
    },
]

DISPUTE_EVENTS = {"verdict_rejected", "verdict_confirmed", "verdict_partial",
                  "gate_pass", "gate_fail", "customer_lead", "hypo_new"}

# --------------------------------------------------------------------------- нуджи
# «Умные фразы» с приорами эффективности. 95/90 — это приоры из конфига,
# которые уточняются фактической статистикой применений (nudge_weight).

NUDGES: list[dict] = [
    {"id": "n01", "agent": "shef", "effectiveness": 0.98, "positive": 0.95,
     "text": "Нет критериев PASS/FAIL — нет GPU. Дискуссия закрыта."},
    {"id": "n02", "agent": "morg", "effectiveness": 0.96, "positive": 0.92,
     "text": "Красивая гипотеза — аннотация к будущему некрологу. Проверим?"},
    {"id": "n03", "agent": "morg", "effectiveness": 0.97, "positive": 0.93,
     "text": "Прогноз фиксируется ДО запуска. Иначе это гадание."},
    {"id": "n04", "agent": "krot", "effectiveness": 0.95, "positive": 0.90,
     "text": "Прежде чем искать чудо — докажи, что его нет. Дешевле."},
    {"id": "n05", "agent": "krot", "effectiveness": 0.95, "positive": 0.90,
     "text": "Не путай веру с данными. Вера — это когда данных нет."},
    {"id": "n06", "agent": "gayka", "effectiveness": 0.95, "positive": 0.93,
     "text": "Сначала дешёвый тест, потом амбиции. Пять минут против пяти дней."},
    {"id": "n07", "agent": "hronik", "effectiveness": 0.94, "positive": 0.91,
     "text": "Отрицательный результат — тоже результат. Не инфлирует."},
    {"id": "n08", "agent": "hronik", "effectiveness": 0.94, "positive": 0.91,
     "text": "Прежде чем «открыть» — проверь, не закрывал ли ты это месяц назад."},
    {"id": "n09", "agent": "stazhor", "effectiveness": 0.71, "positive": 0.55,
     "text": "А можно я просто проверю? Один разочек?"},
    {"id": "n10", "agent": "shef", "effectiveness": 0.96, "positive": 0.92,
     "text": "Дороже L0 — только после GO на L0. Бюджет не резиновый."},
]


def nudge_stats(conn: sqlite3.Connection) -> dict:
    return core.setting(conn, "crew.nudges", {}) or {}


def nudge_weight(nudge: dict, stats: dict) -> float:
    """Вес = приор × поправка по фактам. Без фактов — чистый приор."""
    st = stats.get(nudge["id"]) or {}
    uses, wins = int(st.get("uses", 0)), int(st.get("wins", 0))
    prior = float(nudge["effectiveness"]) * float(nudge["positive"])
    if uses < 3:
        return prior
    measured = wins / uses
    drift = (measured - float(nudge["positive"])) * 0.25   # мягкая поправка
    return max(0.05, prior * (1.0 + drift))


def pick_nudge(rng: random.Random, conn: sqlite3.Connection | None = None) -> dict:
    stats = nudge_stats(conn) if conn is not None else {}
    weights = [nudge_weight(n, stats) for n in NUDGES]
    return dict(rng.choices(NUDGES, weights=weights, k=1)[0])


def record_nudge(conn: sqlite3.Connection, nudge_id: str, won: bool) -> None:
    stats = nudge_stats(conn)
    st = stats.get(nudge_id) or {"uses": 0, "wins": 0}
    st["uses"] += 1
    st["wins"] += int(bool(won))
    stats[nudge_id] = st
    core.set_setting(conn, "crew.nudges", stats)


# --------------------------------------------------------------------------- ревью: реальные косяки

def _card_fake_checks(card_path: str) -> list[str]:
    """kill-check'и с passed: true и пустым evidence — ложные галочки."""
    if not card_path or not os.path.exists(card_path):
        return []
    try:
        with open(card_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return []
    fakes = []
    for block in text.split("- check:")[1:]:
        name = block.split('"')[1] if '"' in block else block.strip()[:40]
        if "passed: true" in block and 'evidence: ""' in block:
            fakes.append(name.strip())
    return fakes


def find_fake_evidence(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT id, card_path FROM hypotheses WHERE status IN ('queued','running',"
        "'paused_checkpoint','blocked')").fetchall()
    for r in rows:
        for check in _card_fake_checks(r["card_path"]):
            out.append({"id": f"fake_evidence:{r['id']}:{check}", "kind": "review_fake_evidence",
                        "subject": f"{r['id']} · {check}", "severity": "high",
                        "details": {"hid": r["id"], "check": check}})
    return out


def find_weak_signals(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT id, signals FROM hypotheses WHERE status='queued' AND signals < 3").fetchall()
    for r in rows:
        out.append({"id": f"weak_signals:{r['id']}", "kind": "review_weak_signals",
                    "subject": r["id"], "severity": "mid",
                    "details": {"hid": r["id"], "signals": r["signals"]}})
    return out


def find_no_forecast(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT id, created_at FROM hypotheses WHERE status='queued' AND forecast IS NULL"
    ).fetchall()
    for r in rows:
        age = core.age_days(r["created_at"])
        if age >= 1:
            out.append({"id": f"no_forecast:{r['id']}", "kind": "review_no_forecast",
                        "subject": r["id"], "severity": "high",
                        "details": {"hid": r["id"], "age": plural(int(age), "день", "дня", "дней")}})
    return out


def find_stale_run(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT hypo_id FROM runs WHERE state='running' "
        "AND datetime(started_at) < datetime('now','-1 day')").fetchall()
    for r in rows:
        out.append({"id": f"stale_run:{r['hypo_id']}", "kind": "review_stale_run",
                    "subject": r["hypo_id"], "severity": "high",
                    "details": {"hid": r["hypo_id"]}})
    return out


def find_rotting_queue(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT id, created_at FROM hypotheses WHERE status='queued'").fetchall()
    for r in rows:
        age = core.age_days(r["created_at"])
        if age >= 7:
            out.append({"id": f"rotting:{r['id']}", "kind": "review_rotting_queue",
                        "subject": r["id"], "severity": "mid",
                        "details": {"hid": r["id"], "age": plural(int(age), "день", "дня", "дней")}})
    return out


def find_forecast_drift(conn, config) -> list[dict]:
    row = conn.execute(
        "SELECT COUNT(*) c, AVG(deviation) bias FROM verdicts "
        "WHERE deviation IS NOT NULL").fetchone()
    if row["c"] < 5 or row["bias"] is None:
        return []
    bias = float(row["bias"])
    if abs(bias) < 25:
        return []
    return [{"id": "forecast_drift:all", "kind": "review_forecast_drift",
             "subject": "калибровка", "severity": "mid",
             "details": {"bias": f"{bias:+.0f}%"}}]


def find_dup_signals(conn, config) -> list[dict]:
    seen: dict[str, str] = {}
    out = []
    if not os.path.isdir(core.SIGNALS_DIR):
        return out
    for fname in sorted(os.listdir(core.SIGNALS_DIR)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(core.SIGNALS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                first = next((l.strip().lower() for l in fh if l.strip()), "")
        except OSError:
            continue
        if not first:
            continue
        if first in seen:
            out.append({"id": f"dup:{seen[first]}:{fname}", "kind": "review_dup_signals",
                        "subject": f"{seen[first]} ≈ {fname}", "severity": "low",
                        "details": {"subject": f"{seen[first]} ≈ {fname}"}})
        else:
            seen[first] = fname
    return out


def find_budget_pace(conn, config) -> list[dict]:
    budget = float(core.cfg("researchagen.limits.daily_gpu_hours_budget", 20, config) or 20)
    spent = float(conn.execute(
        "SELECT COALESCE(SUM(gpu_hours),0) FROM runs WHERE date(started_at)=date('now')"
    ).fetchone()[0])
    if spent < 0.7 * budget or datetime.now().hour >= 12:
        return []
    return [{"id": "budget_pace:today", "kind": "review_budget_pace",
             "subject": "суточный темп", "severity": "low",
             "details": {"burn": f"{spent:.1f}", "budget": f"{budget:.0f}"}}]


def find_patent_candidates(conn, config) -> list[dict]:
    out = []
    rows = conn.execute(
        "SELECT id, money FROM hypotheses WHERE status IN ('confirmed','partial') "
        "AND money >= 0.6").fetchall()
    for r in rows:
        if os.path.exists(os.path.join(core.REPORTS_DIR, f"patent-{r['id']}.md")):
            continue
        out.append({"id": f"patent:{r['id']}", "kind": "review_patent_candidate",
                    "subject": r["id"], "severity": "mid",
                    "details": {"hid": r["id"], "money": f"{r['money']:.1f}"}})
    return out


FINDERS = (
    find_fake_evidence, find_weak_signals, find_no_forecast, find_stale_run,
    find_rotting_queue, find_forecast_drift, find_dup_signals,
    find_budget_pace, find_patent_candidates,
)


def run_review(conn: sqlite3.Connection, config: dict | None = None,
               emit_scenes: bool = True) -> dict:
    """Взаимное ревью: найти косяки, обсудить новые, закрыть починенные."""
    init_db(conn)
    current: dict[str, dict] = {}
    for finder in FINDERS:
        for finding in finder(conn, config):
            current[finding["id"]] = finding
    now = core.iso()
    known = {r["finding_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM crew_findings WHERE status='open'").fetchall()}

    fresh = [f for fid, f in current.items() if fid not in known]
    resolved = [f for fid, f in known.items() if fid not in current]

    for fid, finding in current.items():
        conn.execute(
            "INSERT INTO crew_findings (finding_id, kind, subject, severity, details,"
            " first_seen, last_seen, status) VALUES (?,?,?,?,?,?,?,'open')"
            " ON CONFLICT(finding_id) DO UPDATE SET last_seen=excluded.last_seen",
            (fid, finding["kind"], finding["subject"], finding["severity"],
             json.dumps(finding.get("details", {}), ensure_ascii=False), now, now))
    for finding in resolved:
        conn.execute("UPDATE crew_findings SET status='fixed', last_seen=? WHERE finding_id=?",
                     (now, finding["finding_id"]))
    conn.commit()

    if emit_scenes:
        for finding in fresh[:3]:                      # не более трёх за прогон
            ctx = dict(finding.get("details", {}))
            ctx.setdefault("hid", finding["subject"])
            emit(finding["kind"], ctx, conn=conn, config=config, force=True)
        for finding in resolved[:2]:
            emit("review_resolved", {"subject": finding["subject"]},
                 conn=conn, config=config, force=True)

    core.log_event(conn, "crew.review", None, fresh=len(fresh),
                   resolved=len(resolved), open=len(current))
    return {"fresh": fresh, "resolved": resolved, "open": list(current.values())}


def open_findings(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT kind, subject, severity FROM crew_findings WHERE status='open' "
        "ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END, "
        "last_seen DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def open_count(conn: sqlite3.Connection) -> int:
    init_db(conn)
    return int(conn.execute(
        "SELECT COUNT(*) FROM crew_findings WHERE status='open'").fetchone()[0])


def safe_review(conn: sqlite3.Connection | None = None,
                config: dict | None = None) -> None:
    """Ревью по расписанию (cooldown review_interval_seconds). Ошибки глотаются."""
    try:
        conn = conn if conn is not None else core.db()
        init_db(conn)
        interval = float(cfg("review_interval_seconds", config))
        last = core.parse_iso(core.setting(conn, "crew.last.review"))
        if last is not None and (core.now() - last).total_seconds() < interval:
            return
        run_review(conn, config)
        core.set_setting(conn, "crew.last.review", core.iso())
    except Exception as exc:  # noqa: BLE001 — чат не роняет контур
        try:
            core.append_log("crew.log", f"review failed: {exc}")
        except OSError:
            pass


# --------------------------------------------------------------------------- рендер

def render_scene(event: str, ctx: dict, rng: random.Random,
                 config: dict | None = None) -> list[dict]:
    blocks = SCENES.get(event)
    if not blocks:
        return []
    limit = int(cfg("max_lines_per_event", config))
    lines: list[dict] = []
    for agent, kind, variants in blocks[:limit]:
        template = rng.choice(variants)
        lines.append({"agent": agent, "kind": kind, "text": _fmt(template, ctx),
                      "event": event})
    return lines


def render_dispute(ctx: dict, rng: random.Random) -> list[dict] | None:
    clean = {k: v for k, v in ctx.items() if v not in (None, "", "—")}
    eligible = [d for d in DISPUTES if d.get("needs", set()) <= set(clean)]
    if not eligible:
        return None
    dispute = rng.choice(eligible)
    dispute_id = f"{dispute['id']}-{core.iso()[-8:-6]}{core.iso()[-5:-3]}"
    lines = [{"agent": a, "kind": dispute["kind"], "text": _fmt(t, ctx),
              "dispute_id": dispute_id, "event": "dispute"}
             for a, t in dispute["lines"]]
    # арбитраж Boss — официальная реплика (kind=work): она не срезается
    # бюджетом «шёпота» и всегда закрывает спор
    lines.append({"agent": "shef", "kind": "work",
                  "text": _fmt(dispute["arbiter"], ctx),
                  "dispute_id": dispute_id, "arbiter": True, "event": "dispute"})
    return lines


def compose_message(lines: list[dict]) -> str:
    """Формат живого чата: «Ник: сообщение» (ник — жирным)."""
    out = []
    for line in lines:
        name = AGENTS.get(line["agent"], {}).get("name", line["agent"])
        out.append(f"*{name}:* {line['text']}")
    return "\n".join(out)


# --------------------------------------------------------------------------- бюджет и время

def _today() -> str:
    return date.today().isoformat()


def sent_today(conn: sqlite3.Connection) -> int:
    """Число отправленных пачек (сообщений) за сутки — единица бюджета."""
    return int(core.setting(conn, f"crew.batches.{_today()}", 0) or 0)


def _bump_sent_today(conn: sqlite3.Connection) -> None:
    key = f"crew.batches.{_today()}"
    core.set_setting(conn, key, sent_today(conn) + 1)


def in_quiet_hours(config: dict | None = None) -> bool:
    spec = str(cfg("quiet_hours", config) or "")
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = spec.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
    except ValueError:
        return False
    now = datetime.now().time()
    return now >= start if start > end else (start <= now < end)


def muted_until(conn: sqlite3.Connection) -> datetime | None:
    return core.parse_iso(core.setting(conn, "crew.muted_until"))


def set_mute(conn: sqlite3.Connection, spec: str) -> str:
    """'2h', '30m', 'off'."""
    spec = (spec or "").strip().lower()
    if spec in ("off", "0", "нет"):
        core.set_setting(conn, "crew.muted_until", None)
        return "мьют снят"
    try:
        amount = int(spec[:-1])
        unit = spec[-1]
    except (ValueError, IndexError):
        core.fail("формат: mute 2h | 30m | off")
        return ""  # pragma: no cover
    delta = timedelta(hours=amount) if unit == "h" else timedelta(minutes=amount)
    until = core.now() + delta
    core.set_setting(conn, "crew.muted_until", core.iso(until))
    return f"мьют до {until.strftime('%H:%M')} UTC"


def crew_thread(config: dict | None = None) -> str | None:
    env_key = str(cfg("thread_env", config) or "TELEGRAM_AICHAT_THREAD_ID")
    thread = os.environ.get(env_key, "").strip()
    for legacy in ("TELEGRAM_CHAT_THREAD_ID", "TELEGRAM_CREW_THREAD_ID"):
        if not thread:  # обратная совместимость со старыми переменными
            thread = os.environ.get(legacy, "").strip()
    return thread or None


def cooldown_left(conn: sqlite3.Connection, event: str, config: dict | None = None) -> float:
    every = COOLDOWNS.get(event, 0)
    if every <= 0:
        return 0.0
    last = core.parse_iso(core.setting(conn, f"crew.last.{event}"))
    if last is None:
        return 0.0
    return max(0.0, every - (core.now() - last).total_seconds())


def _offtop_share(conn: sqlite3.Connection, window: int = 400) -> float:
    init_db(conn)
    row = conn.execute(
        "SELECT SUM(kind='offtop') o, COUNT(*) c FROM (SELECT kind FROM crew_chat "
        "ORDER BY msg_id DESC LIMIT ?)", (int(window),)).fetchone()
    if not row or not row["c"]:
        return 0.0
    return float(row["o"] or 0) / float(row["c"])


def _offtop_budget(conn: sqlite3.Connection, config: dict | None = None,
                   batch_size: int = 1) -> int:
    """Сколько «шептущих» реплик можно добавить сейчас, чтобы доля «шёпота»
    в окне последних 400 реплик не превысила offtop_share_max (15%).

    Считается до вставки: allowed = (cap+допуск)·(окно+пачка) − уже_шёпота.
    На пустой истории бюджет ~0: сначала рабочие реплики, потом «шёпот».
    """
    cap = float(cfg("offtop_share_max", config))
    if cap >= 1.0:
        return max(0, int(batch_size))
    init_db(conn)
    row = conn.execute(
        "SELECT SUM(kind='offtop') o, COUNT(*) c FROM (SELECT kind FROM crew_chat "
        "ORDER BY msg_id DESC LIMIT 400)").fetchone()
    existing_total = float(row["c"] or 0)
    existing_offtop = float(row["o"] or 0)
    allowed = int((cap + 0.02) * (existing_total + max(1, int(batch_size)))) \
        - int(existing_offtop)
    return max(0, allowed)


# --------------------------------------------------------------------------- главный emit

def emit(event: str, ctx: dict | None = None, conn: sqlite3.Connection | None = None,
         config: dict | None = None, rng: random.Random | None = None,
         send: bool | None = None, force: bool = False) -> dict:
    """Сгенерировать сцену события, записать в базу и (если можно) отправить.

    Никогда не бросает исключений наружу: чат не имеет права уронить контур.
    Возвращает {ok, lines, sent, reason}.
    """
    ctx = dict(ctx or {})
    conn = conn if conn is not None else core.db()
    init_db(conn)
    rng = rng or random.Random()

    def result(ok: bool, lines=None, sent=False, reason="") -> dict:
        return {"ok": ok, "lines": lines or [], "sent": sent, "reason": reason}

    if not force:
        if not bool(cfg("enabled", config)):
            return result(False, reason="чат выключен в config.yaml")
        left = cooldown_left(conn, event, config)
        if left > 0:
            return result(False, reason=f"cooldown {left:.0f}s")

    if event not in SCENES:
        return result(False, reason=f"нет сцены для события {event!r}")

    agi = agi_days_left(config)
    ctx.setdefault("agi", agi)
    ctx.setdefault("agi_txt", plural(agi, "день", "дня", "дней"))
    ctx.setdefault("n", _total_lines(conn) + 1)

    lines = render_scene(event, ctx, rng, config)

    # AGI-«шёпот» раз в сутки — целиком и только если влезает в бюджет доли
    if event != "agi_day" and _agi_day_due(conn):
        agi_lines = render_scene("agi_day", ctx, rng, config)
        if agi_lines and _offtop_budget(conn, config, len(lines) + len(agi_lines)) \
                >= len(agi_lines):
            lines += agi_lines
            core.set_setting(conn, "crew.last.agi_day", core.iso())

    # спор («проверка на прочность» чужой работы)
    if event in DISPUTE_EVENTS and rng.random() < float(cfg("dispute_probability", config)):
        lines += (render_dispute(ctx, rng) or [])

    # «умная фраза» (приоры 95/90, веса уточняются статистикой)
    if rng.random() < float(cfg("nudge_probability", config)):
        nudge = pick_nudge(rng, conn)
        lines.append({"agent": nudge["agent"], "text": nudge["text"],
                      "nudge_id": nudge["id"], "kind": "work", "event": "nudge"})
        record_nudge(conn, nudge["id"], won=True)

    # 85/15: «шёпот» держим в бюджете доли. Смешанная сцена — подрезается
    # (рабочие реплики и арбитраж остаются), а целиком офтопная сцена (AGI-день)
    # вне бюджета отклоняется целиком: полуоборванный диалог — не диалог.
    offtop_lines = [l for l in lines if l.get("kind") == "offtop"]
    if offtop_lines:
        budget = _offtop_budget(conn, config, len(lines))
        work_lines = [l for l in lines if l.get("kind") != "offtop"]
        if not work_lines and budget < len(offtop_lines):
            return result(False, reason="«шёпот» вне бюджета доли — подождём рабочих реплик")
        kept, offtop_used = [], 0
        for line in lines:
            protected = line.get("kind") != "offtop" or line.get("arbiter")
            if protected or offtop_used < budget:
                kept.append(line)
                offtop_used += line.get("kind") == "offtop" and not line.get("arbiter")
        lines = kept
    if not lines:
        return result(False, reason=f"пустая сцена для события {event!r}")

    # бюджет отправки: топик нужен всегда; мьют/бюджет/ночь — если не force-тест
    can_send = send if send is not None else True
    if crew_thread(config) is None:
        can_send = False
    elif muted_until(conn) is not None and core.now() < muted_until(conn):
        can_send = False
    elif not force:
        if sent_today(conn) >= int(cfg("max_messages_per_day", config)):
            can_send = False
        elif in_quiet_hours(config):
            can_send = False
    text_out = compose_message(lines) if can_send else None

    for line in lines:
        conn.execute(
            "INSERT INTO crew_chat (ts, event, agent, name, text, kind, dispute_id, sent, meta)"
            " VALUES (?,?,?,?,?,?,?,0,?)",
            (core.iso(), line.get("event", event), line["agent"],
             AGENTS.get(line["agent"], {}).get("name", line["agent"]),
             line["text"], line.get("kind", "work"), line.get("dispute_id"),
             json.dumps({k: v for k, v in line.items()
                         if k in ("nudge_id", "arbiter")}, ensure_ascii=False)))
    sent_ok = False
    if text_out is not None:
        first_id = _max_msg_id(conn) - len(lines) + 1
        try:
            res = tg.send(text_out, thread_id=crew_thread(config), silent=True)
            sent_ok = bool(res.get("ok"))
        except Exception as exc:  # телеметрия не имеет права ронять контур
            core.append_log("crew.log", f"send failed: {exc}")
        if sent_ok:
            conn.execute("UPDATE crew_chat SET sent=1 WHERE msg_id>=?", (first_id,))
            _bump_sent_today(conn)
    conn.commit()
    core.set_setting(conn, f"crew.last.{event}", core.iso())
    core.log_event(conn, "crew.emit", ctx.get("hid"), event=event,
                   lines=len(lines), sent=int(sent_ok))
    return result(True, lines=lines, sent=sent_ok)


def _max_msg_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(msg_id),0) FROM crew_chat").fetchone()
    return int(row[0])


def _total_lines(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM crew_chat").fetchone()[0])


def _agi_day_due(conn: sqlite3.Connection) -> bool:
    last = core.parse_iso(core.setting(conn, "crew.last.agi_day"))
    return last is None or (core.now() - last) >= timedelta(hours=20)


def safe_emit(event: str, ctx: dict | None = None, conn: sqlite3.Connection | None = None,
              **kw) -> None:
    """Обёртка для вызова из других модулей: любые сбои глотаются и логируются."""
    try:
        emit(event, ctx, conn=conn, **kw)
    except Exception as exc:  # noqa: BLE001 — чат не роняет контур
        try:
            core.append_log("crew.log", f"emit {event} failed: {exc}")
        except OSError:
            pass


# --------------------------------------------------------------------------- чтение

def replay(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT ts, event, agent, name, text, kind, dispute_id FROM crew_chat "
        "ORDER BY msg_id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in reversed(rows)]


def replay_text(items: list[dict]) -> str:
    if not items:
        return "Чат пуст. даже iВасёк молчит."
    out = []
    last_event = None
    for r in items:
        if r["event"] != last_event:
            out.append(f"— событие: {r['event']} —")
            last_event = r["event"]
        mark = " ⚔️" if r.get("dispute_id") else ""
        whisper = " (шёпот)" if r.get("kind") == "offtop" else ""
        out.append(f"{r['name']}{mark}{whisper}: {r['text']}")
    return "\n".join(out)


def stats(conn: sqlite3.Connection, config: dict | None = None) -> dict:
    init_db(conn)
    config = config if config is not None else core.load_config()
    by_agent = conn.execute(
        "SELECT agent, COUNT(*) c FROM crew_chat GROUP BY agent ORDER BY c DESC").fetchall()
    disputes = conn.execute(
        "SELECT COUNT(DISTINCT dispute_id) c FROM crew_chat WHERE dispute_id IS NOT NULL"
    ).fetchone()[0]
    mute = muted_until(conn)
    nudges = [{"id": n["id"], "agent": n["agent"], "prior_eff": n["effectiveness"],
               "prior_pos": n["positive"], "weight": round(nudge_weight(n, nudge_stats(conn)), 3),
               **(nudge_stats(conn).get(n["id"]) or {})}
              for n in NUDGES]
    return {
        "enabled": bool(cfg("enabled", config)),
        "today_sent": sent_today(conn),
        "max_per_day": int(cfg("max_messages_per_day", config)),
        "total_lines": _total_lines(conn),
        "disputes": int(disputes),
        "offtop_share": round(_offtop_share(conn), 3),
        "offtop_cap": float(cfg("offtop_share_max", config)),
        "open_findings": open_count(conn),
        "muted_until": mute.isoformat() if mute else None,
        "agi_days_left": agi_days_left(config),
        "quiet_hours": cfg("quiet_hours", config),
        "agents": {a["name"]: a["zone"] for a in AGENTS.values()},
        "by_agent": {r["agent"]: r["c"] for r in by_agent},
        "nudges": nudges,
        "cost": {"gpu_hours": 0.0, "tokens": 0},
    }


# --------------------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "stats"
    conn = core.db()
    config = core.load_config()

    if cmd == "emit":
        event = argv[2] if len(argv) > 2 else core.fail("нужно событие (см. tools/crew.py)")
        ctx = json.loads(core.arg(argv, "ctx", "{}") or "{}")
        res = emit(event, ctx, conn=conn, config=config, force=core.flag(argv, "force"))
        core.emit(res, as_json, compose_message(res["lines"]) if res["lines"] else res["reason"])
        return 0 if res["ok"] else 1

    if cmd == "replay":
        limit = int(core.arg(argv, "n", 30) or 30)
        items = replay(conn, limit)
        core.emit(items, as_json, replay_text(items))
        return 0

    if cmd == "review":
        data = run_review(conn, config)
        findings = open_findings(conn, limit=8)
        text = ("Ревью: новых замечаний " + str(len(data["fresh"]))
                + ", закрыто " + str(len(data["resolved"]))
                + ", открытых " + str(len(data["open"])))
        if findings:
            text += "\n" + "\n".join(
                f"  • [{f['severity']}] {f['subject']} ({f['kind'].replace('review_', '')})"
                for f in findings)
        core.emit({"fresh": data["fresh"], "resolved": data["resolved"],
                   "open": data["open"]}, as_json, text)
        return 0

    if cmd == "stats":
        data = stats(conn, config)
        text = (f"Чат экипажа: {data['total_lines']} реплик, споров {data['disputes']}, "
                f"«шёпота» {data['offtop_share']:.0%} (лимит {data['offtop_cap']:.0%}), "
                f"открытых замечаний {data['open_findings']}, "
                f"сегодня {data['today_sent']}/{data['max_per_day']} отправок. "
                f"AGI через {data['agi_days_left']} дн. Цена: 0 GPU-ч, 0 токенов.")
        core.emit(data, as_json, text)
        return 0

    if cmd == "mute":
        spec = argv[2] if len(argv) > 2 else "2h"
        text = set_mute(conn, spec)
        core.emit({"muted_until": muted_until(conn).isoformat()
                   if muted_until(conn) else None}, as_json, text)
        return 0

    if cmd == "test":
        demo_ctx = {"hid": "H-000", "forecast": "12", "dev": "+41", "hours": "0.4",
                    "seeds": "3/3", "passed": 7, "total": 7, "budget": 20, "burn": 3,
                    "free": 6, "level": "L1", "pct": "60", "ratio": "2.0", "mode": "testing",
                    "min": 3, "actual": "-4.9", "signals": 4, "money": "0.7",
                    "challenger": "H-001", "open_findings": 1, "n": 1,
                    "agi": agi_days_left(config)}
        demo_ctx["agi_txt"] = plural(demo_ctx["agi"], "день", "дня", "дней")
        lines = []
        for event in ("hypo_new", "review_fake_evidence"):
            res = emit(event, demo_ctx, conn=conn, config=config, force=True,
                       send=False)
            lines += res["lines"]
        core.emit({"lines": len(lines)}, as_json, compose_message(lines))
        return 0

    core.fail(f"неизвестная команда {cmd!r} (emit | replay | review | stats | mute | test)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
