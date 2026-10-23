"""The action library: what Preflight may do, and what must be true first.

An action's checks are fixed here in code. Neither the LLM nor memory can add,
remove or skip a check; they can only propose which action to try and with
which parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.preconditions.checks import ALL_CHECKS


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    params: tuple[str, ...]
    checks: tuple[str, ...]


LIBRARY: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec(
            "no_cover_needed",
            "Record that the shift is already covered; nobody is contacted.",
            (),
            ("ward_adequately_staffed",),
        ),
        ActionSpec(
            "offer_shift_to_staff",
            "Offer the open shift to one internal staff member.",
            ("staff_id",),
            (
                "ward_actually_understaffed",
                "staff_not_on_leave",
                "has_required_skill",
                "under_weekly_hour_cap",
                "no_conflicting_shift",
                "no_pending_leave_request",  # advisory: warns, never blocks
            ),
        ),
        ActionSpec(
            "escalate_to_agency",
            "Request an agency nurse for the shift.",
            (),
            ("ward_actually_understaffed", "agency_allowed_for_ward"),
        ),
    )
}

assert all(c in ALL_CHECKS for spec in LIBRARY.values() for c in spec.checks)


def describe_library() -> str:
    return "\n".join(
        f"- {s.name}({', '.join(s.params)}): {s.description} Checks: {', '.join(s.checks)}."
        for s in LIBRARY.values()
    )
