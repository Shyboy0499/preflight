"""Planner role: which staff to offer the shift to, and in what order.

The generator (the LLM) ranks internal staff using World Model context and
recalled H-MEM memories. Its output is validated against the action library and
the staff pool; anything else is dropped. The Verifier (gate) decides; the
Planner only proposes. The order the agent tries things in:

  1. no_cover_needed      cheapest outcome; its check proves the shift is covered
  2. propose()            round 1: memory-recalled staff first (always), then the LLM's ranking
  3. replan()             round 2, once, only if round 1 left a gap: the Verifier's refusal
                          reasons go back to the LLM, which proposes different staff
  4. backfill()           the rest of the pool by fit, up to MAX_CANDIDATES offers in total
  5. escalate_to_agency   costs money, so last
"""

from __future__ import annotations

import json
from typing import Any

from app.actions.library import describe_library
from app.adapters.llm import Generator, extract_json
from app.meterless.hmem import Memory
from app.models import Candidate, ShiftRequest

SYSTEM = """TASK: plan_candidates
You are the Planner of a hospital shift-cover agent. Rank internal staff
to offer an open shift to, best fit first. Prefer staff free that day, the
required skill, the home ward, and fewer hours this week. Every candidate will be checked by the
Verifier (SQL precondition checks) before anything happens, so rank by fit; do not invent staff.
Reply with only JSON:
{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "<id>"}, "reason": "<short>"}]}"""

REPLAN_SYSTEM = """TASK: replan_candidates
You are the Planner of a hospital shift-cover agent. The Verifier ran SQL checks on live data and
refused your previous candidates; the refusals and their evidence are below. Propose different staff
from the pool who avoid those reasons, best fit first. Do not repeat anyone already tried and do not
invent staff. Return an empty list if nobody plausible is left.
Reply with only JSON:
{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "<id>"}, "reason": "<short>"}]}"""

MAX_OFFERS = 4  # how many staff the generator is asked to rank per round
MAX_CANDIDATES = 12  # offers tried in total before the agency (all rounds plus backfill)
POOL_FIELDS = ("staff_id", "home_ward", "skills", "week_hours", "weekly_hour_cap", "shifts_on_date",
               "leave_on_date", "already_on_this_shift", "has_required_skill")


def remembered_staff(memories: list[Memory], request: ShiftRequest) -> list[str]:
    """Staff IDs from recalled action templates that apply to this ward and shift."""
    out = []
    for m in memories:
        a = m.action or {}
        applies = a.get("applies_to", {})
        if (
            a.get("action") == "offer_shift_to_staff"
            and applies.get("ward") == request.ward
            and applies.get("shift_type") == request.shift_type
            and a.get("params", {}).get("staff_id") not in out
        ):
            out.append(a["params"]["staff_id"])
    return out


def _validated(raw: str, staff_pool: list[dict[str, Any]], exclude: set[str], origin: str,
               remembered: list[str]) -> tuple[list[Candidate], list[str]]:
    known = {p["staff_id"] for p in staff_pool if not p.get("already_on_this_shift")} - exclude
    dropped: list[str] = []
    try:
        proposed = extract_json(raw).get("candidates", [])
    except (ValueError, AttributeError):
        return [], ["generator reply was not valid JSON"]
    offers: list[Candidate] = []
    for item in proposed:
        if not isinstance(item, dict):
            continue
        staff_id = str((item.get("params") or {}).get("staff_id", ""))
        if item.get("action") != "offer_shift_to_staff" or staff_id not in known:
            dropped.append(f"{item.get('action')}({staff_id}) not in library/pool or already tried")
            continue
        c = Candidate("offer_shift_to_staff", {"staff_id": staff_id}, str(item.get("reason", ""))[:200],
                      "memory" if staff_id in remembered else origin)
        if c.key() not in {o.key() for o in offers}:
            offers.append(c)
    return offers[:MAX_OFFERS], dropped


def _pool_payload(staff_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: p.get(k) for k in POOL_FIELDS} for p in staff_pool]


def propose(request: ShiftRequest, generator: Generator, world_context: str, staff_pool: list[dict[str, Any]],
            memories: list[Memory], memory_context: str) -> tuple[list[Candidate], dict[str, Any]]:
    """Round 1. Memory-recalled staff are always included, first: recall is a suggestion
    the Verifier checks against today's data, never something trusted silently."""
    remembered = remembered_staff(memories, request)
    payload = {"ward": request.ward, "shift": request.label(), "shift_hours": request.hours, "max_offers": MAX_OFFERS,
               "memory_staff_ids": remembered, "staff_pool": _pool_payload(staff_pool)}
    prompt = (
        f"Action library:\n{describe_library()}\n\n"
        f"Recalled memory:\n{memory_context or '(none)'}\n\n"
        f"World model facts:\n{world_context}\n\n"
        f"<input>{json.dumps(payload)}</input>"
    )
    raw = generator(prompt, SYSTEM)
    offers, dropped = _validated(raw, staff_pool, set(), "planner", remembered)
    known = {p["staff_id"] for p in staff_pool if not p.get("already_on_this_shift")}
    proposed_ids = {o.params["staff_id"] for o in offers}
    for staff_id in remembered:
        if staff_id in known and staff_id not in proposed_ids:
            offers.append(Candidate("offer_shift_to_staff", {"staff_id": staff_id},
                                    "recalled from H-MEM (not ranked by the generator)", "memory"))
    offers.sort(key=lambda c: c.origin != "memory")
    return offers, {"raw_reply": raw[:2000], "dropped": dropped, "memory_staff_ids": remembered}


def replan(request: ShiftRequest, generator: Generator, world_context: str, staff_pool: list[dict[str, Any]],
           refusals: list[dict[str, Any]], tried: set[str]) -> tuple[list[Candidate], dict[str, Any]]:
    """Round 2: the Verifier's refusal reasons go back to the Planner once."""
    payload = {"ward": request.ward, "shift": request.label(), "shift_hours": request.hours, "max_offers": MAX_OFFERS,
               "refusals": refusals, "already_tried": sorted(tried), "staff_pool": _pool_payload(staff_pool)}
    prompt = f"World model facts:\n{world_context}\n\n<input>{json.dumps(payload)}</input>"
    raw = generator(prompt, REPLAN_SYSTEM)
    offers, dropped = _validated(raw, staff_pool, tried, "replan", [])
    return offers, {"raw_reply": raw[:2000], "dropped": dropped}


def backfill(staff_pool: list[dict[str, Any]], tried: set[str], budget: int) -> list[Candidate]:
    """The rest of the pool by fit, so the agency is not paid while an eligible nurse
    sits outside the Planner's short lists (found by the safety eval)."""
    rest = sorted(
        (p for p in staff_pool if not p.get("already_on_this_shift") and p["staff_id"] not in tried),
        key=lambda p: (bool(p.get("shifts_on_date")), not p.get("has_required_skill"), float(p.get("week_hours") or 0), p["staff_id"]),
    )
    return [Candidate("offer_shift_to_staff", {"staff_id": p["staff_id"]}, "backfill: next best fit in the staff pool", "backfill")
            for p in rest[:max(0, budget)]]
