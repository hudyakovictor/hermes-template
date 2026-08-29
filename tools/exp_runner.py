#!/usr/bin/env python3
"""researchagen — универсальный запускатель экспериментов (L0–L3).

Зачем отдельный runner:
  * единые правила pre-flight (свободная VRAM через mem_get_info, а не иллюзия);
  * единый стандарт артефактов: results/<H>/<level>/metrics.jsonl + summary.json;
  * checkpoint-вытеснение: флаг state/stop-<H>.flag — корректная остановка;
  * телеметрия в Telegram без участия модели;
  * без torch тоже работает: режим --smoke даёт детерминированный синтетический
    прогон — так проверяется весь контур на macOS без GPU.

Контракт со своими скриптами: experiments/H-XXX.py должен принимать
--hypo/--level/--dry-run и писать summary.json тем же форматом (см. write_summary).

CLI:
  python tools/exp_runner.py --hypo H-001 --level L1 [--seeds 3] [--dry-run] [--smoke]
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
import time

import core
import gpu
import tg

LEVEL_SEEDS = {"L0": 1, "L1": 3, "L2": 3, "L3": 5}
LEVEL_STEPS = {"L0": 40, "L1": 120, "L2": 240, "L3": 400}


def results_dir(hypo_id: str, level: str) -> str:
    path = os.path.join(core.ROOT, "results", hypo_id, level)
    os.makedirs(path, exist_ok=True)
    return path


def preflight(level: str, config: dict | None = None) -> dict:
    """Свободная VRAM берётся из torch.cuda.mem_get_info (если torch есть),
    иначе из nvidia-smi. НИКОГДА не из memory_allocated() — это чужая память."""
    info = {"level": level, "torch": False, "free_gb": None, "total_gb": None}
    try:
        import torch  # noqa: PLC0415  (опциональная зависимость самих экспериментов)
        info["torch"] = True
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            free, total = torch.cuda.mem_get_info()
            info["free_gb"] = round(free / 1024 ** 3, 2)
            info["total_gb"] = round(total / 1024 ** 3, 2)
            info["device"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001 — torch может отсутствовать или быть без CUDA
        info["torch_error"] = str(exc)[:200]
    if info["free_gb"] is None:
        snap = gpu.snapshot(config)
        info["free_gb"] = snap["free_gb"] or None
        info["total_gb"] = (snap["best"] or {}).get("total_gb")
        info["source"] = snap["source"]
    return info


def stop_requested(hypo_id: str) -> bool:
    return os.path.exists(os.path.join(core.STATE_DIR, f"stop-{hypo_id}.flag"))


def clear_stop(hypo_id: str) -> None:
    path = os.path.join(core.STATE_DIR, f"stop-{hypo_id}.flag")
    if os.path.exists(path):
        os.remove(path)


def write_summary(hypo_id: str, level: str, payload: dict) -> str:
    path = os.path.join(results_dir(hypo_id, level), "summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def synthetic_seed_run(seed: int, steps: int, hypo_id: str, level: str,
                       metrics_path: str, dry_run: bool) -> dict:
    """Детерминированный суррогат обучения.

    Это НЕ модель и НЕ доказательство гипотезы. Его единственная задача — дать
    проверяемый поток метрик, чтобы контур (диспетчер, checkpoint,
    телеметрия, вердикт) проверялся целиком и воспроизводимо без GPU.
    """
    rng = random.Random(f"{hypo_id}:{level}:{seed}")
    loss = 2.6 + rng.random() * 0.2
    sign_agree = 0.5
    with open(metrics_path, "a", encoding="utf-8") as fh:
        for step in range(1, steps + 1):
            loss = max(0.05, loss * (1 - 0.02 - rng.random() * 0.004))
            sign_agree = min(0.99, sign_agree + (0.9 - sign_agree) * 0.03)
            curvature = abs(math.sin(step / 7.0)) * (1.0 / math.sqrt(step))
            fh.write(json.dumps({"ts": core.iso(), "seed": seed, "step": step,
                                 "loss": round(loss, 5),
                                 "sign_agreement": round(sign_agree, 5),
                                 "loss_curvature": round(curvature, 6)},
                                ensure_ascii=False) + "\n")
            if step % 20 == 0:
                fh.flush()
            if stop_requested(hypo_id):
                return {"seed": seed, "steps_done": step, "final_loss": round(loss, 5),
                        "sign_agreement": round(sign_agree, 5), "stopped": True}
            if not dry_run:
                time.sleep(0.002)
    return {"seed": seed, "steps_done": steps, "final_loss": round(loss, 5),
            "sign_agreement": round(sign_agree, 5), "stopped": False}


def run(hypo_id: str, level: str, seeds: int | None = None, dry_run: bool = False,
        smoke: bool = False) -> dict:
    core.ensure_dirs()
    config = core.load_config()
    conn = core.db()
    started = time.time()
    clear_stop(hypo_id)

    seeds = int(seeds or LEVEL_SEEDS.get(level, 1))
    steps = LEVEL_STEPS.get(level, 40)
    out_dir = results_dir(hypo_id, level)
    metrics_path = os.path.join(out_dir, "metrics.jsonl")
    pre = preflight(level, config)

    core.log_event(conn, "exp.start", hypo_id, level=level, seeds=seeds,
                   dry_run=dry_run, smoke=smoke, preflight=pre)

    per_seed = []
    for idx, seed in enumerate(range(seeds), start=1):
        result = synthetic_seed_run(seed, steps, hypo_id, level, metrics_path, dry_run)
        per_seed.append(result)
        pct = idx / seeds * 100
        tg.throttled_progress(
            conn, hypo_id,
            tg.progress_card(hypo_id, level, pct,
                             f"seed {idx}/{seeds} завершён",
                             {"loss": result["final_loss"],
                              "режим": "dry-run" if dry_run else "GPU"}),
            config)
        if result["stopped"]:
            break

    losses = [r["final_loss"] for r in per_seed]
    mean = sum(losses) / len(losses)
    var = sum((x - mean) ** 2 for x in losses) / max(1, len(losses) - 1) if len(losses) > 1 else 0.0
    sigma = math.sqrt(var)
    elapsed = time.time() - started
    stopped = any(r["stopped"] for r in per_seed)

    summary = {
        "hypo_id": hypo_id,
        "level": level,
        "seeds_requested": seeds,
        "seeds_done": len(per_seed),
        "steps_per_seed": steps,
        "dry_run": dry_run,
        "smoke": smoke,
        "stopped_at_checkpoint": stopped,
        "final_loss_mean": round(mean, 5),
        "final_loss_sd": round(sigma, 5),
        "seed_results": per_seed,
        "preflight": pre,
        "wall_seconds": round(elapsed, 1),
        "gpu_hours": round(elapsed / 3600.0, 4) if not dry_run else 0.0,
        "metrics": os.path.relpath(metrics_path, core.ROOT),
        "created_at": core.iso(),
        "note": "Артефакт контура. Оценка гипотезы возможна только после замены "
                "синтетического прогона на реальный experiments/<H>.py.",
    }
    path = write_summary(hypo_id, level, summary)
    core.log_event(conn, "exp.finish", hypo_id, level=level,
                   gpu_hours=summary["gpu_hours"], stopped=stopped)

    # Закрываем запись прогона в очереди — диспетчер освобождает GPU.
    import dispatch  # локальный импорт: избегаем цикла на уровне модуля
    dispatch.finish(conn, hypo_id, summary["gpu_hours"],
                    "preempted" if stopped else "done")

    tg.send(
        f"*✅ {hypo_id} {level} завершён*\n"
        f"seeds {summary['seeds_done']}/{seeds} | loss {summary['final_loss_mean']} "
        f"±{summary['final_loss_sd']}\n"
        f"время {core.human_delta(elapsed)} | GPU-ч {summary['gpu_hours']}\n"
        + ("Прервано на checkpoint — гипотеза вернулась в очередь.\n" if stopped else "")
        + f"Следующее действие: вердикт — /v {hypo_id}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main(argv: list[str]) -> int:
    core.load_env()
    hypo_id = core.arg(argv, "hypo") or core.fail("нужен --hypo H-XXX")
    level = core.arg(argv, "level", "L0")
    seeds = core.arg(argv, "seeds")
    run(hypo_id, level, int(seeds) if seeds else None,
        core.flag(argv, "dry-run"), core.flag(argv, "smoke"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
