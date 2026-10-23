"""The precondition gate: an action may fire only if every one of its checks passes.

Every check runs (no short-circuit) so the log carries the full evidence. A
check whose data cannot be fetched counts as failed (fail closed).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.actions.library import LIBRARY
from app.adapters.zetaris import DataSourceUnavailable, SqlSource, UnsafeQuery
from app.models import MIN_REST, Candidate, ShiftRequest
from app.preconditions.checks import ALL_CHECKS, shortfall


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn (advisory check failed; does not block) | fail | error
    explanation: str
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in ("pass", "warn")


@dataclass
class GateResult:
    candidate: Candidate
    results: list[CheckResult]
    shortfall: int | None = None  # nurses still missing when this gate ran (None if not measured)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [r for r in self.results if r.status == "warn"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "passed": self.passed,
            "shortfall": self.shortfall,
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "explanation": r.explanation,
                    "evidence": r.evidence,
                    "provenance": r.provenance,
                }
                for r in self.results
            ],
        }


def build_params(request: ShiftRequest, candidate: Candidate, pending_cover: int = 0) -> dict[str, Any]:
    week_start = request.shift_date - timedelta(days=request.shift_date.weekday())
    return {
        "ward": request.ward,
        "shift_date": request.shift_date,
        "shift_type": request.shift_type,
        "shift_start": request.start,
        "shift_end": request.end,
        "shift_hours": request.hours,
        "rest_from": request.start - MIN_REST,
        "rest_until": request.end + MIN_REST,
        "week_start": week_start,
        "week_end": week_start + timedelta(days=6),
        "pending_cover": pending_cover,
        **candidate.params,
    }


def evaluate(source: SqlSource, request: ShiftRequest, candidate: Candidate, pending_cover: int = 0) -> GateResult:
    spec = LIBRARY.get(candidate.action)
    if spec is None:
        return GateResult(candidate, [CheckResult("known_action", "fail", f"unknown action {candidate.action!r}")])
    missing = [p for p in spec.params if p not in candidate.params]
    if missing:
        return GateResult(candidate, [CheckResult("params_present", "fail", f"missing params: {', '.join(missing)}")])

    params = build_params(request, candidate, pending_cover)

    def run_check(name: str) -> CheckResult:
        check = ALL_CHECKS[name]
        try:
            fetched = source.run(check.query, params)
        except (DataSourceUnavailable, UnsafeQuery) as exc:
            return CheckResult(name, "error", f"could not verify, treated as failed: {exc}")
        try:
            status = "pass" if check.passes(fetched.rows, params) else ("warn" if check.advisory else "fail")
            explanation = check.explain(fetched.rows, params)
        except (KeyError, TypeError, ValueError) as exc:
            status, explanation = "error", f"evidence rows malformed, treated as failed: {exc!r}"
        return CheckResult(name, status, explanation, evidence=fetched.rows, provenance=fetched.provenance())

    # The checks are independent read-only queries: run them concurrently when the
    # source allows it (live Zetaris round-trips dominate). Results keep spec order.
    if getattr(source, "concurrent", False) and len(spec.checks) > 1:
        with ThreadPoolExecutor(max_workers=len(spec.checks)) as pool:
            results = list(pool.map(run_check, spec.checks))
    else:
        results = [run_check(name) for name in spec.checks]
    gate = GateResult(candidate, results)
    staffing = next((r for r in results if r.name == "ward_actually_understaffed" and r.status != "error"), None)
    if staffing is not None:
        gate.shortfall = max(0, shortfall(staffing.evidence, params))
    return gate
