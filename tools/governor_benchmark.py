#!/usr/bin/env python3
"""Live calibration for a local OpenAI-compatible Qwen endpoint.

This is intentionally separate from the deterministic architecture study:
``governor_benchmark.py`` measures the actual endpoint/GPU and does not claim
that a simulated policy is a throughput result.  It uses only urllib and the
stdlib thread pool.

Run only while no scientific experiment is active.  The benchmark takes the
exclusive governor experiment lease, so cooperative research workers and the
research cron are paused for its duration.

Example:
  python tools/governor_benchmark.py run --concurrencies 1,2 \
      --requests-per-level 3 --output reports/qwen-governor.json
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import core
import governor
import gpu


def _float_list(raw: str) -> list[int]:
    values = []
    for token in (raw or "").split(","):
        try:
            value = int(token.strip())
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    return sorted(set(values))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))
    return round(values[index], 3)


def _request(url: str, api_key: str, model: str, prompt: str,
             max_tokens: int, timeout: float) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        elapsed = (time.perf_counter() - started) * 1000.0
        data = json.loads(raw.decode("utf-8"))
        usage = data.get("usage") if isinstance(data, dict) else None
        return {"ok": True, "latency_ms": round(elapsed, 3),
                "completion_tokens": usage.get("completion_tokens") if isinstance(usage, dict) else None,
                "prompt_tokens": usage.get("prompt_tokens") if isinstance(usage, dict) else None}
    except (OSError, urllib.error.URLError, urllib.error.HTTPError,
            TimeoutError, json.JSONDecodeError) as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return {"ok": False, "latency_ms": round(elapsed, 3),
                "error": str(exc)[:240]}


def _level(url: str, api_key: str, model: str, prompt: str, max_tokens: int,
           timeout: float, concurrency: int, count: int, config: dict) -> dict:
    before = gpu.snapshot(config)
    started = time.perf_counter()
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_request, url, api_key, model, prompt,
                               max_tokens, timeout) for _ in range(count)]
        for future in as_completed(futures):
            results.append(future.result())
    wall = time.perf_counter() - started
    successful = [item for item in results if item.get("ok")]
    latencies = [float(item["latency_ms"]) for item in results]
    after = gpu.snapshot(config)
    return {
        "concurrency": concurrency,
        "requests": count,
        "successes": len(successful),
        "errors": count - len(successful),
        "error_rate": round((count - len(successful)) / count, 4) if count else 0.0,
        "wall_seconds": round(wall, 3),
        "throughput_requests_per_second": round(len(successful) / wall, 4) if wall else 0.0,
        "gpu_before_level": before,
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3) if latencies else None,
            "mean": round(statistics.mean(latencies), 3) if latencies else None,
        },
        "gpu_after_level": after,
        "errors_sample": [item.get("error") for item in results if not item.get("ok")][:3],
        "results": results,
    }


def _recommend(levels: list[dict], config: dict) -> dict:
    if not levels:
        return {"recommended_max_concurrency": 0, "reason": "no measurements"}
    baseline = next((item for item in levels if item["concurrency"] == min(
        item["concurrency"] for item in levels)), levels[0])
    base_p95 = baseline["latency_ms"].get("p95")
    allowed = []
    for item in levels:
        p95 = item["latency_ms"].get("p95")
        if item["errors"] == 0 and p95 is not None and base_p95 is not None and p95 <= base_p95 * 1.25:
            allowed.append(item["concurrency"])
    chosen = max(allowed) if allowed else baseline["concurrency"]
    configured = int(core.cfg("researchagen.governor.max_research_children", 2, config))
    chosen = min(chosen, configured)
    return {
        "recommended_max_concurrency": chosen,
        "configured_cap": configured,
        "criterion": "zero errors and p95 <= 1.25 * sequential p95",
        "baseline_p95_ms": base_p95,
        "note": "modelled capacity must still be checked against free VRAM/reserve; benchmark is endpoint-specific",
    }


def run(config: dict | None = None, concurrencies: list[int] | None = None,
        requests_per_level: int = 3, prompt: str = "Return exactly one short sentence: benchmark ready.",
        max_tokens: int = 32, timeout: float = 120.0,
        output: str | None = None) -> dict:
    config = config if config is not None else core.load_config()
    concurrencies = concurrencies or [1, 2]
    concurrencies = sorted(set(max(1, int(value)) for value in concurrencies))
    requests_per_level = max(1, int(requests_per_level))
    env = core.load_env()
    base_url = (env.get("RESEARCHAGEN_MODEL_BASE_URL") or "").rstrip("/")
    model = env.get("RESEARCHAGEN_MODEL_NAME") or str(core.cfg("model.default", "", config) or "")
    api_key = env.get("RESEARCHAGEN_MODEL_API_KEY") or "local-key"
    url = base_url + "/chat/completions"
    if not base_url or not model:
        return {"ok": False, "reason": "RESEARCHAGEN_MODEL_BASE_URL and model are required"}

    conn = core.db()
    lease = governor.acquire_experiment(conn, "governor-benchmark", "CALIBRATION", config)
    if not lease.get("ok"):
        conn.close()
        return {"ok": False, "reason": lease.get("reason"), "governor": lease}
    levels: list[dict] = []
    try:
        for concurrency in concurrencies:
            governor.heartbeat(conn, lease["lease_id"], config)
            levels.append(_level(url, api_key, model, prompt, max_tokens,
                                 timeout, concurrency, requests_per_level, config))
        recommendation = _recommend(levels, config)
        # The measured ceiling only tightens the configured cap; it can never
        # raise it or bypass the parent/experiment lock.  Keeping it in the
        # existing SQLite settings makes the next autonomous plan adaptive.
        core.set_setting(
            conn, "governor.measured_max_concurrency",
            recommendation["recommended_max_concurrency"],
        )
        core.set_setting(conn, "governor.measured_at", core.iso())
        result = {
            "ok": True,
            "model": model,
            "endpoint": base_url,
            "concurrencies": concurrencies,
            "requests_per_level": requests_per_level,
            "levels": levels,
            "recommendation": recommendation,
            "scientific_claim": "none: calibration telemetry only",
        }
    finally:
        governor.finish_experiment(conn, "governor-benchmark", config, analysis=True)
        governor.complete_analysis(conn, config)
        conn.close()

    if output:
        path = output if os.path.isabs(output) else os.path.join(core.ROOT, output)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, default=str)
        result["output"] = path
    return result


def main(argv: list[str]) -> int:
    core.load_env()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd != "run":
        core.fail("использование: governor_benchmark.py run [--concurrencies 1,2] [--json]")
    config = core.load_config()
    data = run(
        config=config,
        concurrencies=_float_list(core.arg(argv, "concurrencies", "1,2")),
        requests_per_level=int(core.arg(argv, "requests-per-level", 3)),
        prompt=core.arg(argv, "prompt", "Return exactly one short sentence: benchmark ready."),
        max_tokens=int(core.arg(argv, "max-tokens", 32)),
        timeout=float(core.arg(argv, "timeout", 120)),
        output=core.arg(argv, "output"),
    )
    text = str(data.get("reason") or data.get("recommendation") or data)
    core.emit(data, as_json, text)
    return 0 if data.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
