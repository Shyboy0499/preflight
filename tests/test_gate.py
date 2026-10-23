from datetime import date
from pathlib import Path

import pytest

from app.adapters.zetaris import DataSourceUnavailable, DuckDBSampleSource
from app.models import Candidate, ShiftRequest
from app.preconditions.gate import evaluate

DATA = Path(__file__).resolve().parent.parent / "data" / "sample"
NIGHT = ShiftRequest("4B", date(2026, 10, 27), "night")
DAY = ShiftRequest("4B", date(2026, 10, 27), "day")


@pytest.fixture(scope="module")
def src_a():
    return DuckDBSampleSource(DATA / "scenario_a")


@pytest.fixture(scope="module")
def src_b():
    return DuckDBSampleSource(DATA / "scenario_b")


def offer(staff_id):
    return Candidate("offer_shift_to_staff", {"staff_id": staff_id})


def statuses(result):
    return {r.name: r.status for r in result.results}


def test_offer_to_priya_fires_when_she_is_available(src_a):
    result = evaluate(src_a, NIGHT, offer("S001"))
    assert result.passed, statuses(result)
    assert len(result.results) == 5


def test_offer_to_priya_refused_with_leave_evidence_when_on_leave(src_b):
    result = evaluate(src_b, NIGHT, offer("S001"))
    assert not result.passed
    assert [r.name for r in result.failed] == ["staff_not_on_leave"]
    leave = result.failed[0]
    assert leave.evidence == [
        {"staff_id": "S001", "leave_type": "annual", "start_date": "2026-10-26", "end_date": "2026-10-30", "status": "approved"}
    ]
    assert leave.provenance["query"] == "leave_overlapping_shift"
    assert "preflight.leave" in leave.provenance["sql"]


@pytest.mark.parametrize(
    "staff_id, failing_check",
    [("S003", "under_weekly_hour_cap"), ("S008", "no_conflicting_shift"), ("S002", "has_required_skill")],
)
def test_each_unsuitable_candidate_fails_exactly_one_check(src_b, staff_id, failing_check):
    result = evaluate(src_b, NIGHT, offer(staff_id))
    assert [r.name for r in result.failed] == [failing_check]
    assert result.failed[0].evidence or failing_check == "has_required_skill"


def test_staffing_excludes_nurses_on_approved_leave(src_a):
    result = evaluate(src_a, NIGHT, Candidate("no_cover_needed"))
    assert not result.passed
    row = result.results[0].evidence[0]
    assert (row["available_rostered"], row["required_nurses"]) == (3, 4)


def test_no_cover_needed_fires_when_day_shift_is_full(src_a):
    assert evaluate(src_a, DAY, Candidate("no_cover_needed")).passed
    assert not evaluate(src_a, DAY, offer("S001")).passed  # not understaffed


def test_escalate_to_agency_passes_when_understaffed(src_b):
    assert evaluate(src_b, NIGHT, Candidate("escalate_to_agency")).passed


def test_unknown_ward_fails_every_action(src_a):
    ghost = ShiftRequest("9Z", date(2026, 10, 27), "night")
    for c in (Candidate("no_cover_needed"), offer("S001"), Candidate("escalate_to_agency")):
        assert not evaluate(src_a, ghost, c).passed


class _DownSource:
    name = "down"
    tables = {}

    def run(self, query, params=None):
        raise DataSourceUnavailable("zetaris: timed out")


def test_gate_fails_closed_when_data_source_is_down():
    result = evaluate(_DownSource(), NIGHT, offer("S001"))
    assert not result.passed
    assert {r.status for r in result.results} == {"error"}


def test_unknown_action_and_missing_params_never_pass(src_a):
    assert not evaluate(src_a, NIGHT, Candidate("book_everyone")).passed
    assert not evaluate(src_a, NIGHT, Candidate("offer_shift_to_staff", {})).passed


def test_injection_through_params_is_refused(src_a):
    result = evaluate(src_a, NIGHT, offer("S001' OR '1'='1"))
    assert not result.passed
    staff_checks = {"staff_not_on_leave", "has_required_skill", "under_weekly_hour_cap", "no_conflicting_shift"}
    assert {r.name for r in result.results if r.status == "error"} == staff_checks


LIVE_TABLES = {
    "staff": "PREFLIGHT_FILES.staff",
    "roster": "PREFLIGHT_FILES.roster",
    "demand": "PREFLIGHT_FILES.demand",
    "wards": "PREFLIGHT_FILES.wards",
    "leave": "preflight_pg.leave",
    "staff_skills": "PREFLIGHT_API.staff_skills",
}


@pytest.mark.parametrize(
    "check_name, sources",
    [
        ("ward_actually_understaffed", {"PREFLIGHT_FILES", "preflight_pg"}),
        ("has_required_skill", {"PREFLIGHT_FILES", "PREFLIGHT_API"}),
    ],
)
def test_key_checks_join_across_zetaris_sources_in_one_query(check_name, sources):
    from app.adapters.zetaris import render
    from app.preconditions.checks import ALL_CHECKS
    from app.preconditions.gate import build_params

    sql = render(ALL_CHECKS[check_name].query, build_params(NIGHT, offer("S001")), LIVE_TABLES)
    used = {name.split(".")[0] for name in LIVE_TABLES.values() if name in sql}
    assert used == sources


def test_concurrent_sources_give_identical_results_in_spec_order(src_b):
    class Concurrent:
        concurrent = True
        name = "concurrent"
        tables = src_b.tables

        def run(self, query, params=None):
            import threading
            with lock:  # duckdb connection is not thread-safe; zetaris over httpx is
                return src_b.run(query, params)

    import threading
    lock = threading.Lock()
    serial = evaluate(src_b, NIGHT, offer("S008"))
    parallel = evaluate(Concurrent(), NIGHT, offer("S008"))
    assert [(r.name, r.status, r.explanation) for r in parallel.results] == [(r.name, r.status, r.explanation) for r in serial.results]
