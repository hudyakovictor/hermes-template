#!/usr/bin/env python3
"""researchagen — гейт по GPU.

Почему не `total - torch.cuda.memory_allocated()`: это память, выделенная **текущим**
процессом. Локальная Qwen3-27B в Ollama занимает 20+ GB из другого процесса, и такой
счёт дал бы «свободно 30 GB» на полностью занятой карте. Истина — nvidia-smi
(`memory.free`), а в Python — `torch.cuda.mem_get_info()`.

CLI:
  python tools/gpu.py show [--json]
  python tools/gpu.py check --need-gb 20 [--json]   # exit 0 = можно запускать
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import core

QUERY = "name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu"


WIN_NVIDIA_SMI = (
    r"C:\\Windows\\System32\\nvidia-smi.exe",
    r"C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe",
)


def read_nvidia_smi() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe and os.name == "nt":
        # на Windows служба драйвера не всегда добавляет nvidia-smi в PATH
        exe = next((p for p in WIN_NVIDIA_SMI if os.path.exists(p)), None)
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    gpus = []
    for idx, line in enumerate(out.stdout.strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                "index": idx,
                "name": parts[0],
                "total_gb": round(float(parts[1]) / 1024, 2),
                "used_gb": round(float(parts[2]) / 1024, 2),
                "free_gb": round(float(parts[3]) / 1024, 2),
                "util_pct": float(parts[4]),
                "temp_c": float(parts[5]),
            })
        except ValueError:
            continue
    return gpus


def snapshot(config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    plat, debug = core.platform_mode(config)
    gpus = read_nvidia_smi()
    need = float(core.cfg("researchagen.limits.gpu_free_gb_required", 20, config))
    if gpus:
        best = max(gpus, key=lambda g: g["free_gb"])
        return {"platform": plat, "debug": debug, "available": True,
                "gpus": gpus, "best": best, "free_gb": best["free_gb"],
                "required_gb": need, "source": "nvidia-smi"}
    # Нет nvidia-smi. На macOS это штатно: отладочный контур, эксперименты dry-run.
    return {"platform": plat, "debug": debug, "available": False, "gpus": [],
            "best": None, "free_gb": 0.0, "required_gb": need,
            "source": "none",
            "note": "nvidia-smi не найден" + (" — debug-режим, запуски только dry-run"
                                                if debug else "")}


def can_launch(need_gb: float | None = None, config: dict | None = None) -> tuple[bool, str, dict]:
    snap = snapshot(config)
    need = float(need_gb if need_gb is not None else snap["required_gb"])
    if snap["debug"] and not snap["available"]:
        return True, "debug-режим (macOS): GPU-гейт имитирован, запуск пойдёт как dry-run", snap
    if not snap["available"]:
        return False, "GPU не обнаружен (nvidia-smi недоступен), а режим production", snap
    if snap["free_gb"] + 1e-9 < need:
        return False, (f"свободно {snap['free_gb']:.1f} GB из нужных {need:.1f} GB — ждём "
                       f"(занято локальной моделью или другим прогоном)"), snap
    return True, f"свободно {snap['free_gb']:.1f} GB ≥ {need:.1f} GB", snap


def main(argv: list[str]) -> int:
    if argv[1:2] and argv[1] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd == "show" or cmd == "snapshot":
        snap = snapshot()
        if snap["gpus"]:
            text = core.table(
                [[g["index"], g["name"], f"{g['free_gb']:.1f}", f"{g['used_gb']:.1f}",
                  f"{g['total_gb']:.1f}", f"{g['util_pct']:.0f}%", f"{g['temp_c']:.0f}°C"]
                 for g in snap["gpus"]],
                ["#", "GPU", "свободно GB", "занято GB", "всего GB", "util", "temp"])
        else:
            text = f"GPU недоступен. {snap.get('note', '')}"
        core.emit(snap, as_json, text)
        return 0
    if cmd == "check":
        need = core.arg(argv, "need-gb")
        ok, why, snap = can_launch(float(need) if need else None)
        core.emit({"ok": ok, "reason": why, "snapshot": snap}, as_json,
                  ("МОЖНО: " if ok else "НЕЛЬЗЯ: ") + why)
        return 0 if ok else 1
    core.fail(f"неизвестная команда {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
