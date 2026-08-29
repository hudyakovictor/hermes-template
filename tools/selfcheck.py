#!/usr/bin/env python3
"""researchagen — самопроверка профиля (doctor).

Проверяет то, что ломается на практике: нет .env, пустой токен, чужой токен
(тот же, что у основного профиля), недоступная локальная модель, нет GPU в production,
база не пишется, гипотез меньше минимума, зависшие прогоны.

Exit code: 0 — всё критичное в порядке; 1 — есть FAIL.

CLI: python tools/selfcheck.py all [--json]
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

import conclave
import core
import gpu
import governor
import queue as q

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _check(name: str, state: str, detail: str) -> dict:
    return {"check": name, "state": state, "detail": detail}


def check_layout() -> list[dict]:
    out = []
    for name in ("SOUL.md", ".hermes.md", "MISSION.md", "FOCUS.md", "config.yaml",
                 "distribution.yaml", "tools/conclave.py", "skills/conclave/SKILL.md",
                 "skills/debate/SKILL.md", "cron/conclave-watch.json"):
        path = os.path.join(core.ROOT, name)
        out.append(_check(f"файл {name}", OK if os.path.exists(path) else FAIL,
                          path if os.path.exists(path) else "отсутствует"))
    for d in core.ALL_DIRS:
        core.ensure_dirs()
        out.append(_check(f"каталог {os.path.basename(d)}",
                          OK if os.path.isdir(d) else FAIL, d))
    return out


def check_env() -> list[dict]:
    env = core.load_env()
    out = []
    if not os.path.exists(core.ENV_PATH):
        out.append(_check(".env", FAIL, "нет файла — запусти install.ps1 / install.sh"))
        return out
    out.append(_check(".env", OK, core.ENV_PATH))
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        out.append(_check("TELEGRAM_BOT_TOKEN", FAIL, "пусто"))
    elif ":" not in token:
        out.append(_check("TELEGRAM_BOT_TOKEN", FAIL, "не похож на токен BotFather"))
    else:
        out.append(_check("TELEGRAM_BOT_TOKEN", OK, f"…{token[-6:]}"))
    out.append(_check("TELEGRAM_HOME_CHANNEL",
                      OK if env.get("TELEGRAM_HOME_CHANNEL") else FAIL,
                      env.get("TELEGRAM_HOME_CHANNEL", "пусто")))
    users = [u for u in (env.get("TELEGRAM_ALLOWED_USERS", "") or "").split(",") if u.strip()]
    out.append(_check("TELEGRAM_ALLOWED_USERS",
                      OK if len(users) >= 1 else FAIL,
                      f"{len(users)} пользователей" +
                      (" — ожидалось 2" if len(users) == 1 else "")))
    return out


def check_isolation() -> list[dict]:
    """Главный риск двухпрофильной схемы: один токен на два gateway."""
    env = core.load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    out = []
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    profiles_dir = os.path.join(home, "profiles")
    clashes = []
    if token and os.path.isdir(profiles_dir):
        for entry in os.listdir(profiles_dir):
            other = os.path.join(profiles_dir, entry, ".env")
            if not os.path.exists(other) or os.path.abspath(other) == os.path.abspath(core.ENV_PATH):
                continue
            try:
                with open(other, "r", encoding="utf-8", errors="replace") as fh:
                    if token in fh.read():
                        clashes.append(entry)
            except OSError:
                continue
    root_env = os.path.join(home, ".env")
    if token and os.path.exists(root_env):
        try:
            with open(root_env, "r", encoding="utf-8", errors="replace") as fh:
                if token in fh.read():
                    clashes.append("default (корневой профиль)")
        except OSError:
            pass
    out.append(_check("изоляция токена", FAIL if clashes else OK,
                      f"тот же токен в: {', '.join(clashes)} — второй gateway не запустится"
                      if clashes else "токен уникален среди профилей"))
    out.append(_check("HERMES_HOME", OK, home))
    return out


def check_model() -> list[dict]:
    env = core.load_env()
    base = (env.get("RESEARCHAGEN_MODEL_BASE_URL") or "").rstrip("/")
    name = env.get("RESEARCHAGEN_MODEL_NAME", "")
    if not base:
        return [_check("локальная модель", FAIL, "RESEARCHAGEN_MODEL_BASE_URL пуст")]
    url = base + "/models"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id", "") for m in data.get("data", [])]
        if name and not any(name.split(":")[0] in i for i in ids):
            return [_check("локальная модель", WARN,
                           f"endpoint жив, но модели {name} в списке нет: "
                           + ", ".join(ids[:6]))]
        return [_check("локальная модель", OK, f"{name} доступна на {base}")]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            json.JSONDecodeError, TimeoutError) as exc:
        return [_check("локальная модель", FAIL,
                       f"{base} не отвечает ({exc}). Запусти ollama serve и проверь порт.")]


def check_gpu() -> list[dict]:
    snap = gpu.snapshot()
    if snap["available"]:
        state = OK if snap["free_gb"] >= snap["required_gb"] else WARN
        return [_check("GPU", state,
                       f"{snap['best']['name']}: свободно {snap['free_gb']:.1f} GB "
                       f"из {snap['best']['total_gb']:.1f} GB")]
    if snap["debug"]:
        return [_check("GPU", OK, "macOS/debug: эксперименты идут как dry-run — штатно")]
    return [_check("GPU", FAIL, "nvidia-smi не найден, а режим production")]


def check_db() -> list[dict]:
    try:
        conn = core.db()
        core.set_setting(conn, "selfcheck.last", core.iso())
        live = q.live_count(conn)
        minimum = int(core.cfg("researchagen.limits.min_live_hypotheses", 3))
        out = [_check("база состояния", OK, core.DB_PATH)]
        out.append(_check("живых гипотез", OK if live >= minimum else WARN,
                          f"{live} (минимум по правилу R2: {minimum})"))
        stale = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE state='running' "
            "AND datetime(started_at) < datetime('now','-1 day')").fetchone()[0]
        out.append(_check("зависшие прогоны", OK if stale == 0 else WARN,
                          f"{stale} прогонов в статусе running >24ч "
                          "(починит tools/hygiene.py)" if stale else "нет"))
        return out
    except Exception as exc:  # noqa: BLE001
        return [_check("база состояния", FAIL, str(exc))]


def check_python() -> list[dict]:
    major, minor = sys.version_info[:2]
    state = OK if (major, minor) >= (3, 9) else FAIL
    return [_check("python", state, f"{sys.version.split()[0]} — нужен 3.9+ (только stdlib)")]


def check_governor() -> list[dict]:
    config = core.load_config()
    enabled = governor.enabled(config)
    max_children = int(core.cfg("delegation.max_concurrent_children", 0, config) or 0)
    max_depth = int(core.cfg("delegation.max_spawn_depth", 0, config) or 0)
    if not enabled:
        return [_check("governor", FAIL, "research/GPU admission controller отключён")]
    if max_children < 1 or max_depth != 1:
        return [_check("governor", FAIL,
                       f"unsafe Hermes delegation cap: children={max_children}, depth={max_depth}")]
    conn = None
    try:
        conn = core.db()
        plan = governor.plan(conn, config)
        return [_check("governor", OK,
                       f"mode={plan['mode']}, capacity={plan.get('capacity', 0)}, "
                       f"available={plan.get('available_slots', 0)}")]
    except Exception as exc:  # noqa: BLE001
        return [_check("governor", FAIL, str(exc))]
    finally:
        if conn is not None:
            conn.close()


def check_conclave() -> list[dict]:
    """Verify the communication layer is bounded and its priors are labelled."""
    config = core.load_config()
    if not conclave.enabled(config):
        return [_check("conclave", FAIL, "persona review layer отключён")]
    rounds = int(core.cfg("researchagen.conclave.max_rounds", 0, config) or 0)
    slots = int(core.cfg("researchagen.conclave.min_slots_for_debate", 0, config) or 0)
    if rounds < 1 or rounds > 3 or slots < 2:
        return [_check("conclave", FAIL,
                       f"unsafe bounds: max_rounds={rounds}, min_slots={slots}")]
    bad_priors = [p["id"] for p in conclave.PHRASES
                  if p.get("prior_effectiveness") != 0.95 or
                  p.get("target_positive_effect") != 0.90]
    conn = None
    try:
        conn = core.db()
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'conclave_%'"
        ).fetchall()}
        required = {"conclave_sessions", "conclave_assignments", "conclave_messages",
                    "conclave_phrase_events"}
        if bad_priors or not required.issubset(tables):
            return [_check("conclave", FAIL,
                           "schema/priors incomplete: " + ", ".join(sorted(bad_priors or ())))]
        return [_check("conclave", OK,
                       f"bounded debate={rounds} rounds, {len(conclave.PHRASES)} measurable nudges")]
    except Exception as exc:  # noqa: BLE001
        return [_check("conclave", FAIL, str(exc))]
    finally:
        if conn is not None:
            conn.close()


def run_all() -> dict:
    checks: list[dict] = []
    checks += check_python()
    checks += check_layout()
    checks += check_env()
    checks += check_isolation()
    checks += check_model()
    checks += check_gpu()
    checks += check_governor()
    checks += check_conclave()
    checks += check_db()
    fails = [c for c in checks if c["state"] == FAIL]
    warns = [c for c in checks if c["state"] == WARN]
    return {"checks": checks, "fails": len(fails), "warns": len(warns),
            "ok": not fails}


def main(argv: list[str]) -> int:
    as_json = core.wants_json(argv)
    data = run_all()
    icons = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    text = "\n".join(f"{icons[c['state']]} {c['check']}: {c['detail']}" for c in data["checks"])
    text += (f"\n\nИтог: ошибок {data['fails']}, предупреждений {data['warns']}. "
             + ("Контур готов к работе." if data["ok"] else "Запускать нельзя — см. ❌."))
    core.emit(data, as_json, text)
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
