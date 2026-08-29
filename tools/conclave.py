#!/usr/bin/env python3
"""researchagen Conclave — bounded persona review and Telegram transcript.

The parent Hermes session remains the only governor.  This module does not
create an unbounded swarm and it does not decide scientific truth.  It provides
three durable pieces around native ``delegate_task`` leaves:

* fixed responsibility zones and short English internal protocols;
* a triggered, turn-limited steelman/falsification debate;
* a public Russian transcript with optional task/customer side-comments.

Only concise public summaries are stored.  Hidden chain-of-thought is never
persisted or sent to Telegram.  ``95%``/``90%`` phrase figures are explicitly
priors to measure, not achieved product metrics.

Typical parent flow:

  python tools/rg.py conclave plan --task-id H-003 --stage critique --context-file c.json
  python tools/rg.py conclave open --task-id H-003 --title "..." \\
      --stage critique --context-file c.json
  python tools/rg.py conclave assign --session D-... --json
  python tools/rg.py conclave brief --session D-... --assignment A-... --json
  # native delegate_task receives the brief and returns reports/...
  python tools/rg.py conclave report --session D-... --assignment A-... \\
      --file reports/worker.json --json
  python tools/rg.py conclave speak --session D-... --assignment A-... \\
      --kind critique --round 1 --task "..." --client "..."
  python tools/rg.py conclave transcript --session D-... --send
  python tools/rg.py conclave close --session D-... --decision "..."

The Telegram gateway remains the only reader of updates.  ``tg.py`` is used
only for outbound messages, so the one-token/one-long-polling invariant stays
intact.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets
import sys
import uuid
from typing import Any

import core
import governor
import tg


# SystemRandom gives genuine per-event variation without adding a dependency.
# Tests replace this object with a deterministic mock.
_RNG = random.SystemRandom()

STAGES = ("research", "triage", "critique", "falsification", "adjudication")
SESSION_STATUSES = ("open", "awaiting_reports", "debating", "awaiting_decision", "closed")
AUDIENCES = ("task", "client", "debate")
MESSAGE_KINDS = (
    "brief", "analysis", "claim", "critique", "defense", "rebuttal",
    "nudge", "comment", "decision", "system",
)
OUTCOMES = ("positive", "neutral", "negative")


# These are stable zones, not costumes selected ad hoc by a child.  The
# protocol text is deliberately in English for internal reasoning; visible
# outputs are required to be short Russian summaries.
ROLE_SPECS: dict[str, dict[str, Any]] = {
    "evidence": {
        "nickname": "Архивариус",
        "icon": "📎",
        "zone": "source_audit",
        "stance": "cold source auditor",
        "protocol": (
            "Audit every material claim. Trace it to a primary source or mark it "
            "unverified. Separate observation, interpretation and causal claim. "
            "Search for contradictory evidence and duplicate sources."
        ),
        "style": "dry obituary clerk; cites the page before sharpening the knife",
        "sample_lines": (
            "Ссылка есть. Теперь покажите строку, а не атмосферу вокруг неё.",
            "Источник красивый, но причинность пока лежит без паспорта.",
        ),
    },
    "falsifier": {
        "nickname": "Кувалда",
        "icon": "🔨",
        "zone": "falsification",
        "stance": "aggressive but fair skeptic",
        "protocol": (
            "Try to kill the claim with the cheapest decisive test. Name one "
            "confounder, one missing control and one observable failure condition. "
            "Attack the claim, never a protected trait or a private person."
        ),
        "style": "toxic methodological troll; sarcasm is aimed at assumptions",
        "sample_lines": (
            "Гипотеза просит оваций, а тест просит контроль. Я голосую за тест.",
            "Это не механизм, а фанфик с GPU-сметой. Где дешёвый kill-test?",
        ),
    },
    "steelman": {
        "nickname": "Адвокат",
        "icon": "⚔️",
        "zone": "steelman_and_defense",
        "stance": "devil's advocate",
        "protocol": (
            "State the strongest version of the opposing claim before defending it. "
            "Answer each objection with evidence or label it unresolved. Do not move "
            "the goalposts and do not treat eloquence as evidence."
        ),
        "style": "polite defender with a knife hidden in the footnotes",
        "sample_lines": (
            "Кувалда права в одном: сильную версию тоже нужно уметь убить.",
            "Защищаю не надежду, а конкретное наблюдение и его границы.",
        ),
    },
    "mechanism": {
        "nickname": "Паяльник",
        "icon": "🧰",
        "zone": "mechanism_and_implementation",
        "stance": "implementation realist",
        "protocol": (
            "Convert the claim into a minimal reproducible mechanism and test. "
            "Check instrumentation, leakage, seeds, runtime and rollback. Prefer "
            "a boring executable experiment over a beautiful story."
        ),
        "style": "factory-floor engineer; celebrates a working control, not a miracle",
        "sample_lines": (
            "Сказка закончилась на первом seed. Собираю минимальный воспроизводимый тест.",
            "Если это нельзя запустить и остановить, это пока не эксперимент.",
        ),
    },
    "market": {
        "nickname": "Касса",
        "icon": "🧾",
        "zone": "value_and_customer_risk",
        "stance": "commercial risk auditor",
        "protocol": (
            "Translate the claim into a measurable user or business outcome. Check "
            "cost, latency, adoption and failure downside. Reject the magic-button "
            "story unless the metric and counterfactual are explicit."
        ),
        "style": "corporate undertaker of fake revenue; keeps a calculator at the funeral",
        "sample_lines": (
            "Прибыль без counterfactual — это корпоративный некролог с красивым заголовком.",
            "Сколько стоит ошибка? Романтика заканчивается на этой строке бюджета.",
        ),
    },
    "narrator": {
        "nickname": "Некролог",
        "icon": "📰",
        "zone": "synthesis_and_public_context",
        "stance": "editorial synthesizer",
        "protocol": (
            "Compress the disagreement into a factual decision record. Distinguish "
            "what changed, what remains unknown and the next cheapest action. Keep "
            "the public message memorable but never let a joke replace a datum."
        ),
        "style": "punk tabloid meets corporate obituary and dry stand-up",
        "sample_lines": (
            "Публика требует чуда; редакция требует один источник и один следующий шаг.",
            "Хорошая новость: мы нашли неизвестное. Плохая: оно ещё не измерено.",
        ),
    },
}

ROLE_ORDER = tuple(ROLE_SPECS)

# A requested design prior, not a promise. Every selection is logged and can be
# marked positive/neutral/negative so the prior can be rejected by data.
# Reusable challenge patterns.  The parent selects a small subset per room;
# workers answer the pattern, not one another's rhetorical energy.  Each pattern
# asks for a checkable object, which makes a "fight" operationally useful.
DEBATE_TEMPLATES: dict[str, dict[str, Any]] = {
    "source-audit": {
        "purpose": "provenance and contradiction",
        "internal_protocol": "Name the exact primary source, quote or table, date, and what would falsify the interpretation.",
        "public_prompt": "ИСТОЧНИК: ссылка, точная строка и что именно она доказывает.",
    },
    "falsification": {
        "purpose": "cheapest decisive kill test",
        "internal_protocol": "Propose the cheapest test that can make the claim fail; specify metric, control and stop condition.",
        "public_prompt": "УДАР: один дешёвый тест, один контроль, один честный FAIL.",
    },
    "steelman": {
        "purpose": "strongest opposing case",
        "internal_protocol": "Restate the strongest version of the opposing claim before answering objections; label unresolved points.",
        "public_prompt": "STEELMAN: сначала усили чужую идею, потом спорь с усиленной версией.",
    },
    "confounder": {
        "purpose": "alternative explanation",
        "internal_protocol": "List the most ordinary confounder and the observation that separates it from the proposed mechanism.",
        "public_prompt": "ПОМЕХА: какое скучное объяснение мы ещё не убили и как его отличить?",
    },
    "replication": {
        "purpose": "robustness and transfer",
        "internal_protocol": "Specify seeds, held-out setting, minimum effect and replication boundary before scaling the claim.",
        "public_prompt": "ПОВТОР: seeds, граница переноса и минимальный эффект — без магии масштаба.",
    },
    "value-check": {
        "purpose": "customer and cost counterfactual",
        "internal_protocol": "Translate the claim into a counterfactual outcome with cost, latency and downside; reject vague profit language.",
        "public_prompt": "ЦЕНА: что изменится для человека, сколько это стоит и что будет при провале?",
    },
    "decision": {
        "purpose": "parent adjudication",
        "internal_protocol": "Summarize strongest evidence, strongest objection, unresolved uncertainty and the next action. Do not promote scientific state.",
        "public_prompt": "ПРИГОВОР: continue, kill, queue or self-review — и одна причина с числом.",
    },
}

_PRIVATE_CONTEXT_KEYS = {
    "chain_of_thought", "chain-of-thought", "cot", "hidden_reasoning", "internal_reasoning",
    "private_reasoning", "raw_reasoning", "reasoning", "analysis_trace", "thoughts", "scratchpad",
}


def _redact_context(value: Any):
    """Keep trigger metadata useful while excluding hidden reasoning recursively."""
    if isinstance(value, dict):
        return {
            str(key): _redact_context(item)
            for key, item in value.items()
            if str(key).lower() not in _PRIVATE_CONTEXT_KEYS
        }
    if isinstance(value, list):
        return [_redact_context(item) for item in value]
    return value


def _template_plan(stage: str, context: dict[str, Any] | None = None,
                   debate: bool = False) -> list[dict[str, Any]]:
    context = context or {}
    if debate or stage in ("critique", "falsification", "adjudication"):
        names = ["steelman", "falsification", "confounder", "source-audit", "decision"]
    else:
        names = ["source-audit", "confounder", "replication", "value-check", "decision"]
    if _truthy(context.get("source_conflict")) or _truthy(context.get("contradictory_evidence")):
        names = ["source-audit", "steelman", "falsification", "decision"]
    if _truthy(context.get("client_request")) or _truthy(context.get("money")):
        names.insert(-1, "value-check")
    seen: set[str] = set()
    selected = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        selected.append(dict(DEBATE_TEMPLATES[name], id=name))
    return selected


PHRASES: tuple[dict[str, Any], ...] = (
    {
        "id": "nudge.fact-before-fight",
        "tags": ("conflict", "source_conflict", "critique"),
        "text": "Стоп. Сейчас у нас не спор, а два красивых способа ошибиться. По одному проверяемому факту на человека.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.confidence-is-not-evidence",
        "tags": ("confidence_gap", "critique", "uncertain"),
        "text": "Уверенность без источника — это не уверенность, а макияж для гипотезы. Покажите контроль.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.steelman",
        "tags": ("defense", "critique", "falsification"),
        "text": "Кувалда, сначала собери сильнейшую версию идеи. Иначе это не критика, а шум в каске.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.cheapest-test",
        "tags": ("falsification", "high_cost", "mechanism"),
        "text": "Перед дорогим ритуалом вызови дешёвого убийцу: какой один тест сломает это за пять минут?",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.loop",
        "tags": ("stalled", "critique", "rebuttal"),
        "text": "Круг замкнулся. Либо новый источник, либо корпоративный некролог этой мысли. Повтор слов не считается данными.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.client-magic",
        "tags": ("client", "money", "high_cost"),
        "text": "Заказчик хочет кнопку «бабло». В наличии пока кнопка «проверить» — зато она не продаёт воздух.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.agi-factory",
        "tags": ("client", "money", "narrative"),
        "text": "AGI уже близко: сначала она принесёт вам прибыль, потом устроит вас на завод проверять CSV.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
    {
        "id": "nudge.no-miracle",
        "tags": ("uncertain", "research", "client"),
        "text": "Ложное утешение принято: чудо почти готово. Осталась мелочь — воспроизводимость, контроль и цифры.",
        "prior_effectiveness": 0.95,
        "target_positive_effect": 0.90,
    },
)

_THINK_BLOCK = re.compile(r"(?is)<(?:think|analysis|reasoning)>.*?</(?:think|analysis|reasoning)>")
_THREAT_PATTERNS = (
    re.compile(r"(?iu)\bсдохни\b"),
    re.compile(r"(?iu)\bумри\b"),
    re.compile(r"(?iu)\bубью\b"),
    re.compile(r"(?iu)\bя\s+тебя\s+уничтожу\b"),
)
_DIRECT_PERSONAL_ABUSE = re.compile(
    r"(?iu)\b(ты|вы|тебя|вас)\s+"
    r"(идиот(?:ка)?|дурак|дура|дебил(?:ка)?|туп(?:ой|ая)|лох|лошара)\b"
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "null", "none")
    return bool(value)


def _cfg(name: str, default: Any, config: dict | None = None) -> Any:
    return core.cfg(f"researchagen.conclave.{name}", default, config)


def _float_cfg(name: str, default: float, config: dict | None = None) -> float:
    try:
        return float(_cfg(name, default, config))
    except (TypeError, ValueError):
        return default


def _int_cfg(name: str, default: int, config: dict | None = None) -> int:
    try:
        return int(_cfg(name, default, config))
    except (TypeError, ValueError):
        return default


def enabled(config: dict | None = None) -> bool:
    return _truthy(_cfg("enabled", True, config))


def _decode(value: Any, default: Any):
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, json.JSONDecodeError):
        return default


def _safe_public(text: str, config: dict | None = None) -> str:
    """Keep chat lively without exposing hidden reasoning or direct threats."""
    text = str(text or "")
    text = _THINK_BLOCK.sub("", text)
    text = re.sub(r"\[/?(?:hidden_)?(?:chain[- ]of[- ]thought|cot)\]", "", text,
                  flags=re.IGNORECASE)
    for pattern in _THREAT_PATTERNS:
        text = pattern.sub("[угроза удалена]", text)
    if _truthy(_cfg("ban_personal_attacks", True, config)):
        text = _DIRECT_PERSONAL_ABUSE.sub("это допущение наивно", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    limit = max(160, _int_cfg("max_visible_chars", 720, config))
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text or "(сообщение без текста — даже сарказм требует payload)"


def _session(conn, session_id: str):
    return conn.execute(
        "SELECT * FROM conclave_sessions WHERE session_id=?", (session_id,)
    ).fetchone()


def _latest_session(conn, include_closed: bool = False):
    if include_closed:
        return conn.execute(
            "SELECT * FROM conclave_sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    return conn.execute(
        "SELECT * FROM conclave_sessions WHERE status!='closed' ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()


def _assignment(conn, assignment_id: str):
    return conn.execute(
        "SELECT * FROM conclave_assignments WHERE assignment_id=?", (assignment_id,)
    ).fetchone()


def _role(role_id: str) -> dict[str, Any]:
    return ROLE_SPECS.get(role_id, ROLE_SPECS["narrator"])


def _context_tags(context: dict[str, Any] | None) -> set[str]:
    context = context or {}
    tags = {"research"}
    stage = str(context.get("stage") or "").lower()
    if stage:
        tags.add(stage)
    for key in (
        "source_conflict", "contradictory_evidence", "confidence_gap",
        "unfalsifiable", "high_cost", "money", "client_request", "stalled",
        "falsification", "defense", "critique",
    ):
        if _truthy(context.get(key)):
            tags.add("client" if key == "client_request" else key)
    return tags


def detect_triggers(context: dict[str, Any] | None,
                    config: dict | None = None) -> dict:
    """Detect exceptional cases where debate is worth spending Qwen calls."""
    context = dict(context or {})
    reasons: list[str] = []
    score = 0.0
    reports = context.get("reports")
    if not isinstance(reports, list):
        reports = []

    confidences: list[float] = []
    for report in reports:
        if not isinstance(report, dict):
            continue
        try:
            value = float(report.get("confidence"))
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            confidences.append(value)
    if len(confidences) >= 2:
        gap = max(confidences) - min(confidences)
        threshold = _float_cfg("trigger_confidence_gap", 0.25, config)
        if gap >= threshold:
            reasons.append(f"confidence gap {gap:.2f} >= {threshold:.2f}")
            score += 0.35
            context["confidence_gap"] = gap

    if _truthy(context.get("source_conflict")) or _truthy(context.get("contradictory_evidence")):
        reasons.append("reports cite conflicting evidence")
        score += 0.40
    if _truthy(context.get("unfalsifiable")) or _truthy(context.get("missing_control")):
        reasons.append("claim has an unresolved falsification/control gap")
        score += 0.30
    try:
        cost = float(context.get("estimated_gpu_hours", 0) or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost >= _float_cfg("high_cost_gpu_hours", 4.0, config) or _truthy(context.get("high_cost")):
        reasons.append("decision has material GPU cost")
        score += 0.25
        context["high_cost"] = True
    if _truthy(context.get("client_request")) or _truthy(context.get("money")):
        reasons.append("customer/value claim needs a counterfactual")
        score += 0.15
    if _truthy(context.get("stalled")):
        reasons.append("discussion is repeating without a new datum")
        score += 0.25

    # Explicit flags are useful when the parent has already inspected a report.
    if _truthy(context.get("force_debate")):
        reasons.append("parent explicitly requested a review")
        score = max(score, 1.0)

    threshold = _float_cfg("trigger_score", 0.45, config)
    hard_trigger = any(_truthy(context.get(key)) for key in (
        "source_conflict", "contradictory_evidence", "unfalsifiable",
        "missing_control", "stalled", "force_debate",
    ))
    # A direct conflict/control failure is exceptional even when it is the only
    # signal. The score remains useful for softer high-cost/customer cases.
    required = bool(reasons) and (score >= threshold or hard_trigger)
    return {
        "required": required,
        "score": round(min(1.0, score), 3),
        "threshold": threshold,
        "reasons": reasons,
        "tags": sorted(_context_tags(context)),
        "report_count": len(reports),
        "context": context,
    }


def _role_ids(stage: str, debate: bool, context: dict[str, Any] | None = None) -> list[str]:
    stage = str(stage or "research").lower()
    context = context or {}
    if debate or stage in ("critique", "falsification", "adjudication"):
        order = ["falsifier", "steelman", "evidence", "mechanism", "market"]
        if _truthy(context.get("source_conflict")) or _truthy(context.get("contradictory_evidence")):
            order = ["evidence", "falsifier", "steelman", "mechanism", "market"]
    elif stage == "triage":
        order = ["evidence", "falsifier", "mechanism", "market"]
    else:
        order = ["evidence", "mechanism", "market", "falsifier"]
    return [role_id for role_id in order if role_id in ROLE_SPECS]


def role_plan(conn, task_id: str, stage: str = "research",
              context: dict[str, Any] | None = None,
              config: dict | None = None) -> dict:
    """Return an adaptive role allocation without spawning anything."""
    config = config if config is not None else core.load_config()
    stage = str(stage or "research").lower()
    if stage not in STAGES:
        return {"ok": False, "reason": f"stage must be one of: {', '.join(STAGES)}"}
    context = dict(context or {})
    context.setdefault("stage", stage)
    trigger = detect_triggers(context, config)
    admission = governor.plan(conn, config, requested_mode="auto")
    available = max(0, int(admission.get("available_slots", 0)))
    min_debate_slots = max(2, _int_cfg("min_slots_for_debate", 2, config))
    debate_capacity_possible = trigger["required"] and available >= min_debate_slots
    target = 2 if debate_capacity_possible else (1 if available else 0)
    requested = context.get("requested_workers")
    try:
        if requested is not None:
            target = min(target, max(0, int(requested)))
    except (TypeError, ValueError):
        pass
    debate_possible = debate_capacity_possible and target >= min_debate_slots
    selected = _role_ids(stage, debate_possible, context)[:target]
    return {
        "task_id": task_id,
        "stage": stage,
        "trigger": trigger,
        "admission": admission,
        "available_slots": available,
        "debate_capacity_possible": debate_capacity_possible,
        "debate_possible": debate_possible,
        "parent_role": {"id": "parent", "nickname": "Шеф", "zone": "governance"},
        "roles": [
            {"id": role_id, **{k: v for k, v in _role(role_id).items() if k != "protocol"},
             "protocol": _role(role_id)["protocol"]}
            for role_id in selected
        ],
        "challenge_templates": _template_plan(stage, context, debate_possible),
        "fallback": (
            "parent_self_review: no second slot; do not simulate consensus"
            if trigger["required"] and len(selected) < min_debate_slots else None
        ),
        "resource_rule": (
            "reserve one governor research lease per selected worker; if capacity changes, "
            "drop the lowest-priority role rather than bypass the gate"
        ),
    }


def open_session(conn, task_id: str, title: str = "", stage: str = "critique",
                 context: dict[str, Any] | None = None, force: bool = False,
                 config: dict | None = None) -> dict:
    """Open a durable debate only when exceptional evidence justifies it."""
    config = config if config is not None else core.load_config()
    if not enabled(config):
        return {"ok": False, "reason": "conclave disabled: no new review session"}
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "reason": "task_id is required"}
    stage = str(stage or "critique").lower()
    if stage not in STAGES:
        return {"ok": False, "reason": f"stage must be one of: {', '.join(STAGES)}"}
    context = dict(context or {})
    context.setdefault("stage", stage)
    if force:
        context["force_debate"] = True
    trigger = detect_triggers(context, config)
    if not trigger["required"] and not force:
        return {"ok": True, "opened": False, "reason": "debate trigger not met", "trigger": trigger}

    existing = conn.execute(
        "SELECT * FROM conclave_sessions WHERE task_id=? AND status NOT IN ('closed') "
        "ORDER BY opened_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    if existing is not None:
        return {"ok": True, "opened": False, "idempotent": True,
                "session_id": existing["session_id"], "status": existing["status"]}

    admission = governor.plan(conn, config, requested_mode="auto")
    if admission.get("mode") in ("testing", "analyze", "paused") or not admission.get("enabled"):
        return {
            "ok": False,
            "opened": False,
            "reason": "resource governor blocks new Qwen debate in current phase",
            "admission": admission,
            "trigger": trigger,
        }

    session_id = "D-" + uuid.uuid4().hex[:12]
    now = core.iso()
    max_rounds = max(1, _int_cfg("max_rounds", 2, config))
    payload = _redact_context(context)
    # ``detect_triggers`` echoes the supplied context for the parent, but the
    # durable room record gets only the detector's public facts.
    payload["detector"] = {key: value for key, value in trigger.items() if key != "context"}
    conn.execute(
        "INSERT INTO conclave_sessions "
        "(session_id,task_id,title,stage,status,trigger,context,max_rounds,opened_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, task_id, str(title or task_id), stage, "open",
         "; ".join(trigger["reasons"]) or "parent requested", json.dumps(payload, ensure_ascii=False),
         max_rounds, now, now),
    )
    conn.commit()
    core.log_event(conn, "conclave.open", None, session_id=session_id,
                   task_id=task_id,
                   trigger={key: value for key, value in trigger.items() if key != "context"})
    plan_data = role_plan(conn, task_id, stage, context, config)
    core.log_event(conn, "conclave.plan", None, session_id=session_id,
                   roles=[r["id"] for r in plan_data["roles"]],
                   debate_possible=plan_data["debate_possible"])
    return {"ok": True, "opened": True, "session_id": session_id,
            "trigger": trigger, "plan": plan_data, "status": "open"}


def assign(conn, session_id: str, reserve: bool = True,
           max_workers: int | None = None, config: dict | None = None) -> dict:
    """Attach stable personas/zones and optionally reserve governor leases."""
    config = config if config is not None else core.load_config()
    session = _session(conn, session_id)
    if session is None:
        return {"ok": False, "reason": "conclave session not found"}
    if session["status"] == "closed":
        return {"ok": False, "reason": "session is closed"}
    context = _decode(session["context"], {})
    detector = context.get("detector") if isinstance(context, dict) else {}
    trigger_required = bool(isinstance(detector, dict) and detector.get("required"))
    admission = governor.plan(conn, config, requested_mode="auto")
    available = max(0, int(admission.get("available_slots", 0)))
    existing = conn.execute(
        "SELECT * FROM conclave_assignments WHERE session_id=? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    existing_roles = {row["role_id"] for row in existing}
    existing_live = sum(row["state"] in ("reserved", "planned") for row in existing)
    min_debate_slots = max(2, _int_cfg("min_slots_for_debate", 2, config))
    debate = trigger_required and (existing_live + available) >= min_debate_slots
    target_total = 2 if debate else (1 if available or existing else 0)
    requested = context.get("requested_workers") if isinstance(context, dict) else None
    if max_workers is None and requested is not None:
        try:
            max_workers = int(requested)
        except (TypeError, ValueError):
            max_workers = None
    if max_workers is not None:
        target_total = min(target_total, max(0, int(max_workers)))
    actual_debate = trigger_required and (existing_live + target_total) >= min_debate_slots
    target = max(0, target_total - len(existing_roles))
    roles = [r for r in _role_ids(session["stage"], actual_debate, context)
             if r not in existing_roles][:target]
    made: list[dict] = []
    denied: list[dict] = []
    for role_id in roles:
        spec = _role(role_id)
        assignment_id = "A-" + uuid.uuid4().hex[:10]
        worker_id = f"{session_id}:{role_id}:{secrets.token_hex(3)}"
        task_leaf = f"{session['task_id']}:{role_id}"
        lease_id = None
        state = "planned"
        if reserve:
            lease = governor.acquire_research(
                conn, worker_id, task_leaf,
                metadata={"conclave_session": session_id, "assignment_id": assignment_id,
                          "role_id": role_id, "zone": spec["zone"]}, config=config,
            )
            if not lease.get("ok"):
                denied.append({"role_id": role_id, "reason": lease.get("reason"),
                               "plan": lease.get("plan")})
                continue
            lease_id = lease["lease_id"]
            state = "reserved"
        now = core.iso()
        conn.execute(
            "INSERT INTO conclave_assignments "
            "(assignment_id,session_id,task_id,worker_id,role_id,persona,zone,protocol,state,lease_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (assignment_id, session_id, task_leaf, worker_id, role_id, spec["nickname"],
             spec["zone"], spec["protocol"], state, lease_id, now, now),
        )
        # acquire_research uses BEGIN IMMEDIATE. Commit the assignment row
        # before asking for the next slot, otherwise the second reservation
        # would collide with this connection's pending INSERT.
        conn.commit()
        made.append({"assignment_id": assignment_id, "session_id": session_id,
                     "task_id": task_leaf, "worker_id": worker_id, "role_id": role_id,
                     "persona": spec["nickname"], "zone": spec["zone"],
                     "state": state, "lease_id": lease_id})
    conn.execute(
        "UPDATE conclave_sessions SET status=?, stage=?, updated_at=? WHERE session_id=?",
        ("awaiting_reports" if made else "open", "critique" if actual_debate else session["stage"],
         core.iso(), session_id),
    )
    conn.commit()
    core.log_event(conn, "conclave.assign", None, session_id=session_id,
                   assignments=made, denied=denied, reserve=reserve)
    existing_data = [
        {"assignment_id": row["assignment_id"], "session_id": row["session_id"],
         "task_id": row["task_id"], "worker_id": row["worker_id"],
         "role_id": row["role_id"], "persona": row["persona"], "zone": row["zone"],
         "state": row["state"], "lease_id": row["lease_id"]}
        for row in existing
    ]
    all_assignments = existing_data + made
    return {"ok": bool(all_assignments) or (not reserve and target == 0),
            "idempotent": bool(existing_data) and not made, "session_id": session_id,
            "assignments": all_assignments, "denied": denied, "debate": actual_debate,
            "admission": governor.plan(conn, config, requested_mode="auto")}


def brief(conn, session_id: str, assignment_id: str,
          config: dict | None = None, with_nudge: bool = True) -> dict:
    """Build the bounded native-child brief; no hidden reasoning is requested."""
    config = config if config is not None else core.load_config()
    session = _session(conn, session_id)
    assignment = _assignment(conn, assignment_id)
    if session is None or assignment is None or assignment["session_id"] != session_id:
        return {"ok": False, "reason": "session/assignment mismatch"}
    spec = _role(assignment["role_id"])
    context = _decode(session["context"], {})
    reminder = choose_nudge(
        conn, session_id, assignment_id, context, config=config
    ) if with_nudge else None
    return {
        "ok": True,
        "session_id": session_id,
        "assignment_id": assignment_id,
        "task_id": assignment["task_id"],
        "worker_id": assignment["worker_id"],
        "persona": {
            "nickname": spec["nickname"], "icon": spec["icon"],
            "zone": spec["zone"], "stance": spec["stance"], "style": spec["style"],
            "sample_lines": list(spec["sample_lines"]),
        },
        "internal_protocol": {
            "reasoning_language": "en",
            "text": spec["protocol"],
            "guardrail": (
                "Do not emit chain-of-thought. Return only concise evidence-backed "
                "rationale, uncertainty and next action. Keep the child leaf flat."
            ),
            "optional_reminder": None if reminder is None else reminder["text"],
        },
        "public_protocol": {
            "language": "ru",
            "max_chars": _int_cfg("max_visible_chars", 720, config),
            "voice": "punk tabloid + corporate obituary + dry stand-up",
            "banter": _truthy(_cfg("allow_banter", True, config)),
            "profanity": _truthy(_cfg("allow_profanity", True, config)),
            "rule": (
                "Mock the assumption, not a protected trait or private person. "
                "A joke may decorate a datum; it may not replace one."
            ),
            "format": "short Russian message: POSITION / EVIDENCE / OBJECTION / NEXT",
        },
        "mission_context": _redact_context(context),
        "challenge_templates": _template_plan(session["stage"], context,
                                                session["stage"] in ("critique", "falsification")),
        "nudge": reminder,
        "report_contract": {
            "task_id": assignment["task_id"],
            "status": "completed|no_finding|blocked|paused|failed",
            "claims": [], "evidence_refs": [], "sources": [], "confidence": 0.0,
            "duplicate_of": None, "recommended_next_action": "",
            "changed_files": [], "resource_usage": {}, "failure_reason": None,
        },
        "resource_rule": "heartbeat lease; checkpoint before pause; release after report",
    }


def _telegram_message_id(result: dict | None) -> int | None:
    if not isinstance(result, dict):
        return None
    message = result.get("result")
    if not isinstance(message, dict):
        return None
    try:
        return int(message.get("message_id"))
    except (TypeError, ValueError):
        return None


def _thread_for(audience: str, explicit: str | None = None) -> str | None:
    if explicit:
        return str(explicit)
    core.load_env()
    names = {
        "task": "TELEGRAM_CONCLAVE_THREAD_ID",
        "debate": "TELEGRAM_DEBATE_THREAD_ID",
        "client": "TELEGRAM_CLIENT_THREAD_ID",
    }
    value = os.environ.get(names.get(audience, "TELEGRAM_CONCLAVE_THREAD_ID"), "").strip()
    if value:
        return value
    return os.environ.get("TELEGRAM_CRON_THREAD_ID", "").strip() or None


def _display_prefix(assignment: Any, kind: str, audience: str, round_no: int) -> str:
    if assignment is None:
        nickname, icon = "Шеф", "🧭"
    else:
        spec = _role(assignment["role_id"])
        nickname, icon = spec["nickname"], spec["icon"]
    label = {"critique": "КРИТИКА", "defense": "ЗАЩИТА", "rebuttal": "ОТВЕТ",
             "decision": "РЕШЕНИЕ", "nudge": "НАПОМИНАНИЕ", "comment": "КОММЕНТ",
             "analysis": "АНАЛИЗ", "claim": "ТЕЗИС", "system": "СИСТЕМА",
             "brief": "БРИФ"}.get(kind, kind.upper())
    target = {"task": "задача", "client": "заказчик", "debate": "дебаты"}.get(audience, audience)
    return f"{icon} @{nickname} · R{max(0, int(round_no))} · {label} · {target}"


def _phrase_candidates(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    tags = _context_tags(context)
    candidates = [p for p in PHRASES if tags.intersection(p["tags"])]
    return candidates or list(PHRASES)


def choose_nudge(conn, session_id: str, assignment_id: str | None = None,
                 context: dict[str, Any] | None = None,
                 force: bool = False, config: dict | None = None) -> dict | None:
    """Select/log an occasional reminder; effectiveness is measured later."""
    config = config if config is not None else core.load_config()
    if not enabled(config):
        return None
    nudge_probability = max(0.0, min(1.0, _float_cfg("nudge_probability", 0.18, config)))
    if not force and _RNG.random() >= nudge_probability:
        return None
    role_id = None
    if assignment_id:
        row = _assignment(conn, assignment_id)
        role_id = row["role_id"] if row is not None else None
    cooldown = max(0, _int_cfg("nudge_cooldown_seconds", 600, config))
    if not force and role_id:
        recent = conn.execute(
            "SELECT selected_at FROM conclave_phrase_events WHERE session_id=? AND role_id=? "
            "ORDER BY event_id DESC LIMIT 1", (session_id, role_id),
        ).fetchone()
        if recent is not None:
            dt = core.parse_iso(recent["selected_at"])
            if dt and (core.now() - dt).total_seconds() < cooldown:
                return None
    phrase = dict(_RNG.choice(_phrase_candidates(context)))
    now = core.iso()
    conn.execute(
        "INSERT INTO conclave_phrase_events "
        "(phrase_id,session_id,assignment_id,role_id,context,prior_effectiveness,target_positive_effect,selected_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (phrase["id"], session_id, assignment_id, role_id,
         json.dumps(_redact_context(context or {}), ensure_ascii=False), phrase["prior_effectiveness"],
         phrase["target_positive_effect"], now),
    )
    conn.commit()
    phrase["selected_at"] = now
    phrase["assignment_id"] = assignment_id
    return phrase


def post_message(conn, session_id: str, text: str, audience: str = "task",
                 kind: str = "analysis", round_no: int = 0,
                 assignment_id: str | None = None, worker_id: str | None = None,
                 reply_to: int | None = None, thread_id: str | None = None,
                 nudge: bool = True, config: dict | None = None) -> dict:
    """Persist and send one short public message. Internal reasoning is excluded."""
    config = config if config is not None else core.load_config()
    if audience not in AUDIENCES:
        return {"ok": False, "reason": f"audience must be one of: {', '.join(AUDIENCES)}"}
    if kind not in MESSAGE_KINDS:
        return {"ok": False, "reason": f"kind must be one of: {', '.join(MESSAGE_KINDS)}"}
    session = _session(conn, session_id)
    if session is None:
        return {"ok": False, "reason": "conclave session not found"}
    try:
        round_no = int(round_no)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "round_no must be an integer"}
    if round_no < 0 or round_no > int(session["max_rounds"]):
        return {"ok": False, "reason": f"round_no must be in 0..{session['max_rounds']}"}
    assignment = _assignment(conn, assignment_id) if assignment_id else None
    if assignment is not None and assignment["session_id"] != session_id:
        return {"ok": False, "reason": "assignment belongs to another session"}
    if assignment is not None:
        worker_id = worker_id or assignment["worker_id"]
    public = _safe_public(text, config)
    context = _decode(session["context"], {})
    phrase = None
    if nudge and kind in ("analysis", "critique", "defense", "rebuttal"):
        phrase = choose_nudge(conn, session_id, assignment_id, context, config=config)
        if phrase:
            public = f"🧷 {phrase['text']}\n{public}"
            public = _safe_public(public, config)

    internal_reply = None
    if reply_to is not None:
        try:
            internal_reply = int(reply_to)
        except (TypeError, ValueError):
            internal_reply = None
    telegram_reply = None
    if internal_reply is not None:
        row = conn.execute(
            "SELECT telegram_message_id FROM conclave_messages WHERE message_id=?",
            (internal_reply,),
        ).fetchone()
        if row is not None:
            telegram_reply = row["telegram_message_id"]

    rendered = _display_prefix(assignment, kind, audience, round_no) + "\n" + public
    telegram: dict[str, Any]
    try:
        telegram = tg.send(rendered, thread_id=_thread_for(audience, thread_id),
                           markdown=False, reply_to_message_id=telegram_reply)
    except SystemExit as exc:
        # State is still durable when Telegram is not configured in a test or
        # during offline operation. The next transcript --send can replay it.
        telegram = {"ok": False, "error": f"telegram configuration: {exc}"}
    except (OSError, RuntimeError) as exc:
        telegram = {"ok": False, "error": str(exc)}

    conn.execute(
        "INSERT INTO conclave_messages "
        "(session_id,assignment_id,worker_id,role_id,audience,kind,text,language,round_no,"
        " reply_to_message_id,nudge_phrase_id,telegram_message_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, assignment_id, worker_id,
         assignment["role_id"] if assignment is not None else None,
         audience, kind, public, "ru", max(0, int(round_no)), internal_reply,
         phrase["id"] if phrase else None, _telegram_message_id(telegram), core.iso()),
    )
    message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    next_status = "debating" if kind in ("critique", "defense", "rebuttal") else session["status"]
    conn.execute("UPDATE conclave_sessions SET status=?, updated_at=? WHERE session_id=?",
                 (next_status, core.iso(), session_id))
    conn.commit()
    core.log_event(conn, "conclave.message", None, session_id=session_id,
                   message_id=message_id, audience=audience, message_kind=kind,
                   telegram_ok=bool(telegram.get("ok")))
    return {"ok": True, "message_id": message_id, "session_id": session_id,
            "audience": audience, "kind": kind, "text": public,
            "nudge_phrase_id": phrase["id"] if phrase else None,
            "telegram": telegram}


def speak(conn, session_id: str, assignment_id: str | None,
          task_text: str, client_text: str | None = None, kind: str = "analysis",
          round_no: int = 0, force_client: bool = False,
          reply_to: int | None = None, config: dict | None = None) -> dict:
    """Send a task note and, probabilistically, a separate client-side comment."""
    config = config if config is not None else core.load_config()
    task = post_message(conn, session_id, task_text, "task", kind, round_no,
                         assignment_id, reply_to=reply_to, nudge=True, config=config)
    if not task.get("ok"):
        return task
    client_probability = max(0.0, min(1.0, _float_cfg("client_comment_probability", 0.35, config)))
    due = bool(client_text) and (
        force_client or _RNG.random() < client_probability
    )
    client = None
    if due:
        client = post_message(conn, session_id, str(client_text), "client", "comment",
                              round_no, assignment_id, nudge=False, config=config)
    return {"ok": True, "task": task, "client": client, "client_due": due}


def receive_report(conn, session_id: str, assignment_id: str, path: str,
                   config: dict | None = None) -> dict:
    """Attach a validated governor report and release only the resource lease."""
    config = config if config is not None else core.load_config()
    assignment = _assignment(conn, assignment_id)
    session = _session(conn, session_id)
    if assignment is None or session is None or assignment["session_id"] != session_id:
        return {"ok": False, "reason": "session/assignment mismatch"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "reason": str(exc)}
    if not isinstance(payload, dict) or payload.get("task_id") != assignment["task_id"]:
        return {"ok": False, "reason": "report task_id does not match assignment task_id"}
    result = governor.record_report(conn, path, assignment["worker_id"])
    report_id = result.get("report_id")
    if result.get("ok") and report_id:
        conn.execute(
            "UPDATE conclave_assignments SET state=?, report_id=?, updated_at=? WHERE assignment_id=?",
            ("reported" if result.get("valid") else "report_invalid", report_id, core.iso(), assignment_id),
        )
        if assignment["lease_id"]:
            governor.release(conn, assignment["lease_id"], "child report received; parent review pending")
        pending = conn.execute(
            "SELECT COUNT(*) FROM conclave_assignments WHERE session_id=? AND state!='reported'",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE conclave_sessions SET status=?, updated_at=? WHERE session_id=?",
            ("awaiting_decision" if result.get("valid") and pending == 0 else "awaiting_reports",
             core.iso(), session_id),
        )
        conn.commit()
    reports = _session_reports(conn, session_id)
    trigger = detect_triggers({**(_decode(session["context"], {}) or {}), "reports": reports}, config)
    return {**result, "session_id": session_id, "assignment_id": assignment_id,
            "trigger": trigger, "reports": len(reports),
            "review_pending": True, "scientific_state_changed": False}


def _session_reports(conn, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT r.payload FROM governor_reports r JOIN conclave_assignments a "
        "ON a.report_id=r.report_id WHERE a.session_id=? ORDER BY r.report_id",
        (session_id,),
    ).fetchall()
    return [payload for row in rows
            if isinstance((payload := _decode(row["payload"], {})), dict)]


def transcript(conn, session_id: str, limit: int = 80) -> dict:
    session = _session(conn, session_id) if session_id else _latest_session(conn)
    if session is None:
        return {"ok": True, "empty": True,
                "text": "🎭 Conclave: открытых комнат нет — персонажи молчат, GPU не плачет."}
    session_id = session["session_id"]
    limit = max(1, min(300, int(limit)))
    rows = conn.execute(
        "SELECT m.*, a.persona FROM conclave_messages m LEFT JOIN conclave_assignments a "
        "ON a.assignment_id=m.assignment_id WHERE m.session_id=? ORDER BY m.message_id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    rows = list(reversed(rows))
    lines = [f"🎭 {session['title']} · {session['session_id']} · {session['status']}"]
    for row in rows:
        role = row["persona"] or "Шеф"
        lines.append(f"{role} [{row['kind']}/{row['audience']}/R{row['round_no']}]: {row['text']}")
    if not rows:
        lines.append("(чат пуст — персонажи ещё не успели устроить научную драку)")
    return {"ok": True, "session_id": session_id, "status": session["status"],
            "messages": [dict(row) for row in rows], "text": "\n".join(lines)}


def session_status(conn, session_id: str, config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    session = _session(conn, session_id) if session_id else _latest_session(conn)
    if session is None:
        return {"ok": False, "reason": "conclave session not found"}
    session_id = session["session_id"]
    assignments = [dict(row) for row in conn.execute(
        "SELECT assignment_id,task_id,worker_id,role_id,persona,zone,state,lease_id,report_id "
        "FROM conclave_assignments WHERE session_id=? ORDER BY created_at", (session_id,)
    ).fetchall()]
    messages = conn.execute(
        "SELECT COUNT(*) FROM conclave_messages WHERE session_id=?", (session_id,)
    ).fetchone()[0]
    return {"ok": True, "session": dict(session), "context": _decode(session["context"], {}),
            "assignments": assignments, "message_count": messages,
            "reports": len(_session_reports(conn, session_id)),
            "admission": governor.plan(conn, config, requested_mode="auto")}


def close_session(conn, session_id: str, decision: str = "",
                  decision_role: str = "parent", force: bool = False,
                  config: dict | None = None) -> dict:
    config = config if config is not None else core.load_config()
    session = _session(conn, session_id)
    if session is None:
        return {"ok": False, "reason": "conclave session not found"}
    if session["status"] == "closed":
        return {"ok": True, "idempotent": True, "session_id": session_id}
    pending = conn.execute(
        "SELECT * FROM conclave_assignments WHERE session_id=? AND state!='reported'",
        (session_id,),
    ).fetchall()
    active = [row for row in pending if row["state"] in ("reserved", "planned")]
    if pending and not force:
        return {"ok": False,
                "reason": "reports/assignments still pending; receive valid reports or use --force",
                "active_assignments": [row["assignment_id"] for row in pending]}
    released = []
    for row in active:
        if row["lease_id"]:
            governor.release(conn, row["lease_id"], "conclave closed")
            released.append(row["lease_id"])
        conn.execute(
            "UPDATE conclave_assignments SET state='released', updated_at=? WHERE assignment_id=?",
            (core.iso(), row["assignment_id"]),
        )
    conn.execute(
        "UPDATE conclave_sessions SET status='closed', decision=?, decision_role=?, closed_at=?, updated_at=? "
        "WHERE session_id=?",
        (_safe_public(decision, config) if decision else "", decision_role, core.iso(), core.iso(), session_id),
    )
    conn.commit()
    core.log_event(conn, "conclave.close", None, session_id=session_id,
                   decision_role=decision_role, released=released)
    return {"ok": True, "session_id": session_id, "status": "closed", "released": released,
            "scientific_state_changed": False,
            "note": "Decision record only; parent must use hypo/verdict tools for scientific state"}


def mark_phrase_outcome(conn, session_id: str, phrase_id: str,
                        outcome: str, assignment_id: str | None = None) -> dict:
    if outcome not in OUTCOMES:
        return {"ok": False, "reason": f"outcome must be one of: {', '.join(OUTCOMES)}"}
    if assignment_id:
        row = conn.execute(
            "SELECT event_id FROM conclave_phrase_events WHERE session_id=? AND phrase_id=? "
            "AND assignment_id=? AND outcome IS NULL ORDER BY event_id DESC LIMIT 1",
            (session_id, phrase_id, assignment_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT event_id FROM conclave_phrase_events WHERE session_id=? AND phrase_id=? "
            "AND outcome IS NULL ORDER BY event_id DESC LIMIT 1", (session_id, phrase_id),
        ).fetchone()
    if row is None:
        return {"ok": False, "reason": "unresolved phrase exposure not found"}
    conn.execute(
        "UPDATE conclave_phrase_events SET outcome=?, outcome_at=? WHERE event_id=?",
        (outcome, core.iso(), row["event_id"]),
    )
    conn.commit()
    return {"ok": True, "event_id": row["event_id"], "phrase_id": phrase_id,
            "outcome": outcome}


def cast() -> dict:
    """Return the fixed cast so the parent/user can audit who does what."""
    return {
        "parent": {"nickname": "Шеф", "zone": "governance",
                    "responsibility": "resource gate, task assignment, report review and adjudication"},
        "roles": [
            {"id": role_id, "nickname": spec["nickname"], "zone": spec["zone"],
             "stance": spec["stance"], "style": spec["style"],
             "sample_lines": list(spec["sample_lines"])}
            for role_id, spec in ROLE_SPECS.items()
        ],
        "rule": "character is a communication contract; evidence still outranks performance",
    }


def phrase_stats(conn) -> dict:
    rows = conn.execute(
        "SELECT phrase_id, prior_effectiveness, target_positive_effect, outcome "
        "FROM conclave_phrase_events ORDER BY phrase_id"
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = grouped.setdefault(row["phrase_id"], {
            "phrase_id": row["phrase_id"], "exposures": 0, "resolved": 0,
            "positive": 0, "neutral": 0, "negative": 0,
            "prior_effectiveness": row["prior_effectiveness"],
            "target_positive_effect": row["target_positive_effect"],
        })
        item["exposures"] += 1
        outcome = row["outcome"]
        if outcome in OUTCOMES:
            item["resolved"] += 1
            item[outcome] += 1
    for item in grouped.values():
        item["measured_positive_rate"] = (
            round(item["positive"] / item["resolved"], 3) if item["resolved"] else None
        )
        item["prior_is_not_measurement"] = True
    return {"phrases": list(grouped.values()), "note": "95%/90% values are priors; measure outcomes before changing them"}


def watch(conn, send: bool = False, config: dict | None = None) -> dict:
    """CPU-only heartbeat for open rooms; it never creates a child."""
    config = config if config is not None else core.load_config()
    rows = conn.execute(
        "SELECT session_id,title,status,updated_at FROM conclave_sessions "
        "WHERE status!='closed' ORDER BY updated_at"
    ).fetchall()
    idle_seconds = max(60, _int_cfg("watch_idle_seconds", 1800, config))
    rooms = []
    for row in rows:
        dt = core.parse_iso(row["updated_at"])
        idle = (core.now() - dt).total_seconds() if dt else 0
        rooms.append({**dict(row), "idle_seconds": round(max(0, idle)),
                      "stalled": idle >= idle_seconds})
    text_lines = [f"🎭 Conclave watch: {len(rooms)} открытых комнат"]
    for room in rooms:
        marker = " ⚠️ stalled" if room["stalled"] else ""
        text_lines.append(f"• {room['session_id']} {room['status']} {core.human_delta(room['idle_seconds'])}{marker}")
    text = "\n".join(text_lines)
    telegram = None
    send_due = False
    if send and rooms:
        cooldown = max(60, _int_cfg("watch_send_cooldown_seconds", 1800, config))
        last = core.parse_iso(core.setting(conn, "conclave.last_watch_sent"))
        send_due = not last or (core.now() - last).total_seconds() >= cooldown
        if send_due:
            try:
                telegram = tg.send(text, thread_id=_thread_for("debate"), markdown=False, silent=True)
            except SystemExit as exc:
                telegram = {"ok": False, "error": str(exc)}
            if telegram and telegram.get("ok"):
                core.set_setting(conn, "conclave.last_watch_sent", core.iso())
        else:
            telegram = {"ok": True, "skipped": True, "reason": "watch cooldown"}
    return {"ok": True, "rooms": rooms, "text": text, "send_due": send_due, "telegram": telegram}


def _context_file(path: str | None) -> dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        core.fail(f"context JSON error: {exc}")
    return {}


def _text(data: dict) -> str:
    if data.get("reason") and not data.get("opened", True):
        return str(data["reason"])
    if "text" in data and isinstance(data["text"], str):
        return data["text"]
    if "roles" in data:
        roles = ", ".join(f"@{r.get('nickname')} [{r.get('zone')}]" for r in data["roles"])
        return f"debate={data.get('debate_possible')} slots={data.get('available_slots')}\n{roles or 'ролей нет — ресурсный начальник сказал нет'}"
    if data.get("errors"):
        return "\n".join(str(e) for e in data["errors"])
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def main(argv: list[str]) -> int:
    core.load_env()
    config = core.load_config()
    as_json = core.wants_json(argv)
    cmd = argv[1] if len(argv) > 1 else "watch"
    conn = core.db()

    if cmd in ("roles", "cast"):
        data = cast()
        core.emit(data, as_json, _text(data))
        return 0
    if cmd == "plan":
        task_id = core.arg(argv, "task-id") or (argv[2] if len(argv) > 2 and not argv[2].startswith("--") else "")
        stage = core.arg(argv, "stage", "research")
        context = _context_file(core.arg(argv, "context-file"))
        data = role_plan(conn, task_id, stage, context, config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok", True) else 1
    if cmd == "open":
        task_id = core.arg(argv, "task-id") or (argv[2] if len(argv) > 2 and not argv[2].startswith("--") else "")
        title = core.arg(argv, "title", task_id)
        stage = core.arg(argv, "stage", "critique")
        context = _context_file(core.arg(argv, "context-file"))
        data = open_session(conn, task_id, title, stage, context, core.flag(argv, "force"), config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "assign":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        workers = core.arg(argv, "workers")
        data = assign(conn, session_id, reserve=not core.flag(argv, "no-reserve"),
                      max_workers=None if workers is None else int(workers), config=config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "brief":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        assignment_id = core.arg(argv, "assignment") or (argv[3] if len(argv) > 3 else "")
        data = brief(conn, session_id, assignment_id, config,
                      with_nudge=not core.flag(argv, "no-nudge"))
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "report":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        assignment_id = core.arg(argv, "assignment") or (argv[3] if len(argv) > 3 else "")
        path = core.arg(argv, "file") or (argv[4] if len(argv) > 4 else "")
        data = receive_report(conn, session_id, assignment_id, path, config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("valid", data.get("ok", False)) else 1
    if cmd in ("speak", "post"):
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        assignment_id = core.arg(argv, "assignment")
        task_text = core.arg(argv, "task") or core.arg(argv, "text", "")
        client_text = core.arg(argv, "client")
        reply_to = core.arg(argv, "reply-to")
        data = speak(conn, session_id, assignment_id, task_text, client_text,
                     core.arg(argv, "kind", "analysis"), int(core.arg(argv, "round", 0)),
                     core.flag(argv, "force-client"),
                     None if reply_to is None else int(reply_to), config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "nudge":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        assignment_id = core.arg(argv, "assignment")
        context = _context_file(core.arg(argv, "context-file"))
        phrase = choose_nudge(conn, session_id, assignment_id, context, force=True, config=config)
        if phrase and core.flag(argv, "send"):
            post = post_message(conn, session_id, phrase["text"], "debate", "nudge", 0,
                                assignment_id, nudge=False, config=config)
            data = {"ok": True, "phrase": phrase, "post": post}
        else:
            data = {"ok": bool(phrase), "phrase": phrase,
                    "reason": None if phrase else "no phrase selected"}
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "transcript":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        data = transcript(conn, session_id, int(core.arg(argv, "limit", 80)))
        if data.get("ok") and core.flag(argv, "send"):
            try:
                data["telegram"] = tg.send(data["text"], thread_id=_thread_for("debate"), markdown=False)
            except SystemExit as exc:
                data["telegram"] = {"ok": False, "error": str(exc)}
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "status":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        data = session_status(conn, session_id, config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "close":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        data = close_session(conn, session_id, core.arg(argv, "decision", ""),
                             core.arg(argv, "decision-role", "parent"), core.flag(argv, "force"), config)
        if data.get("ok") and core.arg(argv, "decision") and core.flag(argv, "announce"):
            data["post"] = post_message(conn, session_id, core.arg(argv, "decision"),
                                         "debate", "decision", 0, nudge=False, config=config)
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd == "outcome":
        session_id = core.arg(argv, "session") or (argv[2] if len(argv) > 2 else "")
        phrase_id = core.arg(argv, "phrase") or (argv[3] if len(argv) > 3 else "")
        data = mark_phrase_outcome(conn, session_id, phrase_id, core.arg(argv, "outcome", "neutral"),
                                   core.arg(argv, "assignment"))
        core.emit(data, as_json, _text(data))
        return 0 if data.get("ok") else 1
    if cmd in ("phrase-stats", "phrases"):
        data = phrase_stats(conn)
        core.emit(data, as_json, _text(data))
        return 0
    if cmd == "watch":
        data = watch(conn, core.flag(argv, "send"), config)
        core.emit(data, as_json, _text(data))
        return 0

    core.fail(f"неизвестная команда conclave: {cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
