"""The four agent roles, and which role produced each trace event.

  Planner       the LLM via the injected generator (Meterless Nightshift by default):
                reads the request, proposes and ranks candidates, and re-plans once
                from the Verifier's refusal reasons. It cannot make anything happen.
  Verifier      the SQL precondition gate over Zetaris: approves or rejects each
                candidate with evidence rows. The only role that can let an action fire.
  Investigator  the LLM, when nothing passes: explains the refusals to the manager.
  Memory        H-MEM: recalls the action library and past resolutions, learns from
                actions that fired, and lowers confidence in recalls the Verifier refused.

Everything else (World Model ingest, sending to the outbox, the naive baseline) is
plumbing, labelled "orchestrator" or "baseline" in the trace rather than given an agent name.
"""

from __future__ import annotations

PLANNER, VERIFIER, INVESTIGATOR, MEMORY = "planner", "verifier", "investigator", "memory"
ORCHESTRATOR, BASELINE = "orchestrator", "baseline"

ROLE_OF_EVENT = {
    "parsed": PLANNER,
    "plan": PLANNER,
    "replan": PLANNER,
    "llm_fallback": PLANNER,
    "gate": VERIFIER,
    "recheck": VERIFIER,
    "stop": VERIFIER,
    "investigation": INVESTIGATOR,
    "memory_recall": MEMORY,
    "memory_write": MEMORY,
    "memory_feedback": MEMORY,
    "naive_shadow": BASELINE,
}


def role_of(kind: str) -> str:
    return ROLE_OF_EVENT.get(kind, ORCHESTRATOR)
