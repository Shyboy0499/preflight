"""The four agent roles as code. See app/roles.py for what each may and may not do.

PreflightAgent (app/agent.py) owns one of each and runs the loop:
  Planner.propose -> Verifier.check each -> (gap left) Planner.replan with refusals
  -> Verifier.check each -> backfill -> agency -> Investigator if nothing passed;
  Memory recalls before planning and learns / gives feedback after verification.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.actions.library import LIBRARY
from app.adapters.llm import Generator
from app.adapters.zetaris import SqlSource
from app.meterless.hmem import (
    Entity,
    Memory,
    MemoryRetrievalService,
    MemoryStoreService,
    RetrievalQuery,
    RetrievalResult,
    format_reinjection,
    now_ms,
)
from app.models import Candidate, ShiftRequest
from app.planner import backfill, propose, replan
from app.preconditions.gate import GateResult, evaluate
from app.request_parser import parse_request

INVESTIGATE_SYSTEM = """TASK: investigate
You are the Investigator of a hospital shift-cover agent. No automatic action passed the Verifier's
safety checks. Using only the facts and failed checks given, explain briefly what blocked each
option and what the manager should consider next. Do not claim anything was booked."""

RECALL_THRESHOLD = 0.25  # tuned for the local hashing embedder (H-MEM default is 0.35)


class Planner:
    """The LLM: reads the request, proposes and ranks candidates, re-plans once from refusals."""

    def __init__(self, generator: Generator):
        self.generator = generator

    def read_request(self, text: str, today: date) -> tuple[ShiftRequest | None, str]:
        return parse_request(text, today, self.generator)

    def propose(self, request, world_context, pool, memories, memory_context):
        return propose(request, self.generator, world_context, pool, memories, memory_context)

    def replan(self, request, world_context, pool, refusals, tried):
        return replan(request, self.generator, world_context, pool, refusals, tried)

    @staticmethod
    def backfill(pool, tried, budget):
        return backfill(pool, tried, budget)


class Verifier:
    """The SQL precondition gate over Zetaris: the only role that can let an action fire."""

    def __init__(self, source: SqlSource):
        self.source = source

    def check(self, request: ShiftRequest, candidate: Candidate, pending_cover: int = 0) -> GateResult:
        return evaluate(self.source, request, candidate, pending_cover=pending_cover)


class Investigator:
    """The LLM, when nothing passed: explains the refusals for the manager."""

    def __init__(self, generator: Generator):
        self.generator = generator

    def explain(self, request: ShiftRequest, attempts: list[GateResult], world_text: str, data_errors: bool) -> str:
        data = {
            "shift": request.label(),
            "data_errors": data_errors,
            "attempts": [
                {"candidate": a.candidate.label(), "failed": [{"name": r.name, "explanation": r.explanation} for r in a.failed]}
                for a in attempts
            ],
        }
        return self.generator(f"Facts:\n{world_text}\n\n<input>{json.dumps(data)}</input>", INVESTIGATE_SYSTEM).strip()


class MemoryRole:
    """H-MEM: recall before planning, learn from what fired, feedback on refused recalls."""

    def __init__(self, store: MemoryStoreService):
        self.store = store
        self.retrieval = MemoryRetrievalService(store)
        self.seed_action_library()

    def seed_action_library(self) -> None:
        seeded = {m.action["action"] for m in self.store.all() if m.source == "seed:action_library" and m.action}
        for spec in LIBRARY.values():
            if spec.name in seeded:
                continue
            self.store.add(
                Memory.new(
                    f"Action {spec.name}: {spec.description} Fires only if these checks pass: {', '.join(spec.checks)}.",
                    type="general",
                    layer="long_term",
                    source="seed:action_library",
                    confidence=0.9,
                    domain="rostering",
                    tags=["library", f"action:{spec.name}", "rostering"],
                    provenance={"origin": "import", "learnedAt": now_ms(), "label": "seeded from the built-in action library"},
                    action={"action": spec.name, "params": {}, "checks": list(spec.checks), "applies_to": {}},
                ),
                actor="system:seed",
            )

    def recall(self, request: ShiftRequest, demand: dict[str, Any]) -> tuple[RetrievalResult, str]:
        result = self.retrieval.query(
            RetrievalQuery(
                text=f"{request.label()} cover {demand.get('required_skill', '')} rostering",
                tags=[f"ward:{request.ward}", f"shift:{request.shift_type}", "rostering"],
                domain=f"rostering/{request.ward}",
                entities=[Entity(f"Ward {request.ward}", "ward")],
                threshold=RECALL_THRESHOLD,
            )
        )
        return result, format_reinjection(result.memories)

    def learn(self, request: ShiftRequest, gate: GateResult, run_id: str, trace_id: str) -> str:
        c = gate.candidate
        template = {
            "action": c.action,
            "params": c.params,
            "checks": [r.name for r in gate.results],
            "applies_to": {"ward": request.ward, "shift_type": request.shift_type},
        }
        for m in self.store.all():
            if m.action and {k: m.action.get(k) for k in ("action", "params", "applies_to")} == {k: template[k] for k in ("action", "params", "applies_to")}:
                self.store.apply_feedback(m.id, "helpful", actor="agent:preflight", trace_id=trace_id, reason=f"fired again in {run_id}")
                return m.id
        entities = [Entity(f"Ward {request.ward}", "ward")] + ([Entity(c.params["staff_id"], "staff")] if "staff_id" in c.params else [])
        mem = Memory.new(
            f"Ward {request.ward} {request.shift_type} cover: {c.label()} passed all checks "
            f"({', '.join(template['checks'])}) for {request.shift_date.isoformat()}.",
            type="general",
            layer="working",
            source=f"decision:{run_id}",
            confidence=0.7,
            domain=f"rostering/{request.ward}",
            tags=["learned", f"ward:{request.ward}", f"shift:{request.shift_type}", f"action:{c.action}", "rostering"],
            entities=entities,
            provenance={"origin": "decision", "learnedAt": now_ms(), "label": "learned from a gated action that fired", "refs": {"decisionId": run_id}},
            action=template,
        )
        return self.store.add(mem, actor="agent:preflight").id

    def penalise(self, candidate: Candidate, request: ShiftRequest, gate: GateResult, trace_id: str) -> list[str]:
        """The Verifier refused a recalled action: lower confidence (not_helpful). Returns memory ids."""
        touched = []
        for m in self.store.all():
            if m.action and m.action.get("action") == candidate.action and m.action.get("params") == candidate.params \
                    and m.action.get("applies_to", {}).get("ward") == request.ward:
                reason = "; ".join(f"{r.name}: {r.explanation}" for r in gate.failed)
                self.store.apply_feedback(m.id, "not_helpful", actor="agent:preflight", trace_id=trace_id,
                                          reason=f"recalled but refused by the Verifier: {reason}")
                touched.append(m.id)
        return touched
