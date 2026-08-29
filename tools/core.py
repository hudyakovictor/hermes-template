"""researchagen — ядро инструментов.

Только stdlib. Никаких pip-зависимостей: профиль обязан ставиться на чистую
Windows и macOS рядом с уже установленным hermes-agent.

Ответственность модуля:
  * пути профиля;
  * чтение config.yaml (мини-парсер подмножества YAML) и .env;
  * единая SQLite-база состояния (очередь, запуски, вердикты, события, настройки);
  * журнал событий и вывод (таблицы / JSON) для Telegram и CLI.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- пути

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("RESEARCHAGEN_HOME") or os.path.dirname(TOOLS_DIR)
ROOT = os.path.abspath(ROOT)

STATE_DIR = os.path.join(ROOT, "state")
DB_PATH = os.path.join(STATE_DIR, "researchagen.sqlite3")
SIGNALS_DIR = os.path.join(ROOT, "signals")
HYPO_DIR = os.path.join(ROOT, "hypotheses")
EXP_DIR = os.path.join(ROOT, "experiments")
INBOX_DIR = os.path.join(ROOT, "inbox")
MEMORY_DIR = os.path.join(ROOT, "memory")
REPORTS_DIR = os.path.join(ROOT, "reports")
LOGS_DIR = os.path.join(ROOT, "logs")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
ENV_PATH = os.path.join(ROOT, ".env")

ALL_DIRS = (STATE_DIR, SIGNALS_DIR, HYPO_DIR, EXP_DIR, INBOX_DIR,
            MEMORY_DIR, REPORTS_DIR, LOGS_DIR)


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------- время

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_days(value: str | None) -> float:
    dt = parse_iso(value)
    if dt is None:
        return 0.0
    return max(0.0, (now() - dt).total_seconds() / 86400.0)


def human_delta(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}с"
    if seconds < 5400:
        return f"{seconds // 60}мин"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}ч"
    return f"{seconds / 86400:.1f}д"


# --------------------------------------------------------------------------- .env

def load_env(path: str = ENV_PATH) -> dict:
    """Читает .env в dict и добавляет в os.environ то, чего там нет."""
    data: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                data[key] = val
                os.environ.setdefault(key, val)
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_ALLOWED_USERS",
                "TELEGRAM_CRON_THREAD_ID", "RESEARCHAGEN_MODEL_BASE_URL",
                "RESEARCHAGEN_MODEL_NAME", "OPENROUTER_API_KEY"):
        if key in os.environ and key not in data:
            data[key] = os.environ[key]
    return data


# --------------------------------------------------------------------------- config.yaml

_NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _coerce(token: str):
    token = token.strip()
    if token.startswith("#"):
        return None
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        return token[1:-1]
    # отрезаем комментарий в хвосте значения
    if "#" in token:
        token = token.split("#", 1)[0].strip()
    if token in ("true", "True", "yes"):
        return True
    if token in ("false", "False", "no"):
        return False
    if token in ("null", "~", ""):
        return None
    if _NUM_RE.match(token):
        return float(token) if "." in token else int(token)
    return token


def load_config(path: str = CONFIG_PATH) -> dict:
    """Мини-парсер подмножества YAML: вложенные маппинги + скаляры.

    Нам не нужен полный YAML: читаем только секцию researchagen: и пару
    ключей модели. Списки и блоки dm_topics игнорируются осознанно.
    """
    root: dict = {}
    if not os.path.exists(path):
        return root
    stack: list[tuple[int, dict]] = [(-1, root)]
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            if raw.lstrip().startswith("- "):
                continue  # списки не используются инструментами
            indent = len(raw) - len(raw.lstrip(" "))
            line = raw.strip()
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            key = key.strip()
            while stack and indent <= stack[-1][0]:
                stack.pop()
            parent = stack[-1][1] if stack else root
            rest = rest.strip()
            if rest == "" or rest.startswith("#") or rest in (">-", ">", "|"):
                node: dict = {}
                parent[key] = node
                stack.append((indent, node))
            else:
                parent[key] = _coerce(rest)
    return root


def cfg(dotted: str, default=None, config: dict | None = None):
    """cfg('researchagen.limits.approval_gpu_hours', 12)"""
    conf = config if config is not None else load_config()
    node = conf
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def platform_mode(config: dict | None = None) -> tuple[str, bool]:
    """(platform, is_debug). macOS — отладочный контур: эксперименты dry-run."""
    plat = str(cfg("researchagen.platform", "", config) or "").lower()
    if not plat or plat.startswith("<<installer_") or plat.startswith("${"):
        plat = "macos" if sys.platform == "darwin" else (
            "windows" if os.name == "nt" else "linux")
    mode = str(cfg("researchagen.mode", "", config) or "").lower()
    if not mode or mode.startswith("<<installer_") or mode.startswith("${"):
        mode = "debug" if plat == "macos" else "production"
    return plat, mode in ("debug", "true", "1")


# --------------------------------------------------------------------------- SQLite

SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id            TEXT PRIMARY KEY,          -- H-001
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    -- queued | running | paused_checkpoint | blocked | confirmed | partial
    -- | rejected | killed | archived
    level         TEXT NOT NULL DEFAULT 'L0',
    signals       INTEGER NOT NULL DEFAULT 0,
    novelty       REAL NOT NULL DEFAULT 0.5,
    early_pct     REAL NOT NULL DEFAULT 10.0, -- % обучения, когда сигнал читаем
    standard     REAL NOT NULL DEFAULT 0.4,   -- шанс стать стандартом
    money        REAL NOT NULL DEFAULT 0.4,   -- коммерческий потенциал
    decidability REAL NOT NULL DEFAULT 0.5,   -- однозначность PASS/FAIL
    est_hours    REAL NOT NULL DEFAULT 4.0,
    forecast     REAL,                        -- прогноз эффекта, % (фиксируется ДО запуска)
    forecast_low REAL,                        -- коридор: нижняя граница
    forecast_high REAL,                       -- коридор: верхняя граница
    p_repro       REAL,                       -- вероятность воспроизведения 0..1
    base_rate     REAL,                       -- base rate: доля похожих случаев с эффектом
    buyer         TEXT,                       -- кому продадим (при money >= 0.5)
    industry_usecase TEXT,                    -- что изменит в индустрии и у кого
    demand_signals INTEGER NOT NULL DEFAULT 0,-- внешние признаки спроса (нужно 3 для L2)
    controversy  INTEGER NOT NULL DEFAULT 0,  -- спорность: сколько споров вызвал в чате
    kill_checks_passed INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL DEFAULT 'dr',  -- dr | human | dr-deep
    card_path    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    notes        TEXT
);

-- Ставки агентов ДО вердикта (#7): кто верит в гипотезу, а кто нет.
-- Оценивается по факту: bet=confirmed выигрывает при confirmed/partial,
-- bet=rejected — при rejected/killed. Brier-подобный счёт в calibration().
CREATE TABLE IF NOT EXISTS agent_bets (
    bet_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    agent      TEXT NOT NULL,
    hypo_id    TEXT NOT NULL,
    bet        TEXT NOT NULL,             -- confirmed | rejected
    made_at    TEXT NOT NULL,
    resolved   INTEGER NOT NULL DEFAULT 0,
    won        INTEGER,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS idea_log (
    idea_id    TEXT PRIMARY KEY,          -- IN-XXX | DUP-XXXXX
    text       TEXT NOT NULL,
    title      TEXT,
    verdict    TEXT NOT NULL,             -- queued | rejected | duplicate
    reason     TEXT,                      -- почему: пробелы или причина отказа
    pi         REAL,
    ppi        REAL,
    hypo_id    TEXT,
    source     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    hypo_id      TEXT NOT NULL,
    level        TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | preempted
    seeds        INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    gpu_hours    REAL NOT NULL DEFAULT 0.0,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    pid          INTEGER,
    log_path     TEXT,
    FOREIGN KEY (hypo_id) REFERENCES hypotheses(id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    hypo_id      TEXT NOT NULL,
    level        TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- confirmed | partial | rejected | killed
    forecast     REAL,
    actual       REAL,
    deviation    REAL,            -- % отклонения факта от прогноза
    in_corridor  INTEGER,         -- #2: факт внутри коридора [low, high] (0/1)
    forecast_low REAL,
    forecast_high REAL,
    seeds_pass   INTEGER NOT NULL DEFAULT 0,
    seeds_total  INTEGER NOT NULL DEFAULT 0,
    sigma        REAL,
    gpu_hours    REAL NOT NULL DEFAULT 0.0,
    what_changes TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    hypo_id      TEXT,
    payload      TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- Governor leases are the durable admission ledger shared by the parent
-- Hermes session, cron ticks and experiment processes.  They are deliberately
-- separate from hypotheses/runs: scientific state remains authoritative there.
CREATE TABLE IF NOT EXISTS governor_leases (
    lease_id           TEXT PRIMARY KEY,
    owner_id           TEXT NOT NULL,
    kind               TEXT NOT NULL,       -- research | experiment
    state              TEXT NOT NULL DEFAULT 'active',
    mode               TEXT NOT NULL,
    task_id            TEXT,
    requested_vram_gb  REAL,
    acquired_at        TEXT NOT NULL,
    heartbeat_at       TEXT NOT NULL,
    expires_at         TEXT,
    checkpoint         TEXT,
    reason             TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS governor_reports (
    report_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id       TEXT,
    task_id         TEXT,
    status          TEXT NOT NULL,
    accepted        INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL,
    errors          TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bd_meta (
    namespace    TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS bd_regions (
    namespace         TEXT NOT NULL,
    id                TEXT NOT NULL,
    parent_id         TEXT,
    name              TEXT NOT NULL,
    query             TEXT NOT NULL,
    depth             INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'frontier',
    visits            INTEGER NOT NULL DEFAULT 0,
    signal_score      REAL NOT NULL DEFAULT 0.0,
    no_signal_streak  INTEGER NOT NULL DEFAULT 0,
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (namespace, id)
);

CREATE TABLE IF NOT EXISTS bd_hypotheses (
    namespace          TEXT NOT NULL,
    id                 TEXT NOT NULL,
    region_id          TEXT NOT NULL,
    text               TEXT NOT NULL,
    mechanism          TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'candidate',
    signal_sources     TEXT NOT NULL DEFAULT '[]',
    evidence_ids       TEXT NOT NULL DEFAULT '[]',
    novelty_score      REAL NOT NULL DEFAULT 0.0,
    mechanism_score    REAL NOT NULL DEFAULT 0.0,
    experiment_score   REAL NOT NULL DEFAULT 0.0,
    commercial_score   REAL NOT NULL DEFAULT 0.0,
    decidability_score REAL NOT NULL DEFAULT 0.0,
    priority           REAL NOT NULL DEFAULT 0.0,
    estimated_hours   REAL NOT NULL DEFAULT 0.25,
    forecast           REAL,
    origin_id          TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (namespace, id)
);

CREATE TABLE IF NOT EXISTS bd_evidence (
    namespace        TEXT NOT NULL,
    id               TEXT NOT NULL,
    candidate_id     TEXT NOT NULL,
    source           TEXT NOT NULL,
    claim            TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'literature',
    independent_key  TEXT NOT NULL DEFAULT '',
    strength         REAL NOT NULL DEFAULT 0.0,
    metadata         TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    PRIMARY KEY (namespace, id)
);

CREATE TABLE IF NOT EXISTS bd_history (
    history_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT NOT NULL,
    run_id       INTEGER,
    iteration    INTEGER NOT NULL DEFAULT 0,
    event        TEXT NOT NULL,
    region_id    TEXT,
    hypothesis_id TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bd_cache (
    namespace    TEXT NOT NULL,
    cache_key    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    PRIMARY KEY (namespace, cache_key)
);

CREATE TABLE IF NOT EXISTS bd_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace    TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    iterations   INTEGER NOT NULL DEFAULT 0,
    cost_usd     REAL NOT NULL DEFAULT 0.0,
    status       TEXT NOT NULL DEFAULT 'running',
    summary      TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_hypo_status ON hypotheses(status);
CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(state);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);
CREATE INDEX IF NOT EXISTS idx_governor_leases_state ON governor_leases(kind, state, acquired_at);
CREATE INDEX IF NOT EXISTS idx_governor_leases_owner ON governor_leases(owner_id, state);
CREATE INDEX IF NOT EXISTS idx_governor_reports_task ON governor_reports(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bd_history_namespace ON bd_history(namespace, created_at);
CREATE INDEX IF NOT EXISTS idx_bd_regions_frontier ON bd_regions(namespace, status, signal_score);
CREATE INDEX IF NOT EXISTS idx_bd_hypotheses_region ON bd_hypotheses(namespace, region_id, status);
"""


# Мягкая миграция: старые базы получают новые колонки hypotheses без пересоздания.
_HYPO_MIGRATIONS = (
    ("forecast_low", "REAL"), ("forecast_high", "REAL"), ("p_repro", "REAL"),
    ("base_rate", "REAL"), ("buyer", "TEXT"), ("industry_usecase", "TEXT"),
    ("demand_signals", "INTEGER NOT NULL DEFAULT 0"),
    ("controversy", "INTEGER NOT NULL DEFAULT 0"),
)
_VERDICT_MIGRATIONS = (
    ("in_corridor", "INTEGER"), ("forecast_low", "REAL"), ("forecast_high", "REAL"),
)


def db(path: str = DB_PATH) -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    existing = {r["name"] for r in conn.execute(
        "PRAGMA table_info(hypotheses)").fetchall()}
    for column, decl in _HYPO_MIGRATIONS:
        if column not in existing:
            conn.execute(f"ALTER TABLE hypotheses ADD COLUMN {column} {decl}")
    existing_v = {r["name"] for r in conn.execute(
        "PRAGMA table_info(verdicts)").fetchall()}
    for column, decl in _VERDICT_MIGRATIONS:
        if column not in existing_v:
            conn.execute(f"ALTER TABLE verdicts ADD COLUMN {column} {decl}")
    conn.commit()
    return conn


def log_event(conn: sqlite3.Connection, kind: str, hypo_id: str | None = None,
              **payload) -> None:
    conn.execute(
        "INSERT INTO events (kind, hypo_id, payload, created_at) VALUES (?,?,?,?)",
        (kind, hypo_id, json.dumps(payload, ensure_ascii=False), iso()),
    )
    conn.commit()


_EXTRA_ROOTS: set[str] = set()


def allow_root(path: str) -> None:
    """Временно разрешить корень (для тестов на временных каталогах).

    Прод-изоляция не меняется: список живёт в процессе, а не в конфиге.
    """
    _EXTRA_ROOTS.add(os.path.abspath(path))


def safe_path(path: str, where: str = "запись") -> str:
    """Изоляция среды: файловые операции только внутри ROOT профиля.

    Основной агент (memories/, sessions/, workspace/, auth.json) живёт на том
    же устройстве — случайная запись мимо ROOT означала бы контаминацию его
    истории и памяти. Любой путь с ".." или абсолютный внешний путь — отказ.
    Вызывается до записи, на ранней стадии — ошибка видна сразу, не в проде.
    """
    candidate = os.path.abspath(os.path.join(os.path.abspath(ROOT), path))
    roots = {os.path.abspath(ROOT)} | set(_EXTRA_ROOTS)
    if any(candidate == r or candidate.startswith(r + os.sep) for r in roots):
        return candidate
    raise PermissionError(
        f"{where} вне ROOT ({os.path.abspath(ROOT)}) запрещена: {path!r} — "
        f"изоляция профилей")


def to_number(value, field: str):
    """Числовой ввод из CLI/JSON: Reject не-чисел И не-конечных (nan/inf).

    NaN проходит float() и тихо отравляет приоритеты — поэтому конечность
    проверяется явно, с внятным сообщением, а не трейсбеком на 3 шага позже.
    """
    import math
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} должен быть числом, получено {value!r}") from None
    if not math.isfinite(num):
        raise ValueError(f"{field} должен быть конечным числом, получено {value!r}")
    return num


def setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), iso()),
    )
    conn.commit()


def next_hypo_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT id FROM hypotheses ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return "H-001"
    try:
        num = int(str(row["id"]).split("-")[-1]) + 1
    except ValueError:
        num = conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0] + 1
    return f"H-{num:03d}"


# --------------------------------------------------------------------------- вывод

LIVE_STATUSES = ("queued", "running", "paused_checkpoint", "blocked")
CLOSED_STATUSES = ("confirmed", "partial", "rejected", "killed", "archived")


def table(rows: list[list], header: list[str]) -> str:
    """Markdown-таблица: Telegram с rich_messages рендерит её нативно."""
    cells = [[str(c) for c in r] for r in rows]
    if not cells:
        return "_пусто_"
    widths = [len(h) for h in header]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(header)) + " |",
           "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for row in cells:
        out.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return "\n".join(out)


def emit(payload, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(text if text is not None else payload)


def wants_json(argv: list[str]) -> bool:
    return "--json" in argv


def arg(argv: list[str], name: str, default=None):
    """--key value | --key=value"""
    flag = f"--{name}"
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return default


def flag(argv: list[str], name: str) -> bool:
    return f"--{name}" in argv


def fail(message: str, code: int = 2) -> "NoReturn":  # type: ignore[name-defined]
    print(f"ОШИБКА: {message}", file=sys.stderr)
    raise SystemExit(code)


def append_log(name: str, line: str) -> None:
    ensure_dirs()
    with open(safe_path(os.path.join(LOGS_DIR, name)), "a", encoding="utf-8") as fh:
        fh.write(f"{iso()} {line}\n")


__all__ = [n for n in dir() if not n.startswith("_")]
