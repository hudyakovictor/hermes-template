#!/usr/bin/env python3
"""researchagen — карточки гипотез и kill-stage.

Карточка = `hypotheses/H-XXX.yaml`. Файл — человекочитаемый источник смысла,
SQLite — источник порядка. `check` — гейт перед запуском (и gate для `/goal`).

CLI:
  python tools/hypo.py new "название" [--signals 3] [--hours 4] [--forecast 12] ...
  python tools/hypo.py check H-001 [--json]     # exit 0 = запуск разрешён
  python tools/hypo.py kill H-001 --why "найдена публикация 2025.11" --lesson "проверять gap по трём формулировкам"
  python tools/hypo.py card H-001            # печать карточки
"""

from __future__ import annotations

import os
import re
import sys

import core
import crew
import queue as q

REQUIRED_SECTIONS = ("signal_chain", "mechanism", "why_missed", "minimal_test",
                     "pass_fail", "scale_path", "impact", "falsification",
                     "kill_checks", "forecast", "base_rate", "industry_usecase")

KILL_CHECKS = (
    "Простое объяснение: lr / scheduler / init / batch / регуляризация / метрика не объясняют эффект",
    "Публикационный gap: прямого аналога нет (arXiv + Semantic Scholar/OpenAlex проверены)",
    "Утечка данных / перекрытие train-test исключены",
    "Эффект не сводится к шуму seeds (есть оценка разброса между seeds)",
    "Есть контрольное условие, при котором эффект ОБЯЗАН исчезнуть",
    "Метрика читаема дешёво (не требует полного обучения для измерения)",
    "PASS/FAIL сформулированы числами и зафиксированы ДО запуска",
    "Кому продадим: назван покупатель/лицензиат или измеримый сценарий экономии",
)

TEMPLATE = """# {hid} — {title}
id: {hid}
title: "{title}"
status: queued          # queued|running|paused_checkpoint|confirmed|partial|rejected|killed
level: L0
source: {source}
created_at: "{created}"

# --- 1. Сигналы: минимум 3 НЕЗАВИСИМЫХ. Зависимые сигналы — это один сигнал.
signal_chain:
  - id: A
    claim: ""
    source: ""        # arXiv id / DOI / ссылка / свой прогон
    independent_of: [] # почему не дубль B и C
  - id: B
    claim: ""
    source: ""
    independent_of: []
  - id: C
    claim: ""
    source: ""
    independent_of: []

# --- 2. Механизм: причинная цепочка, а не корреляция.
mechanism: |

# --- 3. Почему не заметили раньше (без этого гипотеза обычно уже опубликована).
why_missed: |

# --- 4. Минимальный тест (L0, < 5 мин).
minimal_test:
  script: experiments/{hid}.py
  dataset: ""
  model: ""
  runtime_minutes: 5
  seeds: [0, 1, 2]

# --- 5. PASS/FAIL — числами, до запуска, не менять после факта.
pass_fail:
  metric: ""
  pass_if: ""
  fail_if: ""

# --- 6. Прогноз: коридор + вероятность воспроизведения (фиксируется ДО запуска).
forecast: {forecast}
forecast_low: {forecast_low}     # пессимистичная граница коридора
forecast_high: {forecast_high}   # оптимистичная граница коридора
p_repro: {p_repro}               # вероятность, что эффект воспроизведётся (0..1)

# --- 6b. Base rate: доля похожих случаев в базе/литературе, где эффект был.
base_rate: {base_rate}

# --- 6c. Индустриальный сценарий: что изменит и у кого (конкретный use-case).
industry_usecase: |
  # что меняет: ...
  # у кого: ...
  # как измерят экономию: ...

# --- 7. Путь к масштабу.
scale_path: |
  L0 -> L1 (3 seeds, <1ч) -> L2 (абляции, 2-8ч) -> L3 (5 seeds, 2 архитектуры)

# --- 8. Эффект: технологический и рыночный + патентная форма.
impact:
  compute_saving: ""
  becomes_standard: {standard}
  commercial: {money}
  patent_claim: ""

# --- 9. Как это можно опровергнуть (если нечем — это не гипотеза).
falsification: |

# --- 10. Kill-stage: все галочки обязаны быть true до выдачи GPU.
kill_checks:
{kill_checks}

score:
  signals: {signals}
  early_pct: {early}
  est_hours: {hours}
  decidability: {decidability}
  novelty: {novelty}
"""


def card_path(hid: str) -> str:
    return os.path.join(core.HYPO_DIR, f"{hid}.yaml")


def write_card(hid: str, title: str, **kw) -> str:
    core.ensure_dirs()
    checks = "\n".join(f"  - check: \"{c}\"\n    passed: false\n    evidence: \"\""
                       for c in KILL_CHECKS)
    body = TEMPLATE.format(
        hid=hid, title=title.replace('"', "'"), created=core.iso(),
        source=kw.get("source", "dr"), forecast=kw.get("forecast", "null"),
        forecast_low=kw.get("forecast_low", "null"),
        forecast_high=kw.get("forecast_high", "null"),
        p_repro=kw.get("p_repro", "null"), base_rate=kw.get("base_rate", "null"),
        standard=kw.get("standard", 0.4), money=kw.get("money", 0.4),
        signals=kw.get("signals", 0), early=kw.get("early_pct", 10.0),
        hours=kw.get("est_hours", 4.0), decidability=kw.get("decidability", 0.5),
        novelty=kw.get("novelty", 0.5), kill_checks=checks,
    )
    path = core.safe_path(os.path.relpath(card_path(hid), core.ROOT), "карточка")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def fields_from_args(argv: list[str]) -> dict:
    """Extract the common hypothesis fields used by CLI and inbox promotion.

    Числовые поля валидируются здесь: текст в --forecast не должен ронять CLI
    трейсеболом — он получает внятный отказ и код ошибки.
    """
    numeric = {
        "signals": ("signals", int),
        "novelty": ("novelty", float),
        "early_pct": ("early", float),
        "standard": ("standard", float),
        "money": ("money", float),
        "decidability": ("decidability", float),
        "est_hours": ("hours", float),
        "forecast": ("forecast", float),
        "forecast_low": ("forecast-low", float),
        "forecast_high": ("forecast-high", float),
        "p_repro": ("p-repro", float),
        "base_rate": ("base-rate", float),
        "demand_signals": ("demand", int),
    }
    fields: dict = {
        "title": core.arg(argv, "title"),
        "source": core.arg(argv, "source", "dr"),
        "buyer": core.arg(argv, "buyer"),
        "industry_usecase": core.arg(argv, "usecase"),
    }
    for key, (cli_name, cast) in numeric.items():
        raw = core.arg(argv, cli_name)
        if raw in (None, ""):
            fields[key] = None if key == "forecast" else raw
            continue
        try:
            num = core.to_number(raw, f"--{cli_name}")
            fields[key] = int(num) if cast is int else num
        except (TypeError, ValueError):
            core.fail(f"--{cli_name} должен быть числом, получено {raw!r}")
        # дефолты, когда флаг не передан вовсе
    defaults = {"signals": 0, "novelty": 0.5, "early_pct": 10.0, "standard": 0.4,
                "money": 0.4, "decidability": 0.5, "est_hours": 4.0}
    for key, value in defaults.items():
        if fields.get(key) is None:
            fields[key] = value
    return fields


def create(conn, title: str, fields: dict) -> dict:
    """Create a queued hypothesis and its card from an inbox lead."""
    values = dict(fields)
    values.pop("title", None)
    # #8: автоштраф прогноза при слабой evidence — сигналов < 3.
    # Система не верит смелым цифрам на тонком основании: прогноз срезается
    # на 20% (честнее, чем молча завышать ожидания). Возвращается в поле
    # penalty_note для показа человеку при постановке.
    penalty_note = ""
    if int(values.get("signals") or 0) < 3 and values.get("forecast") not in (None, ""):
        original = float(values["forecast"])
        values["forecast"] = round(original * 0.8, 2)
        penalty_note = (f"Автоштраф evidence: сигналов {values['signals']} < 3 — "
                        f"прогноз скорректирован {original:g}% → {values['forecast']:g}%.")
    row = q.add(conn, title, **values)
    path = write_card(row["id"], title, **values)
    q.update_fields(conn, row["id"], card_path=path)
    out = dict(conn.execute("SELECT * FROM hypotheses WHERE id=?",
                            (row["id"],)).fetchone())
    out["penalty_note"] = penalty_note
    return out


def _section_filled(text: str, name: str) -> bool:
    """Секция считается заполненной, если в ней есть непустое содержание."""
    pattern = re.compile(rf"^{re.escape(name)}:(.*)$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return False
    inline = match.group(1).strip()
    if inline and inline not in ("|", ">", "null", '""'):
        return True
    # блочный скаляр или вложенный маппинг: смотрим следующие строки
    tail = text[match.end():]
    for line in tail.splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t", "-")):
            return False
        payload = line.strip().lstrip("-").strip()
        if payload.endswith(":") or payload in ('""', "null", "[]"):
            continue
        if ":" in payload:
            value = payload.split(":", 1)[1].strip()
            if value and value not in ('""', "null", "[]", "|", ">"):
                return True
            continue
        if payload:
            return True
    return False


def check(hid: str, conn) -> dict:
    """Гейт перед запуском. Пригоден как `/goal gate add`."""
    problems: list[str] = []
    row = conn.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
    if row is None:
        return {"ok": False, "id": hid, "problems": [f"{hid} нет в очереди"]}

    path = row["card_path"] or card_path(hid)
    if not os.path.exists(path):
        problems.append(f"нет карточки {os.path.relpath(path, core.ROOT)}")
        text = ""
    else:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for section in REQUIRED_SECTIONS:
            if not _section_filled(text, section):
                problems.append(f"секция не заполнена: {section}")

    if int(row["signals"]) < 3:
        problems.append(f"сигналов {row['signals']} < 3 — гипотеза слабая по MISSION.md")
    if row["forecast"] is None:
        problems.append("прогноз эффекта не зафиксирован — нечему будет сравнивать вердикт")

    total_checks = len(KILL_CHECKS)
    passed = text.count("passed: true")
    if passed < total_checks:
        problems.append(f"kill-stage: {passed}/{total_checks} галочек с доказательством")
    conn.execute("UPDATE hypotheses SET kill_checks_passed=?, card_path=?, updated_at=? WHERE id=?",
                 (passed, path, core.iso(), hid))
    conn.commit()

    return {"ok": not problems, "id": hid, "kill_checks_passed": passed,
            "kill_checks_total": total_checks, "problems": problems, "card": path}


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "help"
    conn = core.db()

    if cmd == "new":
        if len(argv) < 3 or argv[2].startswith("--"):
            core.fail("нужно название")
        title = argv[2]
        # ранний дубль-гейт: «это уже рассматривали» — до создания карточки
        if not core.flag(argv, "force"):
            import ideas as _ideas
            dups = _ideas.find_duplicates(conn, title)
            if dups:
                d = dups[0]
                core.fail(
                    f"дубль {d['kind']} {d['id']} ({d['verdict']}, "
                    f"похожесть {d['score']:.0%}) — уже рассматривали. "
                    f"Новое — с --force и новыми данными")
        kw = fields_from_args(argv)
        kw.pop("title", None)      # title идёт позиционно; в kw его быть не должно

        row = create(conn, title, kw)      # create сам ставит автоштраф #8
        path = row["card_path"]
        penalty_note = row.get("penalty_note", "")
        config = core.load_config()
        ppi = q.ppi(dict(row), config)
        bets = crew.place_bets(conn, row["id"], row["p_repro"])
        crew.safe_emit("customer_lead" if kw.get("source") == "human" else "hypo_new",
                       conn=conn, ctx={"hid": row["id"],
                                       "forecast": "—" if row["forecast"] is None
                                       else f"{row['forecast']:g}%",
                                       "ppi": f"{ppi:.2f}",
                                       "hours": f"{float(row['est_hours']):g}",
                                       "signals": kw.get("signals", 0),
                                       "bets_line": crew._bets_line(conn, row["id"]),
                                       "title": title})
        if bets:
            penalty_note += f"\nСтавки: {len(bets)} агентов зафиксировали прогноз до вердикта."
        # #4: два прогноза — эффект и вероятность: ожидаемая величина сразу видна
        if row["p_repro"] is not None and row["forecast"] is not None:
            ev = float(row["p_repro"]) * float(row["forecast"])
            penalty_note += (f"\np_repro×эффект = {float(row['p_repro']):.2f} × "
                             f"{float(row['forecast']):g}% = {ev:.1f}% ожидаемо.")
        penalty_note = penalty_note.lstrip("\n")
        # Якорь калибровки: агент видит свой систематический сдвиг ДО прогноза
        cal_row = conn.execute(
            "SELECT COUNT(*) n, AVG(deviation) bias FROM verdicts "
            "WHERE deviation IS NOT NULL").fetchone()
        anchor = ""
        if cal_row["n"] >= 5 and cal_row["bias"] is not None:
            bias = float(cal_row["bias"])
            sign = "+" if bias >= 0 else ""
            anchor = (f"\nЯкорь калибровки ({cal_row['n']} вердиктов): сдвиг {sign}{bias:.0f}%. "
                      + ("Прогнозы в среднем завышены — ставь консервативнее." if bias > 0
                         else "Прогнозы в среднем занижены — но не увлекайся."))
        core.emit({"id": row["id"], "card": path}, as_json,
                  f"Создана {row['id']}: {os.path.relpath(path, core.ROOT)}\n"
                  f"Заполни секции и прогони kill-stage: python tools/hypo.py check {row['id']}"
                  + anchor + ("\n" + penalty_note if penalty_note else ""))
        return 0

    if cmd == "check":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        result = check(hid, conn)
        if "не найдена" not in " ".join(result["problems"]) and not result["ok"] and \
                any("kill-stage" in p for p in result["problems"]):
            est = conn.execute("SELECT est_hours, forecast FROM hypotheses WHERE id=?",
                               (hid,)).fetchone()
            crew.safe_emit("gate_fail", conn=conn, ctx={
                "hid": hid, "passed": result["kill_checks_passed"],
                "total": result["kill_checks_total"],
                "hours": f"{float(est['est_hours']):.0f}" if est else "4",
                "forecast": est["forecast"] if est else "—"})
        if result["ok"]:
            crew.safe_emit("gate_pass", conn=conn, ctx={"hid": hid})
            text = f"{hid}: гейт пройден — запуск разрешён " \
                   f"({result['kill_checks_passed']}/{result['kill_checks_total']} kill-checks)"
        else:
            text = f"{hid}: ЗАПУСК ЗАПРЕЩЁН\n" + "\n".join(f"  • {p}" for p in result["problems"])
        core.emit(result, as_json, text)
        return 0 if result["ok"] else 1

    if cmd == "kill":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        reason = core.arg(argv, "why") or core.arg(argv, "reason", "")
        lesson = core.arg(argv, "lesson", "")
        if not reason.strip() or not lesson.strip():
            core.fail("нужны конкретные причина --why и переносимый урок --lesson")
        q.set_status(conn, hid, "killed")
        q.update_fields(conn, hid, notes=f"killed: {reason}")
        core.log_event(conn, "hypo.killed", hid, reason=reason, lesson=lesson)
        # снята до GPU — сигналы не опровергнуты: остаются в банке для будущих
        # гипотез (двусторонняя память: не потерять сырье вместе с идеей)
        try:
            import ideas as _ideas
            card = ""
            if os.path.exists(card_path(hid)):
                with open(card_path(hid), encoding="utf-8") as fh:
                    card = fh.read()
            row = conn.execute("SELECT title FROM hypotheses WHERE id=?",
                               (hid,)).fetchone()
            _ideas.bank_save(conn, hid, (row or {"title": hid})["title"] or "",
                             card, "reusable",
                             f"снята до GPU: {reason}")
        except Exception as exc:  # noqa: BLE001 — банк не роняет kill
            core.append_log("signal_bank.log", f"kill {hid}: {exc}")
        with open(os.path.join(core.MEMORY_DIR, "killed.md"), "a", encoding="utf-8") as fh:
            fh.write(f"- {core.iso()} {hid}: {reason} | урок: {lesson}\n")
        crew.safe_emit("kill", conn=conn, ctx={"hid": hid, "reason": reason})
        core.emit({"ok": True, "id": hid, "reason": reason, "lesson": lesson}, as_json,
                  f"{hid} снята до эксперимента — {reason} (урок записан в memory/killed.md)")
        return 0

    if cmd == "card":
        hid = argv[2] if len(argv) > 2 else core.fail("нужен id")
        path = card_path(hid)
        if not os.path.exists(path):
            core.fail(f"карточки нет: {path}")
        with open(path, "r", encoding="utf-8") as fh:
            print(fh.read())
        return 0

    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
