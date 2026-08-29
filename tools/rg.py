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
  python tools/rg.py crew emit|replay|review|stats|mute|test
"""

from __future__ import annotations

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
        "crew": lambda: crew.main([argv[0]] + argv[2:]),
    }
    if cmd in ("help", "-h", "--help"):
        print(USAGE)
        return 0
    handler = routes.get(cmd)
    if handler is None:
        print(USAGE)
        core.fail(f"неизвестная команда {cmd!r}")
        return 2
    return handler()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
