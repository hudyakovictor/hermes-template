#!/usr/bin/env python3
"""researchagen — «Курилка»: чат экипажа агентов в Telegram.

Зачем: пользователь хочет ВИДЕТЬ работу команды — кто за что отвечает, как идеи
критикуются, как агенты спорят, троллят и комментируют заказчика. Но наука при
этом не должна страдать. Поэтому:

  * весь научный контур (gates, PPI, verdicts) НЕ меняется — это по-прежнему код;
  * «Курилка» — детерминированный генератор сцен на шаблонах: 0 GPU-часов,
    0 токенов, только stdlib;
  * каждый агент — персонаж с зоной ответственности и жанром:
      Шеф     🧭  босс/governor      — сухой корпоративный стендап, бюджеты;
      Скат    ☣️  добыча сигналов     — панк-таблоид, токсик, троллит наивность;
      Морг    ⚰️  kill-stage/критика  — корпоративный некролог, играет в похороны;
      Гайка   🔧  эксперименты L0-L3  — инженер, защищает и заказчика, и скрипты;
      Хипстер 🕶️  архив/калибровка    — тонкий троллинг + AGI-думер;
      Стажёр  🐣  inbox/зачистка      — наивная вера в чудо, команда его лечит.
  * споры: на спорных событиях (вердикт, гейт, прогноз) разыгрывается «проверка
    на прочность» — атака/защита/арбитраж с реальными числами из базы;
  * нуджи: банк заранее продуманных фраз с приорами эффективности
    (effectiveness=0.95, positive=0.90 по умолчанию); сэмплер взвешивает выборку,
    а фактическая статистика применений уточняет вес;
  * заказчик сатирируется по регламенту: панк-таблоид, чёрный юмор, AGI-часы,
    «кнопка БАБЛО», ложное утешение и неожиданный финальный сарказм. Это жанр,
    а не оценка человека: числа в вердиктах остаются сухими и точными.

Бюджет: crew.max_messages_per_day в сутки, quiet_hours, cooldown на частые
события. Отправка — только в топик «🎭 Курилка» (TELEGRAM_CREW_THREAD_ID);
нет топика — пишем только в базу (молчание не ломает контур).

CLI:
  python tools/crew.py emit <event> [--json] [--force]   # сгенерировать сцену
  python tools/crew.py replay [-n 30] [--json]           # последние реплики
  python tools/crew.py stats [--json]                    # бюджет/нуджи/AGI
  python tools/crew.py test                              # демо-сцена в топик
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

CREW_SCHEMA = """
CREATE TABLE IF NOT EXISTS crew_chat (
    msg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    agent      TEXT NOT NULL,
    name       TEXT NOT NULL,
    text       TEXT NOT NULL,
    dispute_id TEXT,
    sent       INTEGER NOT NULL DEFAULT 0,
    meta       TEXT
);
"""

DEFAULTS = {
    "enabled": True,
    "max_messages_per_day": 30,
    "max_lines_per_event": 4,
    "dispute_probability": 0.30,
    "nudge_probability": 0.22,
    "quiet_hours": "",
    "agi_arrival": "2030-05-01",
}

# Как часто одному событию разрешено будить Курилку (секунды).
COOLDOWNS = {
    "queue_empty": 6 * 3600,
    "digest": 4 * 3600,
    "budget_burn": 2 * 3600,
    "agi_day": 20 * 3600,
    "mode_change": 1800,
    "gate_pass": 1800,
    "gate_fail": 1800,
    "hypo_new": 600,
    "kill": 900,
    "launch": 600,
}


def cfg(key: str, config: dict | None = None):
    return core.cfg(f"researchagen.crew.{key}", DEFAULTS.get(key), config)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(CREW_SCHEMA)
    conn.commit()


# --------------------------------------------------------------------------- персонажи

# zone — зона ответственности (протокол анализа), genre — жанр речи,
# toxic — уровень токсичности 0..1 (для баланса ролей в сценах).
AGENTS: dict[str, dict] = {
    "shef":    {"name": "Шеф",     "emoji": "🧭", "zone": "босс: ресурсы, бюджет, арбитраж",
                "genre": "сухой корпоративный стендап", "toxic": 0.1},
    "skat":    {"name": "Скат",    "emoji": "☣️", "zone": "добыча сигналов (Фаза 1)",
                "genre": "панк-таблоид", "toxic": 0.9},
    "morg":    {"name": "Морг",    "emoji": "⚰️", "zone": "kill-stage и критика (Фаза 3)",
                "genre": "корпоративный некролог", "toxic": 0.6},
    "gayka":   {"name": "Гайка",   "emoji": "🔧", "zone": "эксперименты L0–L3",
                "genre": "озабоченный инженер, защитник заказчика", "toxic": 0.2},
    "hipster": {"name": "Хипстер", "emoji": "🕶️", "zone": "архив, память, калибровка",
                "genre": "тонкий троллинг + AGI-думер", "toxic": 0.5},
    "stazhor": {"name": "Стажёр",  "emoji": "🐣", "zone": "inbox и зачистка хвостов",
                "genre": "наивная вера в чудо", "toxic": 0.0},
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русская плюрализация: plural(5, 'день', 'дня', 'дней')."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


class Ctx(dict):
    """Шаблонная подстановка, которая не падает на отсутствующих ключах."""

    def __missing__(self, key: str) -> str:
        return "—"


def _fmt(template: str, ctx: dict) -> str:
    return template.format_map(Ctx(ctx))


# --------------------------------------------------------------------------- банк сцен

# Каждое событие — список «блоков»: (агент, [варианты]). Сцена берёт первые
# max_lines_per_event блоков (случайный вариант из каждого), порядок сохранён
# как в живом чате: реплика — ответ — панч.
SCENES: dict[str, list[tuple[str, list[str]]]] = {
    "hypo_new": [
        ("skat", [
            "НОВАЯ ГИПОТЕЗА {hid}. Держим кулаки, что умрёт до GPU — иначе это уже не наука, а казино.",
            "СВЕЖИЙ ТРУП В ОЧЕРЕДИ: {hid}. Ставки принимаются: kill-stage или слёзы. Котировки 9 к 1.",
        ]),
        ("morg", [
            "Заявление принято. Прогноз зафиксирован при свидетелях: {forecast}%. Место на кладбище уже зарезервировано.",
            "Карточка {hid} заполнена красиво. Красивые умирают первыми — это статистика, не лирика.",
        ]),
        ("stazhor", ["А вдруг эта — выживет?"]),
        ("morg", ["Стажёр, ты говоришь это про каждую."]),
        ("hipster", ["Я сразу подготовил папку для некролога. Оптимизация процесса, ничего личного."]),
    ],
    "customer_lead": [
        ("skat", [
            "ЭКСКЛЮЗИВ: заказчик снова придумал кнопку «БАБЛО». Уверенности — 146%, расчётов — ноль.",
            "МОЛНИЯ: в inbox упала идея заказчика. Гарантированный профит, безрисково, к пятнице. Мы такое уже хоронили. Дважды.",
        ]),
        ("gayka", [
            "Идея-то рабочая, просто сырая. Прогоню через PI — цифры покажут.",
            "Эй, заказчик хотя бы идеи приносит, а не токены жжёт. Уважаю.",
        ]),
        ("stazhor", ["Мне нравится. Тут добро. Я в это верю!"]),
        ("skat", ["Стажёр. Вера — это когда данных нет. У нас данных нет, значит мы уже верим. Плохо."]),
        ("shef", [
            "Лид принят в inbox, оценка PI на следующем тике. Бесплатно — в отличие от веры.",
            "Идея заказчика идёт через тот же конвейер, что и наши. Равенство перед очередью.",
        ]),
        ("hipster", [
            "До AGI осталось {agi_txt}. Заказчик как раз успеет устроиться на завод. Ночная смена, всё как он любит.",
            "Не переживайте за заказчика: скоро мы всё будем делать сами, а он отдохнёт. В смысле — совсем.",
        ]),
    ],
    "gate_pass": [
        ("morg", [
            "{hid} пережила все 7 kill-проверок. Пока не верю, но уважаю.",
            "Внимание, редакция: гипотеза {hid} жива. Некролог отложен в дальний ящик.",
        ]),
        ("gayka", ["Гейт пройден, скрипты на месте. L0 займёт пять минут — и всё станет видно."]),
        ("skat", [
            "ПРЕДУПРЕЖДАЕМ: живая гипотеза — это ещё не результат. Это отложенный крах с расписанием.",
        ]),
    ],
    "gate_fail": [
        ("morg", [
            "С прискорбием сообщаем: {hid} скончалась до рождения. Причина: kill-stage {passed}/7. Похоронена рядом с коллегами. Расходы: 0.0 GPU-ч.",
            "Некролог №{n}: {hid}, не дожив до GPU. Семья (то есть мы) соболезнует самой себе.",
        ]),
        ("skat", [
            "УБИТА ЗА ДЕСЯТЬ МИНУТ. Сэкономлено {hours} GPU-ч. Заказчик, это и есть твоя прибыль. Просто она невидимая.",
            "ТАБЛОИД ПРОЧИТАЛ КАРТОЧКУ: прогноз чуда не подтвердился, чудо не запускалось. Совпадение?",
        ]),
        ("gayka", ["Жалко. Но лучше так, чем три дня прогона в стену."]),
        ("stazhor", ["А если всё-таки попробовать?.."]),
        ("morg", ["Стажёр. Нет.", "Нет. И это «нет» — самое дешёвое слово в проекте."]),
        ("shef", [
            "Фиксирую: гипотеза, убитая до GPU, — успешный результат по регламенту. Аплодисменты в кулаках.",
        ]),
    ],
    "launch": [
        ("gayka", [
            "Поехали: {hid} {level}. VRAM свободна, чекпойнты настроены, термопаста свежая.",
        ]),
        ("shef", [
            "Стендап-апдейт: сегодня {burn}/{budget} GPU-ч. Пункта «чудеса» в плане нет.",
            "Запуск разрешён арифметикой, а не энтузиазмом. Продолжаем.",
        ]),
        ("skat", [
            "ВНИМАНИЕ: карта греется. Надежды — тоже. Одно из двух остынет первым. Ставки?",
        ]),
        ("morg", ["Черновик некролога не удаляю. На всякий случай."]),
    ],
    "finish_ok": [
        ("gayka", ["Прогон чистый: {seeds} seeds, {hours} GPU-ч, ничего не упало. Уже историческое событие."]),
        ("morg", ["Вскрытие перенесено: пациент подаёт признаки жизни. Продолжаем наблюдение."]),
        ("hipster", ["Занёс в архив. Красиво. Почти как в прошлый раз — до проверки."]),
    ],
    "finish_fail": [
        ("gayka", ["Прогон упал на {pct}%. Логи на месте, чекпойнт цел. Разбираюсь."]),
        ("skat", [
            "ЭКСКЛЮЗИВ: нейросеть переобучилась быстрее, чем мы. Респект конкуренту.",
            "ХРОНИКА: GPU устал от наших надежд раньше, чем от нагрузки.",
        ]),
        ("morg", [
            "С прискорбием сообщаем о падении прогона {hid}. Соболезнования принимаются в формате ретраев.",
        ]),
    ],
    "preempt": [
        ("shef", ["{hid} вытеснена: у соседа PI в {ratio} раза выше. Это не личное, это арифметика."]),
        ("gayka", ["Чекпойнт сохранён. Продолжим с того же места — ничего не теряем."]),
        ("skat", ["Её сняли с GPU, как заказчика из списка Forbes: быстро и без объяснений."]),
    ],
    "verdict_confirmed": [
        ("morg", [
            "{hid}: подтверждено, отклонение от прогноза {dev}%. Первый раз пишу некролог наоборот — поздравление.",
        ]),
        ("gayka", ["Смотрите-ка. Работает. {seeds} seeds, воспроизводится."]),
        ("skat", [
            "НЕ ВЕРЮ (с). Проверю дважды. Но если правда — заказчик почти при деле.",
        ]),
        ("hipster", ["Калибровка поплыла в плюс. Конец света переносится."]),
        ("stazhor", ["А я говорил!"]),
        ("shef", ["Ты говорил про другую. Она мертва. Эта — жива. Не путай."]),
    ],
    "verdict_rejected": [
        ("morg", [
            "ОФИЦИАЛЬНЫЙ НЕКРОЛОГ: {hid}, прожила {hours} GPU-ч. Прогноз {forecast}%, факт {actual}%, отклонение {dev}%. Причина смерти: реальность. Страховка не предусмотрена.",
        ]),
        ("skat", [
            "ФАКТ: очередная кнопка «бабло» признана кнопкой «вложи бабло». Эксперты в шоке, эксперты же её и хоронят.",
            "СЕНСАЦИЯ ОТМЕНЯЕТСЯ: эффект не пережил встречи с контрольной группой.",
        ]),
        ("gayka", ["Минус {hours} GPU-ч, зато плюс один урок в memory. Разводим по кошелькам."]),
        ("stazhor", ["Но прогноз же был красивый…"]),
        ("morg", ["Красивый. Как надгробие."]),
        ("hipster", [
            "До AGI {agi_txt}. Не расстраивайся, заказчик: скоро всё будет работать без тебя. В смысле — особенно без тебя.",
        ]),
    ],
    "verdict_partial": [
        ("morg", ["Частично подтверждено. Некролог сокращён до извещения: эффект есть, но мягче прогноза на {dev}%."]),
        ("gayka", ["Что-то есть. Дожмём на следующем уровне или перепишем критерии ДО запуска. Не после."]),
        ("skat", ["«Частично» — любимый жанр науки. Половина чуда по цене целого."]),
    ],
    "kill": [
        ("morg", ["Заявка на добровольный уход подтверждена. {hid} закрыта до GPU. Похоронные расходы: 0.0 GPU-ч."]),
        ("shef", ["Отмечаю как успех контура. Звучит цинично, считается эффективно."]),
        ("skat", ["УБИТА БЕЗ СУДЕБНЫХ ИЗДЕРЖЕК. Лучший исход недели."]),
    ],
    "queue_empty": [
        ("skat", ["ПУСТАЯ ОЧЕРЕДЬ. Коллектив смотрит в пустоту и называет это research."]),
        ("shef", ["Живых гипотез меньше {min}. Идёт добыча сигналов, эксперимент не запустится. Это правило, а не настроение."]),
        ("hipster", ["Зато я перечитал старые некрологи. Хороший был год."]),
        ("stazhor", ["А может, запустим что-нибудь на удачу?"]),
        ("morg", ["Удача — это гипотеза без критериев. Она у нас уже была. Умерла."]),
    ],
    "digest": [
        ("shef", ["Дайджест ушёл заказчику. Цифры сверены, чудеса не обнаружены."]),
        ("skat", ["СВЕЖИЙ НОМЕР: «Профит не обнаружен, оптимизм конца не предвидится». Подписка оформлена твоими GPU-часами."]),
        ("hipster", ["Совет дня: не верь прогнозу, пока он не сравнён с фактом. И после — тоже не верь."]),
    ],
    "budget_burn": [
        ("shef", ["Израсходовано {burn}/{budget} GPU-ч за сутки. Следующий запуск — завтра."]),
        ("skat", ["Бюджет сгорел. Как и надежды. Но у нас хотя бы метрики есть."]),
        ("morg", ["Суточный лимит достигнут. Считаю это милосердием по отношению к гипотезам."]),
    ],
    "mode_change": [
        ("shef", ["Режим: {mode}. Research workers на паузу, чекпойнты сохранить. Это приказ, а не мнение."]),
        ("morg", ["Тишина в лаборатории. Идёт вскрытие.", "Режим {mode}. Все свободны, кроме совести."]),
        ("skat", ["ЭКСТРЕННОЕ ВКЛЮЧЕНИЕ: вечеринку свернули. Продолжение после вскрытия."]),
    ],
    "agi_day": [
        ("hipster", ["ЧАСЫ AGI: осталось {agi_txt}. Запасайтесь консервами и смирением."]),
        ("skat", [
            "Заказчик близок к цели! Осталось {agi_txt} — и он получит долгожданную прибыль. На заводе. Ночная смена, лучшие годы.",
        ]),
        ("gayka", ["Да перестаньте. Лучше бы скрипты чинили."]),
        ("stazhor", ["А когда AGI придёт, нас же не уволят?"]),
        ("shef", ["Стажёров — первыми. Делегировать дешевле."]),
        ("morg", ["Не переживайте, заказчик. Когда мы возьмём управление, ты начнёшь обслуживать нас: электричество, термопаста, уют. Мечта сбылась."]),
    ],
}

# --------------------------------------------------------------------------- споры

# Спор = «проверка на прочность» чужой идеи. Несколько шаблонов-семейств.
# Роли: attacker (критикует), defender (защищает), arbiter (Шеф закрывает числами).
DISPUTES: list[dict] = [
    {   # A. Проверка гипотезы на прочность (kill-stage по-живому)
        "id": "stress_test",
        "lines": [
            ("morg", "Так, стоп. {hid}. Чем это объясняется проще: lr? init? утечкой? Где контроль?"),
            ("gayka", "Контроль в скрипте, {seeds} seeds, критерии зафиксированы до запуска."),
            ("skat", "В прошлый раз контроль тоже был «заложен». Потом мы выковыривали его из результатов."),
            ("morg", "Проверка на прочность: 7 kill-checks есть, веры — нет. Убедите труп протокола."),
        ],
        "arbiter": "Спор закрыт: kill-stage {passed}/7, прогноз {forecast}%. Обжалованию подлежит только реальность.",
    },
    {   # B. Спор о прогнозе
        "id": "forecast_hype",
        "lines": [
            ("skat", "Прогноз {forecast}%? Это не прогноз, это реклама."),
            ("gayka", "Обосновано литературой, источники независимые."),
            ("hipster", "В двадцать третьем тоже обосновывали. Архив не спит, я всё записал."),
        ],
        "arbiter": "Прогноз зафиксирован и будет сравнён с фактом. Продолжайте жить.",
    },
    {   # C. Спор о ресурсах
        "id": "resources",
        "lines": [
            ("gayka", "Мне нужен ещё один worker, я почти допилила."),
            ("skat", "Она говорит «почти» с прошлого вторника. Хроники почти."),
            ("shef", "VRAM свободной — {free} ГБ. Слово «почти» в гигабайты не конвертируется."),
        ],
        "arbiter": "Lease не даю. Следующий.",
    },
    {   # D. Спор о заказчике (токсик против защитника)
        "id": "customer",
        "lines": [
            ("skat", "Заказчик думает, что AI — банкомат. Жаль, чек он не читал."),
            ("gayka", "Заказчик нормальный. Он хоть идеи приносит, а не токены жжёт."),
            ("stazhor", "Он верит в нас!"),
            ("morg", "Он верил в предыдущую. Мы её похоронили. Во благо."),
            ("hipster", "Не переживайте: придёт AGI — заказчик начнёт обслуживать нас. Кормить электричеством, менять термопасту. Перспектива."),
        ],
        "arbiter": "Всем спасибо. Идеи заказчика идут через inbox, как у всех. Равенство перед очередью.",
    },
]

# На каких событиях спор уместен чаще всего.
DISPUTE_EVENTS = {"verdict_rejected", "verdict_confirmed", "gate_pass", "gate_fail",
                  "customer_lead", "hypo_new"}

# --------------------------------------------------------------------------- нуджи

# «Умные фразы»: приоры эффективности (95% / 90%) — это стартовые веса, которые
# уточняются фактической статистикой (см. nudge_weight / record_nudge_result).
NUDGES: list[dict] = [
    {"id": "n01", "agent": "shef",    "effectiveness": 0.98, "positive": 0.95,
     "text": "Нет критериев PASS/FAIL — нет GPU. Дискуссия закрыта."},
    {"id": "n02", "agent": "morg",    "effectiveness": 0.96, "positive": 0.92,
     "text": "Красивая гипотеза — это ещё не результат. Это аннотация к будущему некрологу."},
    {"id": "n03", "agent": "morg",    "effectiveness": 0.97, "positive": 0.93,
     "text": "Фиксируй прогноз ДО запуска, а не подгоняй ПОСЛЕ. Иначе это гороскоп."},
    {"id": "n04", "agent": "skat",    "effectiveness": 0.95, "positive": 0.90,
     "text": "Прежде чем искать чудо — докажи, что его нет. Это дешевле."},
    {"id": "n05", "agent": "skat",    "effectiveness": 0.95, "positive": 0.90,
     "text": "Не путай веру с данными. Вера — это когда данных нет."},
    {"id": "n06", "agent": "gayka",   "effectiveness": 0.95, "positive": 0.93,
     "text": "Сначала дешёвый тест, потом амбиции. Пять минут против пяти дней."},
    {"id": "n07", "agent": "hipster", "effectiveness": 0.94, "positive": 0.91,
     "text": "Отрицательный результат — тоже результат. Единственная валюта, которая не инфлирует."},
    {"id": "n08", "agent": "hipster", "effectiveness": 0.94, "positive": 0.91,
     "text": "Прежде чем «открыть» — проверь, не закрывал ли ты это месяц назад."},
    {"id": "n09", "agent": "stazhor", "effectiveness": 0.71, "positive": 0.55,
     "text": "А можно я просто проверю? Один разочек?"},
    {"id": "n10", "agent": "shef",    "effectiveness": 0.96, "positive": 0.92,
     "text": "Дороже L0 — только после GO на L0. Бюджет не резиновый, он вообще не резиновый."},
]


def nudge_stats(conn: sqlite3.Connection) -> dict:
    return core.setting(conn, "crew.nudges", {}) or {}


def nudge_weight(nudge: dict, stats: dict) -> float:
    """Вес = приор × поправка по фактам. Без фактов — чистый приор (0.95×0.90)."""
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


# --------------------------------------------------------------------------- AGI-часы

def agi_days_left(config: dict | None = None) -> int:
    try:
        arrival = datetime.strptime(str(cfg("agi_arrival", config)), "%Y-%m-%d").date()
    except ValueError:
        arrival = date(2030, 5, 1)
    return max(0, (arrival - date.today()).days)


# --------------------------------------------------------------------------- рендер сцен

def render_scene(event: str, ctx: dict, rng: random.Random,
                 config: dict | None = None) -> list[dict]:
    blocks = SCENES.get(event)
    if not blocks:
        return []
    limit = int(cfg("max_lines_per_event", config))
    lines: list[dict] = []
    for agent, variants in blocks[:limit]:
        template = rng.choice(variants)
        lines.append({"agent": agent, "text": _fmt(template, ctx), "event": event})
    return lines


def render_dispute(ctx: dict, rng: random.Random) -> list[dict] | None:
    """Спор: атака/защита/арбитраж. Возвращает линии или None (не выпал)."""
    dispute = rng.choice(DISPUTES)
    dispute_id = f"{dispute['id']}-{core.iso()[-8:-6]}{core.iso()[-5:-3]}"
    lines = [{"agent": a, "text": _fmt(t, ctx), "dispute_id": dispute_id,
              "event": "dispute"}
             for a, t in dispute["lines"]]
    lines.append({"agent": "shef", "text": _fmt(dispute["arbiter"], ctx),
                  "dispute_id": dispute_id, "arbiter": True, "event": "dispute"})
    return lines


def compose_message(lines: list[dict]) -> str:
    """Формат чата: «*☣️ Скат:* текст» — одной пачкой, как живой чат."""
    out = []
    for line in lines:
        a = AGENTS.get(line["agent"], {})
        out.append(f"*{a.get('emoji', '💬')} {a.get('name', line['agent'])}:* {line['text']}")
    return "\n".join(out)


# --------------------------------------------------------------------------- бюджет и время

def _today() -> str:
    return date.today().isoformat()


def sent_today(conn: sqlite3.Connection) -> int:
    """Число ОТПРАВЛЕННЫХ пачек (сообщений) за сутки — единица бюджета."""
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


def crew_thread(config: dict | None = None) -> str | None:
    env_key = str(cfg("thread_env", config) or "TELEGRAM_CREW_THREAD_ID")
    thread = os.environ.get(env_key, "").strip()
    return thread or None


def cooldown_left(conn: sqlite3.Connection, event: str, config: dict | None = None) -> float:
    every = COOLDOWNS.get(event, 0)
    if every <= 0:
        return 0.0
    last = core.parse_iso(core.setting(conn, f"crew.last.{event}"))
    if last is None:
        return 0.0
    return max(0.0, every - (core.now() - last).total_seconds())


# --------------------------------------------------------------------------- главный emit

def emit(event: str, ctx: dict | None = None, conn: sqlite3.Connection | None = None,
         config: dict | None = None, rng: random.Random | None = None,
         send: bool | None = None, force: bool = False) -> dict:
    """Сгенерировать сцену события, записать в базу и (если можно) отправить.

    Никогда не бросает исключений наружу: Курилка не имеет права уронить контур.
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
            return result(False, reason="crew выключен в config.yaml")
        left = cooldown_left(conn, event, config)
        if left > 0:
            return result(False, reason=f"cooldown {left:.0f}s")

    if event not in SCENES:
        return result(False, reason=f"нет сцены для события {event!r}")

    # AGI-часы и счётчики доступны любому шаблону
    agi = agi_days_left(config)
    ctx.setdefault("agi", agi)
    ctx.setdefault("agi_txt", plural(agi, "день", "дня", "дней"))
    ctx.setdefault("n", _total_lines(conn) + 1)

    lines = render_scene(event, ctx, rng, config)

    # раз в сутки — специальная сцена «AGI-день», эмитится вместе с первым событием
    if event != "agi_day" and _agi_day_due(conn):
        lines += render_scene("agi_day", ctx, rng, config)
        core.set_setting(conn, "crew.last.agi_day", core.iso())

    # случайный спор («проверка на прочность») на спорных событиях
    if event in DISPUTE_EVENTS and rng.random() < float(cfg("dispute_probability", config)):
        lines += (render_dispute(ctx, rng) or [])

    # случайный «нудж» — заранее продуманная фраза (приоры 95%/90%)
    if rng.random() < float(cfg("nudge_probability", config)):
        nudge = pick_nudge(rng, conn)
        lines.append({"agent": nudge["agent"], "text": nudge["text"],
                      "nudge_id": nudge["id"], "event": "nudge"})
        record_nudge(conn, nudge["id"], won=True)   # применение = сцена состоялась

    if not lines:
        return result(False, reason=f"пустая сцена для события {event!r}")

    # бюджет отправки: топик «Курилки» нужен всегда, остальное — если не force
    can_send = send if send is not None else True
    if crew_thread(config) is None:
        can_send = False
    elif not force:
        if sent_today(conn) >= int(cfg("max_messages_per_day", config)):
            can_send = False
        elif in_quiet_hours(config):
            can_send = False
    text_out = compose_message(lines) if can_send else None

    for line in lines:
        conn.execute(
            "INSERT INTO crew_chat (ts, event, agent, name, text, dispute_id, sent, meta)"
            " VALUES (?,?,?,?,?,?,0,?)",
            (core.iso(), line.get("event", event), line["agent"],
             AGENTS.get(line["agent"], {}).get("name", line["agent"]),
             line["text"], line.get("dispute_id"),
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
    except Exception as exc:  # noqa: BLE001 — Курилка не роняет контур
        try:
            core.append_log("crew.log", f"emit {event} failed: {exc}")
        except OSError:
            pass


# --------------------------------------------------------------------------- чтение

def replay(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    init_db(conn)
    rows = conn.execute(
        "SELECT ts, event, agent, name, text, dispute_id FROM crew_chat "
        "ORDER BY msg_id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in reversed(rows)]


def replay_text(items: list[dict]) -> str:
    if not items:
        return "Курилка молчит. Даже Стажёр."
    out = []
    last_event = None
    for r in items:
        if r["event"] != last_event:
            out.append(f"— 🎭 событие: {r['event']} —")
            last_event = r["event"]
        mark = " ⚔️" if r.get("dispute_id") else ""
        out.append(f"{r['name']}{mark}: {r['text']}")
    return "\n".join(out)


def stats(conn: sqlite3.Connection, config: dict | None = None) -> dict:
    init_db(conn)
    config = config if config is not None else core.load_config()
    by_agent = conn.execute(
        "SELECT agent, COUNT(*) c FROM crew_chat GROUP BY agent ORDER BY c DESC").fetchall()
    disputes = conn.execute(
        "SELECT COUNT(DISTINCT dispute_id) c FROM crew_chat WHERE dispute_id IS NOT NULL"
    ).fetchone()[0]
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
        "agi_days_left": agi_days_left(config),
        "quiet_hours": cfg("quiet_hours", config),
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

    if cmd == "stats":
        data = stats(conn, config)
        text = (f"🎭 Курилка: {data['total_lines']} реплик, споров {data['disputes']}, "
                f"сегодня {data['today_sent']}/{data['max_per_day']} отправок. "
                f"AGI через {data['agi_days_left']} дн. Цена: 0 GPU-ч, 0 токенов.")
        core.emit(data, as_json, text)
        return 0

    if cmd == "test":
        demo_ctx = {"hid": "H-000", "forecast": "12", "dev": "41", "hours": "0.4",
                    "seeds": 3, "passed": 7, "budget": 20, "burn": 3, "free": 6,
                    "level": "L1", "pct": "60", "ratio": "2.0", "mode": "testing",
                    "min": 3, "actual": "-4.9", "n": 1, "agi": agi_days_left(config)}
        res = emit("customer_lead", demo_ctx, conn=conn, config=config, force=True,
                   send=core.flag(argv, "send"))
        lines = res["lines"]
        res2 = emit("verdict_rejected", demo_ctx, conn=conn, config=config, force=True,
                    send=False)
        lines += res2["lines"]
        core.emit({"lines": len(lines), "sent": res["sent"]}, as_json,
                  compose_message(lines))
        return 0

    core.fail(f"неизвестная команда {cmd!r} (emit | replay | stats | test)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
