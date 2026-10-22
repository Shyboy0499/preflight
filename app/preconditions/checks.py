"""Precondition checks: executable SQL against live data, plus a pass rule.

Each check fetches evidence rows through the Zetaris adapter and decides
pass/fail from those rows only. The rows are kept as evidence, so every
refusal can show the exact records that caused it.

Parameters available to every check (built by app.preconditions.gate.build_params):
  :ward :shift_date :shift_type :staff_id :week_start :week_end
  :shift_start :shift_end :rest_from :rest_until :shift_hours
and, for pass rules only, pending_cover (offers already fired in this run).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.adapters.zetaris import Query

Rows = list[dict[str, Any]]


@dataclass(frozen=True)
class Check:
    name: str
    description: str
    query: Query
    passes: Callable[[Rows, dict[str, Any]], bool]
    explain: Callable[[Rows, dict[str, Any]], str]


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y", "t"}


# --- staffing ----------------------------------------------------------------

STAFFING = Query(
    "staffing_level",
    """
    SELECT d.ward AS ward, d.shift_date AS shift_date, d.shift_type AS shift_type,
           d.required_nurses AS required_nurses, d.required_skill AS required_skill,
           COUNT(r.staff_id) AS available_rostered
    FROM {demand} d
    LEFT JOIN (
        SELECT ro.staff_id AS staff_id, ro.ward AS ward, ro.shift_date AS shift_date, ro.shift_type AS shift_type
        FROM {roster} ro
        LEFT JOIN (
            SELECT lv.staff_id AS staff_id FROM {leave} lv
            WHERE lv.status = 'approved' AND lv.start_date <= :shift_date AND lv.end_date >= :shift_date
        ) away ON away.staff_id = ro.staff_id
        WHERE ro.ward = :ward AND ro.shift_date = :shift_date AND ro.shift_type = :shift_type
          AND away.staff_id IS NULL
    ) r ON r.ward = d.ward AND r.shift_date = d.shift_date AND r.shift_type = d.shift_type
    WHERE d.ward = :ward AND d.shift_date = :shift_date AND d.shift_type = :shift_type
    GROUP BY d.ward, d.shift_date, d.shift_type, d.required_nurses, d.required_skill
    """,
    ("ward", "shift_date", "shift_type", "required_nurses", "required_skill", "available_rostered"),
)


def _staffing_text(rows: Rows, p: dict[str, Any]) -> str:
    if not rows:
        return f"no demand record for {p['ward']} {p['shift_type']} {p['shift_date']}"
    r = rows[0]
    pending = int(p.get("pending_cover", 0))
    extra = f" + {pending} offered in this run" if pending else ""
    return f"{r['available_rostered']}{extra} of {r['required_nurses']} required nurses available (after approved leave)"


def shortfall(rows: Rows, p: dict[str, Any]) -> int:
    """Nurses still missing: demand minus rostered (not on leave) minus offers made in this run."""
    if not rows:
        return 0
    r = rows[0]
    return int(r["required_nurses"]) - int(r["available_rostered"]) - int(p.get("pending_cover", 0))


WARD_UNDERSTAFFED = Check(
    "ward_actually_understaffed",
    "Rostered nurses not on leave are fewer than demand requires",
    STAFFING,
    passes=lambda rows, p: bool(rows) and shortfall(rows, p) > 0,
    explain=_staffing_text,
)

WARD_ADEQUATELY_STAFFED = Check(
    "ward_adequately_staffed",
    "Rostered nurses not on leave already meet demand",
    STAFFING,
    passes=lambda rows, p: bool(rows) and int(rows[0]["available_rostered"]) >= int(rows[0]["required_nurses"]),
    explain=_staffing_text,
)

# --- staff member ----------------------------------------------------------------

STAFF_NOT_ON_LEAVE = Check(
    "staff_not_on_leave",
    "No approved leave overlaps the shift date",
    Query(
        "leave_overlapping_shift",
        """
        SELECT lv.staff_id AS staff_id, lv.leave_type AS leave_type, lv.start_date AS start_date,
               lv.end_date AS end_date, lv.status AS status
        FROM {leave} lv
        WHERE lv.staff_id = :staff_id AND lv.status = 'approved'
          AND lv.start_date <= :shift_date AND lv.end_date >= :shift_date
        """,
        ("staff_id", "leave_type", "start_date", "end_date", "status"),
    ),
    passes=lambda rows, p: not rows,
    explain=lambda rows, p: (
        "no approved leave on the shift date"
        if not rows
        else f"on approved {rows[0]['leave_type']} leave {rows[0]['start_date']} to {rows[0]['end_date']}"
    ),
)

HAS_REQUIRED_SKILL = Check(
    "has_required_skill",
    "Staff member holds the skill the ward's demand requires",
    Query(
        "required_skill_match",
        """
        SELECT d.required_skill AS required_skill, sk.skill AS held_skill
        FROM {demand} d
        LEFT JOIN {staff_skills} sk ON sk.skill = d.required_skill AND sk.staff_id = :staff_id
        WHERE d.ward = :ward AND d.shift_date = :shift_date AND d.shift_type = :shift_type
        """,
        ("required_skill", "held_skill"),
    ),
    passes=lambda rows, p: bool(rows) and rows[0]["held_skill"] is not None,
    explain=lambda rows, p: (
        "no demand record to compare skills against"
        if not rows
        else f"holds {rows[0]['required_skill']}"
        if rows[0]["held_skill"] is not None
        else f"missing required skill {rows[0]['required_skill']}"
    ),
)

UNDER_WEEKLY_HOUR_CAP = Check(
    "under_weekly_hour_cap",
    "Rostered hours this week plus this shift stay within the staff member's cap",
    Query(
        "weekly_hours",
        """
        SELECT s.staff_id AS staff_id, s.weekly_hour_cap AS weekly_hour_cap,
               COALESCE(SUM(r.hours), 0) AS rostered_hours
        FROM {staff} s
        LEFT JOIN {roster} r ON r.staff_id = s.staff_id
             AND r.shift_date >= :week_start AND r.shift_date <= :week_end
        WHERE s.staff_id = :staff_id
        GROUP BY s.staff_id, s.weekly_hour_cap
        """,
        ("staff_id", "weekly_hour_cap", "rostered_hours"),
    ),
    passes=lambda rows, p: bool(rows)
    and float(rows[0]["rostered_hours"]) + float(p["shift_hours"]) <= float(rows[0]["weekly_hour_cap"]),
    explain=lambda rows, p: (
        "staff member not found"
        if not rows
        else f"{rows[0]['rostered_hours']}h rostered + {p['shift_hours']}h shift vs {rows[0]['weekly_hour_cap']}h cap"
    ),
)

NO_CONFLICTING_SHIFT = Check(
    "no_conflicting_shift",
    "No rostered shift overlaps this one or breaks the 10-hour rest gap",
    Query(
        "conflicting_shifts",
        """
        SELECT r.shift_id AS shift_id, r.ward AS ward, r.shift_date AS shift_date,
               r.shift_type AS shift_type, r.start_ts AS start_ts, r.end_ts AS end_ts
        FROM {roster} r
        WHERE r.staff_id = :staff_id AND r.start_ts < :rest_until AND r.end_ts > :rest_from
        """,
        ("shift_id", "ward", "shift_date", "shift_type", "start_ts", "end_ts"),
    ),
    passes=lambda rows, p: not rows,
    explain=lambda rows, p: (
        "no overlapping shift and rest gap respected"
        if not rows
        else f"rostered {rows[0]['ward']} {rows[0]['shift_type']} {rows[0]['start_ts']} to {rows[0]['end_ts']} (< 10h rest)"
    ),
)

# --- ward policy -------------------------------------------------------------

AGENCY_ALLOWED = Check(
    "agency_allowed_for_ward",
    "Ward policy permits booking agency staff",
    Query(
        "ward_agency_policy",
        "SELECT w.ward AS ward, w.agency_allowed AS agency_allowed FROM {wards} w WHERE w.ward = :ward",
        ("ward", "agency_allowed"),
    ),
    passes=lambda rows, p: bool(rows) and _truthy(rows[0]["agency_allowed"]),
    explain=lambda rows, p: (
        f"ward {p['ward']} not found"
        if not rows
        else "agency allowed"
        if _truthy(rows[0]["agency_allowed"])
        else "ward policy forbids agency"
    ),
)

ALL_CHECKS = {
    c.name: c
    for c in (
        WARD_UNDERSTAFFED,
        WARD_ADEQUATELY_STAFFED,
        STAFF_NOT_ON_LEAVE,
        HAS_REQUIRED_SKILL,
        UNDER_WEEKLY_HOUR_CAP,
        NO_CONFLICTING_SHIFT,
        AGENCY_ALLOWED,
    )
}
