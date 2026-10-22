"""Candidate planning: which actions to try, in which order.

The generator ranks internal staff using World Model context and recalled
H-MEM memories. Its output is validated against the action library and the
staff pool; anything else is dropped. The outer order is fixed policy:

  1. no_cover_needed     (cheapest outcome; its check proves the shift is covered)
  2. offers to staff     (memory-recalled first and always included, then the generator's ranking,
                          then backfill from the rest of the pool, up to MAX_CANDIDATES)
  3. escalate_to_agency  (costs money, so last)
"""

from __future__ import annotations

import json
from typing import Any

from app.actions.library import LIBRARY, describe_library
from app.adapters.llm import Generator, extract_json
from app.meterless.hmem import Memory
from app.models import Candidate, ShiftRequest

SYSTEM = """TASK: plan_candidates
You are the planning step of a hospital shift-cover agent. Rank internal staff
to offer an open shift to, best fit first. Prefer staff free that day, the
required skill, the home ward, and fewer hours this week. Every candidate will be verified by SQL
precondition checks before anything happens, so rank by fit; do not invent staff.
Reply with only JSON:
{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "<id>"}, "reason": "<short>"}]}"""

MAX_OFFERS = 4  # how many staff the generator is asked to rank
MAX_CANDIDATES = 12  # offers tried in total before the agency (generator's picks, then backfill)


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


def plan(
    request: ShiftRequest,
    generator: Generator,
    world_context: str,
    staff_pool: list[dict[str, Any]],
    memories: list[Memory],
    memory_context: str,
) -> tuple[list[Candidate], dict[str, Any]]:
    remembered = remembered_staff(memories, request)
    payload = {
        "ward": request.ward,
        "shift": request.label(),
        "max_offers": MAX_OFFERS,
        "memory_staff_ids": remembered,
        "staff_pool": [
            {k: p[k] for k in ("staff_id", "home_ward", "skills", "week_hours", "weekly_hour_cap", "shifts_on_date", "already_on_this_shift", "has_required_skill")}
            for p in staff_pool
        ],
    }
    prompt = (
        f"Action library:\n{describe_library()}\n\n"
        f"Recalled memory:\n{memory_context or '(none)'}\n\n"
        f"World model facts:\n{world_context}\n\n"
        f"<input>{json.dumps(payload)}</input>"
    )
    raw = generator(prompt, SYSTEM)
    known_staff = {p["staff_id"] for p in staff_pool if not p.get("already_on_this_shift")}
    dropped: list[str] = []
    offers: list[Candidate] = []
    try:
        proposed = extract_json(raw).get("candidates", [])
    except (ValueError, AttributeError):
        proposed, dropped = [], ["generator reply was not valid JSON"]

    for item in proposed:
        if not isinstance(item, dict):
            continue
        staff_id = str((item.get("params") or {}).get("staff_id", ""))
        if item.get("action") != "offer_shift_to_staff" or staff_id not in known_staff:
            dropped.append(f"{item.get('action')}({staff_id}) not in library/pool")
            continue
        origin = "memory" if staff_id in remembered else "planner"
        c = Candidate("offer_shift_to_staff", {"staff_id": staff_id}, str(item.get("reason", ""))[:200], origin)
        if c.key() not in {o.key() for o in offers}:
            offers.append(c)

    # Memory-recalled staff are always tried, first, even if the generator left
    # them out: recall is a suggestion, and the gate is what checks it against
    # today's data. A stale memory is refused with evidence, never silently trusted.
    proposed_ids = {o.params["staff_id"] for o in offers}
    for staff_id in remembered:
        if staff_id in known_staff and staff_id not in proposed_ids:
            offers.append(Candidate("offer_shift_to_staff", {"staff_id": staff_id},
                                    "recalled from H-MEM (not ranked by the generator)", "memory"))
    offers.sort(key=lambda c: c.origin != "memory")
    offers = offers[:MAX_OFFERS + len(remembered)]

    # Backfill from the rest of the pool before paying for agency: the safety
    # eval found the agency being used while an eligible nurse sat outside the
    # generator's short list. Ranked by fit; the gate still checks every one.
    tried = {o.params["staff_id"] for o in offers}
    rest = sorted(
        (p for p in staff_pool if not p.get("already_on_this_shift") and p["staff_id"] not in tried),
        key=lambda p: (bool(p.get("shifts_on_date")), not p.get("has_required_skill"), float(p.get("week_hours") or 0), p["staff_id"]),
    )
    for p in rest[: max(0, MAX_CANDIDATES - len(offers))]:
        offers.append(Candidate("offer_shift_to_staff", {"staff_id": p["staff_id"]}, "backfill: next best fit in the staff pool", "backfill"))
    candidates = [
        Candidate("no_cover_needed", {}, "check whether the shift is already covered", "default"),
        *offers,
        Candidate("escalate_to_agency", {}, "fallback when no internal staff pass", "default"),
    ]
    assert all(c.action in LIBRARY for c in candidates)
    return candidates, {"raw_reply": raw[:2000], "dropped": dropped, "memory_staff_ids": remembered}
