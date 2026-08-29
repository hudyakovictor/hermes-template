"""Mission-scoped state and SQLite persistence for Bottom Detection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import core


REGION_STATES = ("frontier", "active", "exhausted", "backtracked", "closed")
HYPOTHESIS_STATES = ("candidate", "evaluated", "promoted", "rejected", "archived")


def namespace_for(mission: str, domain: str) -> str:
    """Return a stable namespace so a changed mission never mixes state."""

    digest = hashlib.sha256(
        (domain.strip() + "\n" + mission.strip()).encode("utf-8")
    ).hexdigest()
    return digest[:20]


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_list(value: str, default: Optional[List[Any]] = None) -> List[Any]:
    loaded = _json_loads(value, default if default is not None else [])
    return loaded if isinstance(loaded, list) else list(default or [])


def _json_dict(value: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    loaded = _json_loads(value, default if default is not None else {})
    return loaded if isinstance(loaded, dict) else dict(default or {})


@dataclass
class Evidence:
    """A traceable observation attached to an exploratory hypothesis."""

    id: str
    candidate_id: str
    source: str
    claim: str
    kind: str = "literature"
    independent_key: str = ""
    strength: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=core.iso)


@dataclass
class Region:
    """A node in the search tree/frontier."""

    id: str
    name: str
    query: str
    parent_id: Optional[str] = None
    depth: int = 0
    status: str = "frontier"
    visits: int = 0
    signal_score: float = 0.0
    no_signal_streak: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=core.iso)
    updated_at: str = field(default_factory=core.iso)


@dataclass
class Hypothesis:
    """Candidate mechanism before promotion to the profile's main queue."""

    id: str
    region_id: str
    text: str
    mechanism: str = ""
    signal_sources: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    novelty_score: float = 0.0
    mechanism_score: float = 0.0
    experiment_score: float = 0.0
    commercial_score: float = 0.0
    decidability_score: float = 0.0
    priority: float = 0.0
    estimated_hours: float = 0.25
    forecast: Optional[float] = None
    status: str = "candidate"
    origin_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=core.iso)
    updated_at: str = field(default_factory=core.iso)


@dataclass
class SearchState:
    """Complete in-memory view of one mission/domain search namespace."""

    mission: str
    domain: str
    namespace: str
    regions: Dict[str, Region] = field(default_factory=dict)
    hypotheses: Dict[str, Hypothesis] = field(default_factory=dict)
    evidence: Dict[str, Evidence] = field(default_factory=dict)
    frontier: List[str] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    cost_usd: float = 0.0

    def add_history(self, event: str, **payload: Any) -> Dict[str, Any]:
        item = {
            "event": event,
            "iteration": self.iteration,
            "created_at": core.iso(),
            **payload,
        }
        self.history.append(item)
        return item

    def to_summary(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "domain": self.domain,
            "iteration": self.iteration,
            "cost_usd": round(self.cost_usd, 4),
            "regions": [asdict(r) for r in self.regions.values()],
            "hypotheses": [asdict(h) for h in self.hypotheses.values()],
            "evidence": [asdict(e) for e in self.evidence.values()],
            "frontier": list(self.frontier),
            "history_events": len(self.history),
        }


def load_state(conn: Any, mission: str, domain: str) -> SearchState:
    """Load only records belonging to the current mission namespace."""

    namespace = namespace_for(mission, domain)
    meta = {
        row["key"]: _json_loads(row["value"], row["value"])
        for row in conn.execute(
            "SELECT key, value FROM bd_meta WHERE namespace=?", (namespace,)
        ).fetchall()
    }
    state = SearchState(
        mission=mission,
        domain=domain,
        namespace=namespace,
        frontier=(
            list(meta.get("frontier", []))
            if isinstance(meta.get("frontier", []), list)
            else []
        ),
        iteration=int(meta.get("iteration", 0) or 0),
        cost_usd=float(meta.get("cost_usd", 0.0) or 0.0),
    )
    for row in conn.execute(
        "SELECT * FROM bd_regions WHERE namespace=?", (namespace,)
    ).fetchall():
        state.regions[row["id"]] = Region(
            id=row["id"],
            name=row["name"],
            query=row["query"],
            parent_id=row["parent_id"],
            depth=int(row["depth"]),
            status=row["status"],
            visits=int(row["visits"]),
            signal_score=float(row["signal_score"]),
            no_signal_streak=int(row["no_signal_streak"]),
            metadata=_json_dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    for row in conn.execute(
        "SELECT * FROM bd_hypotheses WHERE namespace=?", (namespace,)
    ).fetchall():
        state.hypotheses[row["id"]] = Hypothesis(
            id=row["id"],
            region_id=row["region_id"],
            text=row["text"],
            mechanism=row["mechanism"],
            status=row["status"],
            signal_sources=_json_list(row["signal_sources"]),
            evidence_ids=_json_list(row["evidence_ids"]),
            novelty_score=float(row["novelty_score"]),
            mechanism_score=float(row["mechanism_score"]),
            experiment_score=float(row["experiment_score"]),
            commercial_score=float(row["commercial_score"]),
            decidability_score=float(row["decidability_score"]),
            priority=float(row["priority"]),
            estimated_hours=float(row["estimated_hours"]),
            forecast=None if row["forecast"] is None else float(row["forecast"]),
            origin_id=row["origin_id"],
            metadata=_json_dict(row["metadata"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    for row in conn.execute(
        "SELECT * FROM bd_evidence WHERE namespace=?", (namespace,)
    ).fetchall():
        state.evidence[row["id"]] = Evidence(
            id=row["id"],
            candidate_id=row["candidate_id"],
            source=row["source"],
            claim=row["claim"],
            kind=row["kind"],
            independent_key=row["independent_key"],
            strength=float(row["strength"]),
            metadata=_json_dict(row["metadata"]),
            created_at=row["created_at"],
        )
    state.history = [
        {
            "history_id": row["history_id"],
            "run_id": row["run_id"],
            "iteration": row["iteration"],
            "event": row["event"],
            "region_id": row["region_id"],
            "hypothesis_id": row["hypothesis_id"],
            "payload": _json_dict(row["payload"]),
            "created_at": row["created_at"],
        }
        for row in conn.execute(
            "SELECT * FROM bd_history WHERE namespace=? ORDER BY history_id",
            (namespace,),
        ).fetchall()
    ]
    return state


def _upsert_meta(conn: Any, namespace: str, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO bd_meta(namespace,key,value,updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (namespace, key, _json_dump(value), core.iso()),
    )


def persist_state(conn: Any, state: SearchState) -> None:
    """Persist the materialized state; history is append-only via append_history."""

    _upsert_meta(conn, state.namespace, "mission", state.mission)
    _upsert_meta(conn, state.namespace, "domain", state.domain)
    _upsert_meta(conn, state.namespace, "frontier", state.frontier)
    _upsert_meta(conn, state.namespace, "iteration", state.iteration)
    _upsert_meta(conn, state.namespace, "cost_usd", state.cost_usd)
    for region in state.regions.values():
        conn.execute(
            "INSERT INTO bd_regions(namespace,id,parent_id,name,query,depth,status,"
            "visits,signal_score,no_signal_streak,metadata,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(namespace,id) DO UPDATE SET "
            "parent_id=excluded.parent_id,name=excluded.name,query=excluded.query,"
            "depth=excluded.depth,status=excluded.status,visits=excluded.visits,"
            "signal_score=excluded.signal_score,no_signal_streak=excluded.no_signal_streak,"
            "metadata=excluded.metadata,updated_at=excluded.updated_at",
            (
                state.namespace,
                region.id,
                region.parent_id,
                region.name,
                region.query,
                region.depth,
                region.status,
                region.visits,
                region.signal_score,
                region.no_signal_streak,
                _json_dump(region.metadata),
                region.created_at,
                region.updated_at,
            ),
        )
    for hypothesis in state.hypotheses.values():
        conn.execute(
            "INSERT INTO bd_hypotheses(namespace,id,region_id,text,mechanism,status,"
            "signal_sources,evidence_ids,novelty_score,mechanism_score,experiment_score,"
            "commercial_score,decidability_score,priority,estimated_hours,forecast,"
            "origin_id,metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace,id) DO UPDATE SET region_id=excluded.region_id,"
            "text=excluded.text,mechanism=excluded.mechanism,status=excluded.status,"
            "signal_sources=excluded.signal_sources,evidence_ids=excluded.evidence_ids,"
            "novelty_score=excluded.novelty_score,mechanism_score=excluded.mechanism_score,"
            "experiment_score=excluded.experiment_score,commercial_score=excluded.commercial_score,"
            "decidability_score=excluded.decidability_score,priority=excluded.priority,"
            "estimated_hours=excluded.estimated_hours,forecast=excluded.forecast,"
            "origin_id=excluded.origin_id,metadata=excluded.metadata,updated_at=excluded.updated_at",
            (
                state.namespace,
                hypothesis.id,
                hypothesis.region_id,
                hypothesis.text,
                hypothesis.mechanism,
                hypothesis.status,
                _json_dump(hypothesis.signal_sources),
                _json_dump(hypothesis.evidence_ids),
                hypothesis.novelty_score,
                hypothesis.mechanism_score,
                hypothesis.experiment_score,
                hypothesis.commercial_score,
                hypothesis.decidability_score,
                hypothesis.priority,
                hypothesis.estimated_hours,
                hypothesis.forecast,
                hypothesis.origin_id,
                _json_dump(hypothesis.metadata),
                hypothesis.created_at,
                hypothesis.updated_at,
            ),
        )
    for evidence in state.evidence.values():
        conn.execute(
            "INSERT INTO bd_evidence(namespace,id,candidate_id,source,claim,kind,"
            "independent_key,strength,metadata,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace,id) DO UPDATE SET candidate_id=excluded.candidate_id,"
            "source=excluded.source,claim=excluded.claim,kind=excluded.kind,"
            "independent_key=excluded.independent_key,strength=excluded.strength,"
            "metadata=excluded.metadata",
            (
                state.namespace,
                evidence.id,
                evidence.candidate_id,
                evidence.source,
                evidence.claim,
                evidence.kind,
                evidence.independent_key,
                evidence.strength,
                _json_dump(evidence.metadata),
                evidence.created_at,
            ),
        )
    conn.commit()


def append_history(
    conn: Any,
    state: SearchState,
    event: str,
    run_id: Optional[int] = None,
    region_id: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    **payload: Any,
) -> None:
    """Append an immutable event and mirror it in the in-memory history."""

    item = state.add_history(
        event,
        region_id=region_id,
        hypothesis_id=hypothesis_id,
        payload=payload,
    )
    conn.execute(
        "INSERT INTO bd_history(namespace,run_id,iteration,event,region_id,"
        "hypothesis_id,payload,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            state.namespace,
            run_id,
            state.iteration,
            event,
            region_id,
            hypothesis_id,
            json.dumps(payload, ensure_ascii=False, default=str),
            item["created_at"],
        ),
    )
    conn.commit()
