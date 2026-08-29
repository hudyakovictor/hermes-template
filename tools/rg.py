#!/usr/bin/env python3
"""researchagen — единая точка входа для всего контура.

Причина существования: скиллы и cron-задания должны вызывать одну короткую команду,
а не знать внутреннюю раскладку файлов.

  python tools/rg.py status | queue | next | tick | digest | weekly | doctor | calib
  python tools/rg.py bottom run --iterations 1
  python tools/rg.py add "текст идеи" [--signals 3 --hours 4 ...]
  python tools/rg.py launch H-003 [--level L1]
  python tools/rg.py verdict H-003 --kind confirmed --actual 11.4 ...
  python tools/rg.py pause | resume | approve H-007
  python tools/rg.py governor plan | mode | reserve | report
  python tools/rg.py benchmark --concurrencies 1,2
  python tools/rg.py panel [--send]               # панель: стадии + пульт
  python tools/rg.py aichat [--n 30]              # история чата экипажа
  python tools/rg.py crew emit|replay|review|stats|mute|test|bet|bets
  python tools/rg.py inbox add|list|take|drop     # сырьё от человека
  python tools/rg.py idea "текст"                 # идея от человека → разбор экипажем
  python tools/rg.py triage IN-001 [--signals 3]  # разбор: очередь или лог неэффективных
  python tools/rg.py ideas [--verdict rejected]   # очередь идей и лог отклонённых
  python tools/rg.py hygiene                      # ночная уборка состояния
  python tools/rg.py priors search "запрос"       # prior-art по 6 источникам
  python tools/rg.py audit                        # 30 анализов функционала
"""

from __future__ import annotations

import shutil
import difflib
import importlib
import sys

import bottom_detection_cli
import calib
import core
import crew
import dispatch
import governor
import governor_benchmark
import hypo
import queue as q
import report
import selfcheck
import verdict as v

USAGE = __doc__




def boot_report(rest: list[str]) -> int:
    """Старт контура одним вызовом: факты, самопроверка, что запустить.

    Вызывается человеком (/start, /boot) и на первом сообщении сессии.
    Детерминирован и бесплатен для модели: только чтение состояния и
    подсказки. Автономию обеспечивают cron-задания и gateway, не этот отчёт.
    """
    as_json = core.wants_json(["rg.py", "boot"] + rest)
    conn = core.db()
    config = core.load_config()
    st = report.status(conn, config)
    doctor = selfcheck.run_all()
    cron_ok = shutil.which("hermes") is not None
    plat, debug = core.platform_mode(config)
    paused = st.get("paused")
    data = {
        "ok": (not paused) and bool(doctor.get("ok")),
        "paused": bool(paused),
        "doctor": {"ok": doctor.get("ok"), "fails": doctor.get("fails"),
                   "warns": doctor.get("warns")},
        "autonomy": {
            "cron_cli": cron_ok,
            "cron_note": ("hermes найден: диспетчер (*/2 мин) и research-loop (*/25 мин) "
                          "должны стоять в `hermes cron list`; если нет — перезапусти "
                          "install.sh (блок cron)" if cron_ok else
                          "hermes не в PATH: cron не зарегистрирован, контур не автономен. "
                          "Запусти установщик или добавь задания из cron/ вручную"),
            "gateway_note": "управление в Telegram живёт только при запущенном "
                            "`researchagen gateway start`",
            "platform_note": (
                "Windows: hermes cron нет — задания ставятся в планировщик задач, "
                "готовые команды в docs/OPERATIONS.md (раздел «Автономия на Windows»)"
                if plat == "windows" else
                "macOS: контур отладочный, эксперименты идут как dry-run; GPU-обучение — "
                "на Windows-узле" if plat == "macos" else
                ""),
        },
        "status": st,
        "first_actions": (
            ["/resume — вернуть автозапуск"] if paused else []
        ) + [
            "python tools/rg.py tick — один тик диспетчера прямо сейчас",
            "python tools/rg.py status — полная картина",
        ],
    }
    if as_json:
        core.emit(data, True)
        return 0
    # человек читает текст: короткий стартовый отчёт, а не одна строка
    q = st.get("planned") or []
    run = st.get("running") or []
    ver = (st.get("calibration") or {}).get("verdicts")
    doc = ("чисто" if doctor.get("ok")
           else "провалов %s, предупреждений %s — детали: python tools/rg.py doctor"
           % (doctor.get("fails"), doctor.get("warns")))
    print("boot: %s" % ("пауза — /resume вернёт автозапуск" if paused else "контур активен"))
    print("  доктор: %s" % doc)
    print("  очередь: %d гипотез в плане, на GPU: %d%s"
          % (len(q), len(run), (", вердиктов закрыто: %s" % ver) if ver else ""))
    gpu = (st.get("gpu") or {})
    if not gpu.get("available"):
        print("  GPU: недоступен (нужно %s ГБ) — пульт, очередь и экипаж работают"
              % gpu.get("required_gb"))
    print("  автономия: %s" % data["autonomy"]["cron_note"])
    if data["autonomy"].get("platform_note"):
        print("  платформа: %s" % data["autonomy"]["platform_note"])
    for a in data["first_actions"]:
        print("  → %s" % a)
    return 0


def main(argv: list[str]) -> int:
    core.load_env()
    cmd = argv[1] if len(argv) > 1 else "status"
    rest = [argv[0]] + argv[2:]

    routes = {
        "status": lambda: report.main([argv[0], "status"] + argv[2:]),
        "panel": lambda: report.main([argv[0], "panel"] + argv[2:]),
        "digest": lambda: report.main([argv[0], "digest"] + argv[2:]),
        "weekly": lambda: report.main([argv[0], "weekly"] + argv[2:]),
        "patent": lambda: report.main([argv[0], "patent"] + argv[2:]),
        "queue": lambda: q.main([argv[0], "list"] + argv[2:]),
        "next": lambda: q.main([argv[0], "next"] + argv[2:]),
        "add": lambda: hypo.main([argv[0], "new"] + argv[2:]),
        "check": lambda: hypo.main([argv[0], "check"] + argv[2:]),
        "kill": lambda: hypo.main([argv[0], "kill"] + argv[2:]),
        "tick": lambda: dispatch.main([argv[0], "tick"] + argv[2:]),
        "launch": lambda: dispatch.main([argv[0], "launch"] + argv[2:]),
        "finish": lambda: dispatch.main([argv[0], "finish"] + argv[2:]),
        "preempt": lambda: dispatch.main([argv[0], "preempt"] + argv[2:]),
        "running": lambda: dispatch.main([argv[0], "running"] + argv[2:]),
        "pause": lambda: dispatch.main([argv[0], "pause"] + argv[2:]),
        "resume": lambda: dispatch.main([argv[0], "resume"] + argv[2:]),
        "approve": lambda: dispatch.main([argv[0], "approve"] + argv[2:]),
        "governor": lambda: governor.main([argv[0]] + argv[2:]),
        "benchmark": lambda: governor_benchmark.main([argv[0], "run"] + argv[2:]),
        "verdict":  lambda: v.main([argv[0], "record"] + argv[2:]),
        "verdicts": lambda: v.main([argv[0], "list"] + argv[2:]),
        "calib": lambda: calib.main([argv[0], "report"] + argv[2:]),
        "recalib": lambda: calib.main([argv[0], "apply"] + argv[2:]),
        "doctor": lambda: selfcheck.main([argv[0], "all"] + argv[2:]),
        "bottom": lambda: bottom_detection_cli.main([argv[0]] + argv[2:]),
        "aichat": lambda: crew.main([argv[0], "replay"] + argv[2:]),
        "chat": lambda: crew.main([argv[0], "replay"] + argv[2:]),
        "gossip": lambda: crew.main([argv[0], "replay"] + argv[2:]),
        "bet": lambda: crew.main([argv[0], "bet"] + argv[2:]),
        "bets": lambda: crew.main([argv[0], "bets"] + argv[2:]),
        "crew": lambda: crew.main([argv[0]] + argv[2:]),
        "inbox": lambda: importlib.import_module("inbox").main(
            [argv[0]] + argv[2:]),
        "idea": lambda: importlib.import_module("ideas").main(
            [argv[0], "submit"] + argv[2:]),
        "ideas": lambda: importlib.import_module("ideas").main(
            [argv[0], "log"] + argv[2:]),
        "triage": lambda: importlib.import_module("ideas").main(
            [argv[0], "triage"] + argv[2:]),
        "hygiene": lambda: importlib.import_module("hygiene").main(
            [argv[0], "run"] + argv[2:]),
        "audit": lambda: importlib.import_module("audit").main(
            [argv[0], "run"] + argv[2:]),
        "priors": lambda: importlib.import_module("priors").main(
            [argv[0]] + argv[2:]),
        "board": lambda: importlib.import_module("board").main(
            [argv[0], "show"] + argv[2:]),
        "boot": lambda: boot_report(argv[2:]),
    }
    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    handler = routes.get(cmd)
    if handler is None:
        close = difflib.get_close_matches(cmd, routes, n=1, cutoff=0.6)
        hint = f" Возможно, вы имели в виду `{close[0]}`?" if close else ""
        print(USAGE)
        core.fail(f"неизвестная команда {cmd!r}.{hint}")
        return 2
    return handler()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
