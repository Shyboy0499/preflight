"""World Model (Meterless), the services Preflight reads its facts from.

Follows engines/world-model/HOW_TO_USE_WORLD_MODEL.md in github.com/Meterless/Meterless.
Service names map to the documented contracts:

* WorldStoreService     - canonical store: entities, edges and the append-only
                          observation log that every fact traces back to (sections 2, 4).
* IngestPipelineService - the `IngestPipeline` contract: per-observation path
                          extract -> normalize -> resolve -> validate -> append -> project
                          (section 5), with stable hash IDs (section 3). Facts come
                          from Zetaris via the adapter, with the query as provenance.
* ContextBuilderService - the `ReadSurface` contract: projections plus
                          build_prompt_context() returning {text, provenance} (section 9).

The world model is a read cache for reasoning and explanation. The precondition
gate never reads from it; checks always query the data source directly.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from app.adapters.zetaris import DataSourceUnavailable, Query, QueryResult, SqlSource, UnsafeQuery
from app.models import ShiftRequest


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", s).strip()


def stable_entity_id(name: str, type_: str) -> str:
    h = hashlib.sha256(f"{type_}:{normalize_name(name)}".encode("utf-8")).hexdigest()[:16]
    return f"ent_{h}"


def stable_edge_id(src: str, type_: str, dst: str) -> str:
    return "edge_" + hashlib.sha256(f"{src}|{type_}|{dst}".encode()).hexdigest()[:16]


@dataclass
class Entity:
    id: str
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    traits: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    summary: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    mention_count: int = 0
    first_seen_at: int = 0
    last_seen_at: int = 0
    linked_ids: list[str] = field(default_factory=list)
    is_custom: bool = False
    imported_from: str | None = None  # observation id of the first supporting row
    observation_ids: list[str] = field(default_factory=list)  # every supporting row (provenance)
    discovered_at: int = 0
    state: str = "active"
    confidence: float = 1.0
    staleness: float = 0.0


@dataclass
class Edge:
    id: str
    from_id: str
    to_id: str
    type: str
    confidence: float
    observation_id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    id: str
    source: str
    query: str
    query_id: str
    sql: str
    row: dict[str, Any]
    fetched_at: str


class WorldStoreService:
    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.edges: dict[str, Edge] = {}
        self.observations: list[Observation] = []
        self.views: dict[str, Any] = {}

    def append_observation(self, result: QueryResult, row: dict[str, Any]) -> Observation:
        obs = Observation(
            id=f"obs_{len(self.observations) + 1:05d}_{result.query_id[:8]}",
            source=result.source,
            query=result.query,
            query_id=result.query_id,
            sql=result.sql,
            row=row,
            fetched_at=result.fetched_at,
        )
        self.observations.append(obs)
        return obs

    def upsert_entity(self, name: str, type_: str, obs: Observation, **properties: Any) -> Entity:
        eid = stable_entity_id(name, type_)
        ts = int(time.time() * 1000)
        ent = self.entities.get(eid)
        if ent is None:
            ent = Entity(id=eid, name=name, type=type_, first_seen_at=ts, discovered_at=ts)
            self.entities[eid] = ent
        ent.properties.update({k: v for k, v in properties.items() if v is not None})
        ent.mention_count += 1
        ent.last_seen_at = ts
        ent.imported_from = ent.imported_from or obs.id
        ent.observation_ids.append(obs.id)
        return ent

    def put_edge(self, src: Entity, type_: str, dst: Entity, obs: Observation, **properties: Any) -> Edge:
        edge = Edge(stable_edge_id(src.id, type_, dst.id), src.id, dst.id, type_, 1.0, obs.id, properties)
        self.edges[edge.id] = edge
        for a, b in ((src, dst), (dst, src)):
            if b.id not in a.linked_ids:
                a.linked_ids.append(b.id)
        return edge

    def entity(self, name: str, type_: str) -> Entity | None:
        return self.entities.get(stable_entity_id(name, type_))

    def observation(self, obs_id: str) -> Observation | None:
        return next((o for o in self.observations if o.id == obs_id), None)

    def edges_of(self, entity_id: str, type_: str | None = None) -> list[Edge]:
        return [e for e in self.edges.values() if (e.from_id == entity_id or e.to_id == entity_id) and (type_ is None or e.type == type_)]


# --- ingest --------------------------------------------------------------------

SCOPE_QUERIES: tuple[Query, ...] = (
    Query(
        "wm_shift_demand",
        """
        SELECT d.ward AS ward, w.ward_name AS ward_name, w.agency_allowed AS agency_allowed,
               d.shift_date AS shift_date, d.shift_type AS shift_type, d.patients AS patients,
               d.acuity AS acuity, d.required_nurses AS required_nurses, d.required_skill AS required_skill
        FROM {p}.demand d LEFT JOIN {p}.wards w ON w.ward = d.ward
        WHERE d.ward = :ward AND d.shift_date = :shift_date AND d.shift_type = :shift_type
        """,
        ("ward", "ward_name", "agency_allowed", "shift_date", "shift_type", "patients", "acuity", "required_nurses", "required_skill"),
    ),
    Query(
        "wm_staff_week_hours",
        """
        SELECT s.staff_id AS staff_id, s.name AS name, s.role AS role, s.home_ward AS home_ward,
               s.weekly_hour_cap AS weekly_hour_cap, COALESCE(SUM(r.hours), 0) AS week_hours
        FROM {p}.staff s
        LEFT JOIN {p}.roster r ON r.staff_id = s.staff_id AND r.shift_date >= :week_start AND r.shift_date <= :week_end
        GROUP BY s.staff_id, s.name, s.role, s.home_ward, s.weekly_hour_cap
        """,
        ("staff_id", "name", "role", "home_ward", "weekly_hour_cap", "week_hours"),
    ),
    Query(
        "wm_staff_skills",
        "SELECT k.staff_id AS staff_id, k.skill AS skill FROM {p}.staff_skills k",
        ("staff_id", "skill"),
    ),
    Query(
        "wm_shifts_on_date",
        """
        SELECT r.shift_id AS shift_id, r.staff_id AS staff_id, r.ward AS ward, r.shift_date AS shift_date,
               r.shift_type AS shift_type, r.start_ts AS start_ts, r.end_ts AS end_ts
        FROM {p}.roster r WHERE r.shift_date = :shift_date
        """,
        ("shift_id", "staff_id", "ward", "shift_date", "shift_type", "start_ts", "end_ts"),
    ),
    Query(
        "wm_leave_on_date",
        """
        SELECT l.staff_id AS staff_id, l.leave_type AS leave_type, l.start_date AS start_date,
               l.end_date AS end_date, l.status AS status
        FROM {p}.leave l WHERE l.start_date <= :shift_date AND l.end_date >= :shift_date
        """,
        ("staff_id", "leave_type", "start_date", "end_date", "status"),
    ),
)

REQUIRED_FIELDS = {q.name: q.columns[:1] for q in SCOPE_QUERIES}


@dataclass
class IngestReport:
    queries_run: int = 0
    observations: int = 0
    entities_upserted: int = 0
    edges_upserted: int = 0
    rejected_rows: int = 0
    errors: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IngestPipelineService:
    def __init__(self, source: SqlSource, store: WorldStoreService):
        self.source = source
        self.store = store
        self._resolvers: dict[str, Callable[[Observation, dict[str, Any]], int]] = {
            "wm_shift_demand": self._resolve_demand,
            "wm_staff_week_hours": self._resolve_staff,
            "wm_staff_skills": self._resolve_skill,
            "wm_shifts_on_date": self._resolve_shift,
            "wm_leave_on_date": self._resolve_leave,
        }

    def ingest_scope(self, request: ShiftRequest, params: dict[str, Any]) -> IngestReport:
        report = IngestReport()
        for query in SCOPE_QUERIES:
            # extract
            try:
                result = self.source.run(query, params)
            except (DataSourceUnavailable, UnsafeQuery) as exc:
                report.errors.append(f"{query.name}: {exc}")
                continue
            report.queries_run += 1
            report.provenance.append(result.provenance())
            for raw in result.rows:
                row = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}  # normalize
                if any(row.get(f) in (None, "") for f in REQUIRED_FIELDS[query.name]):  # validate
                    report.rejected_rows += 1
                    continue
                obs = self.store.append_observation(result, row)  # append
                report.observations += 1
                report.edges_upserted += self._resolvers[query.name](obs, row)  # resolve + edges
        report.entities_upserted = len(self.store.entities)
        self._project(request)
        return report

    # resolve: rows -> entities and edges with stable ids
    def _resolve_demand(self, obs: Observation, r: dict[str, Any]) -> int:
        ward = self.store.upsert_entity(r["ward"], "ward", obs, ward_name=r["ward_name"], agency_allowed=r["agency_allowed"])
        shift = self.store.upsert_entity(
            f"{r['ward']}|{r['shift_date']}|{r['shift_type']}", "shift_demand", obs,
            patients=r["patients"], acuity=r["acuity"], required_nurses=r["required_nurses"], required_skill=r["required_skill"],
        )
        self.store.put_edge(shift, "demand_on_ward", ward, obs)
        return 1

    def _resolve_staff(self, obs: Observation, r: dict[str, Any]) -> int:
        staff = self.store.upsert_entity(
            r["staff_id"], "staff", obs, display_name=r["name"], role=r["role"], home_ward=r["home_ward"],
            weekly_hour_cap=r["weekly_hour_cap"], week_hours=r["week_hours"],
        )
        ward = self.store.upsert_entity(r["home_ward"], "ward", obs)
        self.store.put_edge(staff, "home_ward", ward, obs)
        return 1

    def _resolve_skill(self, obs: Observation, r: dict[str, Any]) -> int:
        staff = self.store.upsert_entity(r["staff_id"], "staff", obs)
        skill = self.store.upsert_entity(r["skill"], "skill", obs)
        self.store.put_edge(staff, "has_skill", skill, obs)
        return 1

    def _resolve_shift(self, obs: Observation, r: dict[str, Any]) -> int:
        staff = self.store.upsert_entity(r["staff_id"], "staff", obs)
        ward = self.store.upsert_entity(r["ward"], "ward", obs)
        self.store.put_edge(
            staff, "rostered_on", ward, obs,
            shift_id=r["shift_id"], shift_date=r["shift_date"], shift_type=r["shift_type"], start_ts=r["start_ts"], end_ts=r["end_ts"],
        )
        return 1

    def _resolve_leave(self, obs: Observation, r: dict[str, Any]) -> int:
        staff = self.store.upsert_entity(r["staff_id"], "staff", obs)
        leave = self.store.upsert_entity(f"{r['staff_id']}|{r['leave_type']}|{r['start_date']}", "leave", obs, **{k: r[k] for k in ("leave_type", "start_date", "end_date", "status")})
        self.store.put_edge(staff, "on_leave", leave, obs)
        return 1

    # project: rebuild the derived view the planner reads
    def _project(self, request: ShiftRequest) -> None:
        s = self.store
        date_s = request.shift_date.isoformat()
        demand = s.entity(f"{request.ward}|{date_s}|{request.shift_type}", "shift_demand")
        required_skill = demand.properties.get("required_skill") if demand else None
        pool = []
        for ent in s.entities.values():
            if ent.type != "staff" or "role" not in ent.properties:
                continue
            skills = sorted(s.entities[e.to_id].name for e in s.edges_of(ent.id, "has_skill"))
            shifts = [(s.entities[e.to_id].name, e.properties) for e in s.edges_of(ent.id, "rostered_on")]
            leave = [s.entities[e.to_id].properties for e in s.edges_of(ent.id, "on_leave")]
            pool.append({
                "staff_id": ent.name,
                "role": ent.properties.get("role"),
                "home_ward": ent.properties.get("home_ward"),
                "skills": skills,
                "week_hours": ent.properties.get("week_hours"),
                "weekly_hour_cap": ent.properties.get("weekly_hour_cap"),
                "shifts_on_date": [f"{ward} {x['shift_type']} {x['start_ts']} to {x['end_ts']}" for ward, x in shifts],
                "already_on_this_shift": any(
                    ward == request.ward and x["shift_type"] == request.shift_type for ward, x in shifts
                ),
                "leave_on_date": [f"{x['leave_type']} {x['status']} {x['start_date']} to {x['end_date']}" for x in leave],
                "has_required_skill": (required_skill in skills) if required_skill else None,
                "observations": list(ent.observation_ids),
            })
        s.views[f"staff_pool:{request.ward}:{date_s}:{request.shift_type}"] = sorted(pool, key=lambda p: p["staff_id"])
        s.views[f"demand:{request.ward}:{date_s}:{request.shift_type}"] = (
            {"ward": request.ward, **demand.properties, "observations": list(demand.observation_ids)} if demand else None
        )


# --- read surface ----------------------------------------------------------------


class ContextBuilderService:
    def __init__(self, store: WorldStoreService):
        self.store = store

    def demand(self, request: ShiftRequest) -> dict[str, Any] | None:
        return self.store.views.get(f"demand:{request.ward}:{request.shift_date.isoformat()}:{request.shift_type}")

    def staff_pool(self, request: ShiftRequest) -> list[dict[str, Any]]:
        return self.store.views.get(f"staff_pool:{request.ward}:{request.shift_date.isoformat()}:{request.shift_type}", [])

    def display_name(self, staff_id: str) -> str:
        ent = self.store.entity(staff_id, "staff")
        return (ent.properties.get("display_name") if ent else None) or staff_id

    def build_prompt_context(self, request: ShiftRequest, limit: int = 20) -> dict[str, Any]:
        """Prompt-ready facts plus the observations they came from.

        Staff appear by ID only; names stay local and are never sent to the LLM.
        """
        demand = self.demand(request)
        pool = self.staff_pool(request)[:limit]
        lines = [f"Shift: {request.label()}"]
        if demand:
            lines.append(
                f"Demand: {demand.get('required_nurses')} nurses with {demand.get('required_skill')}, "
                f"{demand.get('patients')} patients, acuity {demand.get('acuity')}, agency_allowed={demand.get('agency_allowed')}"
            )
        else:
            lines.append("Demand: no demand record found for this ward and shift")
        for p in pool:
            lines.append(
                f"Staff {p['staff_id']}: home {p['home_ward']}, skills {', '.join(p['skills'])}, "
                f"{p['week_hours']}h of {p['weekly_hour_cap']}h this week"
                + (f", shifts that day: {'; '.join(p['shifts_on_date'])}" if p["shifts_on_date"] else "")
                + (f", leave: {'; '.join(p['leave_on_date'])}" if p["leave_on_date"] else "")
            )
        obs_ids = {o for p in pool for o in p["observations"]} | set(demand["observations"] if demand else [])
        provenance = []
        for oid in sorted(obs_ids):
            o = self.store.observation(oid)
            if o:
                provenance.append({"kind": "observation", "id": o.id, "source": o.source, "query": o.query, "query_id": o.query_id})
        return {"text": "\n".join(lines), "provenance": provenance}
