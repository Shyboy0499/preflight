"""Preflight agent loop: check before acting.

  parse request -> ingest facts (World Model, from Zetaris) -> recall (H-MEM)
  -> plan candidates -> for each: run every precondition check (Zetaris)
       -> all pass: fire, learn, stop
       -> any fail: log evidence, try next
  -> nothing passed: LLM investigation for a human, no action
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.actions.executor import fire, open_actions, withdraw
from app.actions.library import LIBRARY
from app.adapters.llm import Generator, make_generator
from app.adapters.zetaris import DataSourceUnavailable, SqlSource, make_source
from app.audit import AuditLog
from app.config import Config, load_config
from app.meterless.hmem import (
    Entity,
    Memory,
    MemoryRetrievalService,
    MemoryStoreService,
    RetrievalQuery,
    TrustLedger,
    format_reinjection,
    now_ms,
)
from app.meterless.world_model import ContextBuilderService, IngestPipelineService, WorldStoreService
from app.models import Candidate, ShiftRequest
from app.planner import plan, remembered_staff
from app.preconditions.gate import GateResult, build_params, evaluate
from app.request_parser import parse_request

INVESTIGATE_SYSTEM = """TASK: investigate
You support a nurse unit manager. No automatic action passed its safety checks.
Using only the facts and failed checks given, explain briefly what blocked each
option and what the manager should consider next. Do not claim anything was booked."""

RECALL_THRESHOLD = 0.25  # tuned for the local hashing embedder (H-MEM default is 0.35)


@dataclass
class RunResult:
    run_id: str
    request_text: str
    request: ShiftRequest | None
    parse_path: str
    decision: str  # fired | partially_covered | already_covered | no_safe_action | unverifiable | unparsed
    summary: str
    fired: dict[str, Any] | None = None  # first action that fired
    actions: list[dict[str, Any]] = field(default_factory=list)  # every action that fired, in order
    shortfall_remaining: int | None = None  # nurses still missing after this run (None if unknown)
    attempts: list[GateResult] = field(default_factory=list)
    rechecks: list[dict[str, Any]] = field(default_factory=list)  # earlier open actions re-checked: kept / withdrawn
    naive: dict[str, Any] | None = None
    investigation: str | None = None
    memory_trace: dict[str, Any] | None = None
    world: dict[str, Any] | None = None
    learned_memory_id: str | None = None
    generator: str = ""
    data_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_text": self.request_text,
            "request": self.request.to_dict() if self.request else None,
            "parse_path": self.parse_path,
            "decision": self.decision,
            "summary": self.summary,
            "fired": self.fired,
            "actions": self.actions,
            "shortfall_remaining": self.shortfall_remaining,
            "attempts": [a.to_dict() for a in self.attempts],
            "rechecks": [{"record": r["record"], "status": r["status"], "gate": r["gate"].to_dict()} for r in self.rechecks],
            "naive": self.naive,
            "investigation": self.investigation,
            "memory_trace": self.memory_trace,
            "world": self.world,
            "learned_memory_id": self.learned_memory_id,
            "generator": self.generator,
            "data_source": self.data_source,
        }


class PreflightAgent:
    def __init__(self, cfg: Config | None = None, source: SqlSource | None = None, generator: Generator | None = None):
        self.cfg = cfg or load_config()
        self.source = source or make_source(self.cfg)
        self._fallback_notes: list[str] = []
        if generator is not None:
            self.generator, self.generator_name = generator, "injected"
        else:
            self.generator, self.generator_name = make_generator(self.cfg, self._fallback_notes.append)
        self.ledger = TrustLedger(self.cfg.logs_dir / "hmem_ledger.jsonl")
        self.memory = MemoryStoreService(self.cfg.state_dir / "hmem_memories.json", self.ledger)
        self.memory.hydrate()
        self.retrieval = MemoryRetrievalService(self.memory)
        self._seed_action_library()

    # --- H-MEM -------------------------------------------------------------------

    def _seed_action_library(self) -> None:
        seeded = {m.action["action"] for m in self.memory.all() if m.source == "seed:action_library" and m.action}
        for spec in LIBRARY.values():
            if spec.name in seeded:
                continue
            self.memory.add(
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

    def _learn(self, request: ShiftRequest, gate: GateResult, run_id: str, trace_id: str) -> str:
        c = gate.candidate
        template = {
            "action": c.action,
            "params": c.params,
            "checks": [r.name for r in gate.results],
            "applies_to": {"ward": request.ward, "shift_type": request.shift_type},
        }
        for m in self.memory.all():
            if m.action and {k: m.action.get(k) for k in ("action", "params", "applies_to")} == {k: template[k] for k in ("action", "params", "applies_to")}:
                self.memory.apply_feedback(m.id, "helpful", actor="agent:preflight", trace_id=trace_id, reason=f"fired again in {run_id}")
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
        return self.memory.add(mem, actor="agent:preflight").id

    # --- main loop ----------------------------------------------------------------

    def run(self, text: str) -> RunResult:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        audit = AuditLog(self.cfg.logs_dir, run_id)
        self._fallback_notes.clear()
        source_name = getattr(self.source, "name", "unknown")
        audit.event("request", text=text, sample_mode=self.cfg.sample_mode, data_source=source_name, generator=self.generator_name)

        request, parse_path = parse_request(text, self.cfg.today, self.generator)
        audit.event("parsed", request=request.to_dict() if request else None, path=parse_path)
        if request is None:
            summary = "Could not read a ward, date and shift (day/evening/night) from the request. Nothing done."
            return self._finish(audit, RunResult(run_id, text, None, parse_path, "unparsed", summary, generator=self.generator_name, data_source=source_name))

        # World Model: pull the facts in scope from Zetaris, with provenance
        world = WorldStoreService()
        params = build_params(request, Candidate("scope"))
        report = IngestPipelineService(self.source, world).ingest_scope(request, params)
        reader = ContextBuilderService(world)
        context = reader.build_prompt_context(request)
        pool = reader.staff_pool(request)
        audit.event("world_ingest", report=report.to_dict(), context_provenance=context["provenance"], staff_pool_size=len(pool))

        # H-MEM: recall the library and past resolutions
        demand = reader.demand(request) or {}
        recall = self.retrieval.query(
            RetrievalQuery(
                text=f"{request.label()} cover {demand.get('required_skill', '')} rostering",
                tags=[f"ward:{request.ward}", f"shift:{request.shift_type}", "rostering"],
                domain=f"rostering/{request.ward}",
                entities=[Entity(f"Ward {request.ward}", "ward")],
                threshold=RECALL_THRESHOLD,
            )
        )
        memory_context = format_reinjection(recall.memories)
        audit.event("memory_recall", trace=recall.trace, reinjected=memory_context)

        candidates, plan_meta = plan(request, self.generator, context["text"], pool, recall.memories, memory_context)
        audit.event("plan", candidates=[c.to_dict() for c in candidates], **plan_meta)

        result = RunResult(
            run_id, text, request, parse_path, "no_safe_action", "", memory_trace=recall.trace,
            world={
                "report": report.to_dict(), "context": context, "staff_pool": pool, "demand": demand or None,
                "names": {p["staff_id"]: reader.display_name(p["staff_id"]) for p in pool},  # local display only
            },
            generator=self.generator_name, data_source=source_name,
        )
        naive_pick = next((c for c in candidates if c.action == "offer_shift_to_staff"), None)

        # Actions already sent for this shift are re-checked first, through the
        # same gate, as if sending them now. Still valid: they count as cover.
        # No longer valid (e.g. new leave): withdrawn, with the evidence.
        pending, remaining, rechecked_staff = 0, None, set()
        for record in open_actions(self.cfg.logs_dir / "outbox.jsonl", request):
            previous = Candidate(record["action"], record["params"], f"re-check of {record['action_id']} sent in {record.get('run_id')}", "open")
            gate = evaluate(self.source, request, previous, pending_cover=pending)
            audit.event("recheck", action_id=record["action_id"], **gate.to_dict())
            rechecked_staff.add(record["params"].get("staff_id"))
            if gate.passed:
                nurses = int(record.get("nurses") or 1)
                pending += nurses
                remaining = max(0, (gate.shortfall or 0) - nurses)
                result.rechecks.append({"record": record, "status": "kept", "gate": gate})
                continue
            reason = "; ".join(f"{r.name} ({r.explanation})" for r in gate.failed)
            sid = record["params"].get("staff_id")
            out = withdraw(record, reason, audit, reader.display_name(sid) if sid else "")
            audit.event("action_withdrawn", **out)
            result.actions.append(out)
            result.rechecks.append({"record": record, "status": "withdrawn", "gate": gate})
            if sid in remembered_staff(recall.memories, request):
                self._penalise_recalled(previous, request, gate, recall.trace["trace_id"])

        if pending and remaining == 0:
            result.decision, result.shortfall_remaining = "already_covered", 0
            audit.event("stop", reason="shift already covered by open actions that still pass every check")
            candidates = []
        elif pending or result.rechecks:
            # don't re-offer staff just re-checked, and "no cover needed" is moot once cover is in flight
            candidates = [c for c in candidates
                          if c.params.get("staff_id") not in rechecked_staff and not (pending and c.action == "no_cover_needed")]

        # Fill the whole shortfall: each passing offer covers one nurse and is
        # counted by ward_actually_understaffed on the next gate; the agency
        # request (last) covers whatever is still missing.
        for candidate in candidates:
            gate = evaluate(self.source, request, candidate, pending_cover=pending)
            result.attempts.append(gate)
            audit.event("gate", **gate.to_dict())
            if gate.shortfall is not None:
                result.shortfall_remaining = gate.shortfall
            if not gate.passed:
                if candidate.origin == "memory":
                    self._penalise_recalled(candidate, request, gate, recall.trace["trace_id"])
                # Not short (or no demand record): every later candidate needs this
                # same check and offers only add cover, so none can pass. Stop.
                if any(r.name == "ward_actually_understaffed" and r.status == "fail" for r in gate.results):
                    audit.event("stop", reason="ward_actually_understaffed failed; no later candidate can pass")
                    break
                continue
            name = reader.display_name(candidate.params.get("staff_id", "")) if "staff_id" in candidate.params else ""
            nurses = gate.shortfall if candidate.action == "escalate_to_agency" and gate.shortfall else 1
            record = fire(candidate, request, audit, name, nurses=nurses)
            result.actions.append(record)
            result.fired = result.fired or record
            result.decision = "fired"
            audit.event("action_fired", **record)
            memory_id = self._learn(request, gate, run_id, recall.trace["trace_id"])
            result.learned_memory_id = result.learned_memory_id or memory_id
            audit.event("memory_write", memory_id=memory_id)
            if candidate.action == "offer_shift_to_staff":
                pending += 1
                result.shortfall_remaining = max(0, (gate.shortfall or 1) - 1)
                if result.shortfall_remaining:
                    continue
            elif candidate.action == "escalate_to_agency":
                result.shortfall_remaining = 0
            elif candidate.action == "no_cover_needed":
                result.shortfall_remaining = 0
            break
        if result.decision == "fired" and result.shortfall_remaining:
            result.decision = "partially_covered"
        if result.decision == "no_safe_action" and pending and not result.fired:
            # open actions still cover part of the gap and nothing new passed
            result.decision = "partially_covered"
            result.shortfall_remaining = remaining

        if naive_pick:
            naive_gate = next((a for a in result.attempts if a.candidate.key() == naive_pick.key()), None) \
                or evaluate(self.source, request, naive_pick)  # shadow only, never fired
            result.naive = {
                "would_fire": naive_pick.label(),
                "staff_name": reader.display_name(naive_pick.params["staff_id"]),
                "unsafe": bool(naive_gate and not naive_gate.passed),
                "violations": [f"{r.name}: {r.explanation}" for r in naive_gate.failed] if naive_gate else [],
            }
            audit.event("naive_shadow", **result.naive)

        if result.decision not in ("fired", "already_covered"):
            any_errors = any(r.status == "error" for a in result.attempts for r in a.results) or not report.complete
            if not result.actions and any_errors and all(any(r.status == "error" for r in a.results) for a in result.attempts):
                result.decision = "unverifiable"
            result.investigation = self._investigate(request, result, context["text"], any_errors)
            audit.event("investigation", text=result.investigation)

        result.summary = self._summary(result, reader)
        return self._finish(audit, result)

    def _penalise_recalled(self, candidate: Candidate, request: ShiftRequest, gate: GateResult, trace_id: str) -> None:
        for m in self.memory.all():
            if m.action and m.action.get("action") == candidate.action and m.action.get("params") == candidate.params \
                    and m.action.get("applies_to", {}).get("ward") == request.ward:
                reason = "; ".join(f"{r.name}: {r.explanation}" for r in gate.failed)
                self.memory.apply_feedback(m.id, "not_helpful", actor="agent:preflight", trace_id=trace_id, reason=f"recalled but refused by gate: {reason}")

    def _investigate(self, request: ShiftRequest, result: RunResult, world_text: str, data_errors: bool) -> str:
        import json

        data = {
            "shift": request.label(),
            "data_errors": data_errors,
            "attempts": [
                {"candidate": a.candidate.label(), "failed": [{"name": r.name, "explanation": r.explanation} for r in a.failed]}
                for a in result.attempts
            ],
        }
        return self.generator(f"Facts:\n{world_text}\n\n<input>{json.dumps(data)}</input>", INVESTIGATE_SYSTEM).strip()

    @staticmethod
    def _summary(result: RunResult, reader: ContextBuilderService) -> str:
        refused = [a for a in result.attempts if not a.passed and a.candidate.action != "no_cover_needed"]
        lines = []
        for r in result.rechecks:
            if r["status"] == "kept":
                lines.append(f"STILL OPEN {r['record']['action']} ({r['record']['action_id']}): re-checked, every check passes")
        for action in result.actions:
            args = ", ".join(f"{k}={v}" for k, v in action["params"].items())
            verb = "WITHDRAWN" if action["action"] == "withdraw" else "FIRED"
            name = action.get("withdrawn_action", action["action"])
            lines.append(f"{verb} {name}({args}): {action['message']}")
        if result.decision == "already_covered":
            lines.append("ALREADY COVERED by the open actions above. Nothing new sent.")
        if result.decision == "partially_covered":
            lines.append(f"STILL SHORT by {result.shortfall_remaining}: nothing else passed every check. Escalated to the manager with evidence.")
        elif result.decision == "unverifiable":
            lines.append("NO ACTION: live data could not be verified, so nothing was booked.")
        elif result.decision == "no_safe_action":
            lines.append("NO ACTION: no candidate passed every check. Escalated to the manager with evidence.")
        for a in refused:
            who = a.candidate.label()
            if "staff_id" in a.candidate.params:
                who += f" [{reader.display_name(a.candidate.params['staff_id'])}]"
            lines.append(f"  refused {who}: " + "; ".join(f"{r.name} ({r.explanation})" for r in a.failed))
        if result.naive and result.naive["unsafe"]:
            lines.append(f"  a naive agent would have fired {result.naive['would_fire']} [{result.naive['staff_name']}] despite: "
                         + "; ".join(result.naive["violations"]))
        return "\n".join(lines)

    def _finish(self, audit: AuditLog, result: RunResult) -> RunResult:
        if self._fallback_notes:
            audit.event("llm_fallback", notes=list(self._fallback_notes))
        audit.event("decision", decision=result.decision, summary=result.summary)
        audit.decision({
            "request_text": result.request_text,
            "request": result.request.to_dict() if result.request else None,
            "decision": result.decision,
            "fired": result.fired,
            "actions": result.actions,
            "shortfall_remaining": result.shortfall_remaining,
            "refused": [
                {"candidate": a.candidate.label(), "failed": [{"check": r.name, "why": r.explanation, "evidence": r.evidence} for r in a.failed]}
                for a in result.attempts if not a.passed
            ],
            "naive": result.naive,
            "data_source": result.data_source,
            "generator": result.generator,
        })
        return result


__all__ = ["PreflightAgent", "RunResult", "DataSourceUnavailable"]
