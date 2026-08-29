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
                     "kill_checks", "forecast")

KILL_CHECKS = (
    "Простое объяснение: lr / scheduler / init / batch / регуляризация / метрика не объясняют эффект",
    "Публикационный gap: прямого аналога нет (arXiv + Semantic Scholar/OpenAlex проверены)",
    "Утечка данных / перекрытие train-test исключены",
    "Эффект не сводится к шуму seeds (есть оценка разброса между seeds)",
    "Есть контрольное условие, при котором эффект ОБЯЗАН исчезнуть",
    "Метрика читаема дешёво (не требует полного обучения для измерения)",
    "PASS/FAIL сформулированы числами и зафиксированы ДО запуска",
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

# --- 6. Прогноз эффекта в % (фиксируется ДО запуска — основа калибровки).
forecast: {forecast}

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
        standard=kw.get("standard", 0.4), money=kw.get("money", 0.4),
        signals=kw.get("signals", 0), early=kw.get("early_pct", 10.0),
        hours=kw.get("est_hours", 4.0), decidability=kw.get("decidability", 0.5),
        novelty=kw.get("novelty", 0.5), kill_checks=checks,
    )
    path = card_path(hid)
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
    }
    fields: dict = {
        "title": core.arg(argv, "title"),
        "source": core.arg(argv, "source", "dr"),
    }
    for key, (cli_name, cast) in numeric.items():
        raw = core.arg(argv, cli_name)
        if raw in (None, ""):
            fields[key] = None if key == "forecast" else raw
            continue
        try:
            fields[key] = cast(raw)
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
    row = q.add(conn, title, **values)
    path = write_card(row["id"], title, **values)
    q.update_fields(conn, row["id"], card_path=path)
    return dict(conn.execute("SELECT * FROM hypotheses WHERE id=?", (row["id"],)).fetchone())


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
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "help"
    conn = core.db()

    if cmd == "new":
        if len(argv) < 3 or argv[2].startswith("--"):
            core.fail("нужно название")
        title = argv[2]
        kw = fields_from_args(argv)
        kw.pop("title", None)      # title идёт позиционно; в kw его быть не должно
        row = q.add(conn, title, **kw)
        path = write_card(row["id"], title, **kw)
        q.update_fields(conn, row["id"], card_path=path)
        crew.safe_emit("customer_lead" if kw.get("source") == "human" else "hypo_new",
                       conn=conn, ctx={"hid": row["id"], "forecast": kw.get("forecast"),
                                       "title": title})
        core.emit({"id": row["id"], "card": path}, as_json,
                  f"Создана {row['id']}: {os.path.relpath(path, core.ROOT)}\n"
                  f"Заполни секции и прогони kill-stage: python tools/hypo.py check {row['id']}")
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
