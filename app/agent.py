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

from app.actions.executor import amend_agency, fire, open_actions, withdraw
from app.adapters.llm import Generator, make_generator
from app.adapters.zetaris import DataSourceUnavailable, SqlSource, make_source
from app.audit import AuditLog
from app.config import Config, load_config
from app.meterless.hmem import MemoryStoreService, TrustLedger
from app.meterless.world_model import ContextBuilderService, IngestPipelineService, WorldStoreService
from app.agents import Investigator, MemoryRole, Planner, Verifier
from app.models import Candidate, ShiftRequest
from app.planner import MAX_CANDIDATES, remembered_staff
from app.preconditions.gate import GateResult, build_params

  # tuned for the local hashing embedder (H-MEM default is 0.35)


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
    replans: list[dict[str, Any]] = field(default_factory=list)  # Planner round 2: refusals in, proposals out
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
            "replans": self.replans,
            "naive": self.naive,
            "investigation": self.investigation,
            "memory_trace": self.memory_trace,
            "world": self.world,
            "learned_memory_id": self.learned_memory_id,
            "generator": self.generator,
            "data_source": self.data_source,
        }


class PreflightAgent:
    """Runs the loop with four roles (app/agents.py): Planner, Verifier, Investigator, Memory."""

    def __init__(self, cfg: Config | None = None, source: SqlSource | None = None, generator: Generator | None = None,
                 auto_send: bool = True):
        self.cfg = cfg or load_config()
        self.auto_send = auto_send  # False: actions are prepared for the manager to send (UI)
        self.source = source or make_source(self.cfg)
        self._fallback_notes: list[str] = []
        if generator is not None:
            self.generator, self.generator_name = generator, "injected"
        else:
            self.generator, self.generator_name = make_generator(self.cfg, self._fallback_notes.append)
        self.ledger = TrustLedger(self.cfg.logs_dir / "hmem_ledger.jsonl")
        self.memory = MemoryStoreService(self.cfg.state_dir / "hmem_memories.json", self.ledger)
        self.memory.hydrate()
        self.planner = Planner(self.generator)
        self.verifier = Verifier(self.source)
        self.investigator = Investigator(self.generator)
        self.memory_role = MemoryRole(self.memory)

    # --- main loop ----------------------------------------------------------------

    def run(self, text: str) -> RunResult:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        audit = AuditLog(self.cfg.logs_dir, run_id)
        self._fallback_notes.clear()
        source_name = getattr(self.source, "name", "unknown")
        audit.event("request", text=text, sample_mode=self.cfg.sample_mode, data_source=source_name, generator=self.generator_name)

        # Planner reads the request
        request, parse_path = self.planner.read_request(text, self.cfg.today)
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

        # Memory recalls the library and past resolutions
        demand = reader.demand(request) or {}
        recall, memory_context = self.memory_role.recall(request, demand)
        trace_id = recall.trace["trace_id"]
        audit.event("memory_recall", trace=recall.trace, reinjected=memory_context)
        remembered = remembered_staff(recall.memories, request)

        # Planner, round 1
        round1, plan_meta = self.planner.propose(request, context["text"], pool, recall.memories, memory_context)
        audit.event("plan", round=1, candidates=[c.to_dict() for c in round1], **plan_meta)

        result = RunResult(
            run_id, text, request, parse_path, "no_safe_action", "", memory_trace=recall.trace,
            world={
                "report": report.to_dict(), "context": context, "staff_pool": pool, "demand": demand or None,
                "names": {p["staff_id"]: reader.display_name(p["staff_id"]) for p in pool},  # local display only
            },
            generator=self.generator_name, data_source=source_name,
        )
        naive_pick = round1[0] if round1 else None

        def penalise(candidate: Candidate, gate: GateResult) -> None:
            touched = self.memory_role.penalise(candidate, request, gate, trace_id)
            if touched:
                audit.event("memory_feedback", signal="not_helpful", memory_ids=touched, candidate=candidate.label(),
                            reasons=[f"{r.name}: {r.explanation}" for r in gate.failed])

        # Verifier re-checks actions already sent for this shift, as if sending them now.
        # Still valid: they count as cover. No longer valid (e.g. new leave): withdrawn with evidence.
        pending, remaining, tried = 0, None, set()
        for record in open_actions(self.cfg.logs_dir / "outbox.jsonl", request):
            previous = Candidate(record["action"], record["params"], f"re-check of {record['action_id']} sent in {record.get('run_id')}", "open")
            gate = self.verifier.check(request, previous, pending_cover=pending)
            audit.event("recheck", action_id=record["action_id"], **gate.to_dict())
            if record["params"].get("staff_id"):
                tried.add(record["params"]["staff_id"])
            if gate.passed:
                nurses = int(record.get("nurses") or 1)
                if record["action"] == "escalate_to_agency" and gate.shortfall is not None and nurses > gate.shortfall:
                    out = amend_agency(record, gate.shortfall, f"only {gate.shortfall} still missing", audit)
                    audit.event("action_amended", **out)
                    result.actions.append(out)
                    nurses = gate.shortfall
                pending += nurses
                remaining = max(0, (gate.shortfall or 0) - nurses)
                result.rechecks.append({"record": record, "status": "kept", "gate": gate})
                continue
            sid = record["params"].get("staff_id")
            out = withdraw(record, "; ".join(f"{r.name} ({r.explanation})" for r in gate.failed), audit, reader.display_name(sid) if sid else "")
            audit.event("action_withdrawn", **out)
            result.actions.append(out)
            result.rechecks.append({"record": record, "status": "withdrawn", "gate": gate})
            if sid in remembered:
                penalise(previous, gate)

        if pending and remaining == 0:
            result.decision, result.shortfall_remaining = "already_covered", 0
            audit.event("stop", reason="shift already covered by open actions that still pass every check")

        def verify(batch: list[Candidate]) -> str:
            """Verifier checks each candidate in order; fire what passes. Returns
            'done' (gap closed / final action), 'stop' (no later candidate can pass) or 'continue'."""
            nonlocal pending
            for candidate in batch:
                sid = candidate.params.get("staff_id")
                if sid:
                    if sid in tried:
                        continue
                    tried.add(sid)
                gate = self.verifier.check(request, candidate, pending_cover=pending)
                result.attempts.append(gate)
                audit.event("gate", **gate.to_dict())
                if gate.shortfall is not None:
                    result.shortfall_remaining = gate.shortfall
                if not gate.passed:
                    if candidate.origin == "memory":
                        penalise(candidate, gate)
                    # Not short (or no demand record): every later candidate needs this
                    # same check and offers only add cover, so none can pass.
                    if any(r.name == "ward_actually_understaffed" and r.status == "fail" for r in gate.results):
                        audit.event("stop", reason="ward_actually_understaffed failed; no later candidate can pass")
                        return "stop"
                    continue
                name = reader.display_name(sid) if sid else ""
                nurses = gate.shortfall if candidate.action == "escalate_to_agency" and gate.shortfall else 1
                record = fire(candidate, request, audit, name, nurses=nurses, warnings=[w.explanation for w in gate.warnings],
                              auto_send=self.auto_send)
                result.actions.append(record)
                result.fired = result.fired or record
                result.decision = "fired"
                audit.event("action_fired", **record)
                memory_id = self.memory_role.learn(request, gate, run_id, trace_id)
                result.learned_memory_id = result.learned_memory_id or memory_id
                audit.event("memory_write", memory_id=memory_id)
                if candidate.action == "offer_shift_to_staff":
                    pending += 1
                    result.shortfall_remaining = max(0, (gate.shortfall or 1) - 1)
                    if result.shortfall_remaining:
                        continue
                else:
                    result.shortfall_remaining = 0
                return "done"
            return "continue"

        if result.decision != "already_covered":
            head = [] if pending else [Candidate("no_cover_needed", {}, "check whether the shift is already covered", "default")]
            status = verify(head + round1)
            if status == "continue":
                # Feedback loop: the Verifier's refusal reasons go back to the Planner once.
                refusals = [
                    {"staff_id": a.candidate.params["staff_id"], "failed_checks": [r.name for r in a.failed],
                     "reasons": [r.explanation for r in a.failed]}
                    for a in result.attempts if not a.passed and a.candidate.action == "offer_shift_to_staff"
                ]
                if refusals:
                    round2, meta = self.planner.replan(request, context["text"], pool, refusals, set(tried))
                    audit.event("replan", round=2, refusals=refusals, candidates=[c.to_dict() for c in round2], **meta)
                    result.replans.append({"refusals": refusals, "proposed": [c.params["staff_id"] for c in round2]})
                    status = verify(round2)
            if status == "continue":
                offers_so_far = sum(a.candidate.action == "offer_shift_to_staff" for a in result.attempts)
                status = verify(self.planner.backfill(pool, set(tried), MAX_CANDIDATES - offers_so_far))
            if status == "continue":
                verify([Candidate("escalate_to_agency", {}, "fallback when no internal staff pass", "default")])

        if result.decision == "fired" and result.shortfall_remaining:
            result.decision = "partially_covered"
        if result.decision == "no_safe_action" and pending and not result.fired:
            # open actions still cover part of the gap and nothing new passed
            result.decision = "partially_covered"
            result.shortfall_remaining = remaining

        if naive_pick:
            known = [*result.attempts, *(r["gate"] for r in result.rechecks)]
            naive_gate = next((a for a in known if a.candidate.action == naive_pick.action and a.candidate.params == naive_pick.params), None) \
                or self.verifier.check(request, naive_pick)  # shadow only, never fired
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
            result.investigation = self.investigator.explain(request, result.attempts, context["text"], any_errors)
            audit.event("investigation", text=result.investigation)

        result.summary = self._summary(result, reader)
        return self._finish(audit, result)

    @staticmethod
    def _summary(result: RunResult, reader: ContextBuilderService) -> str:
        refused = [a for a in result.attempts if not a.passed and a.candidate.action != "no_cover_needed"]
        lines = []
        for r in result.rechecks:
            if r["status"] == "kept":
                lines.append(f"STILL OPEN {r['record']['action']} ({r['record']['action_id']}): re-checked, every check passes")
        for action in result.actions:
            args = ", ".join(f"{k}={v}" for k, v in action["params"].items())
            verb = {"withdraw": "WITHDRAWN", "amend": "AMENDED"}.get(action["action"], "FIRED")
            name = action.get("withdrawn_action") or action.get("amended_action") or action["action"]
            lines.append(f"{verb} {name}({args}): {action['message']}")
            lines += [f"  {w}" for w in action.get("warnings", [])]
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
