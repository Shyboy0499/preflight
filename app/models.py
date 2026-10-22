"""Shared value types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

SHIFT_TYPES = ("day", "evening", "night")

# Local shift pattern: start time and paid hours. Night ends the next morning.
SHIFT_TIMES: dict[str, tuple[time, time, float]] = {
    "day": (time(7, 0), time(15, 30), 8),
    "evening": (time(15, 0), time(23, 30), 8),
    "night": (time(21, 0), time(7, 30), 10),
}
MIN_REST = timedelta(hours=10)


@dataclass(frozen=True)
class ShiftRequest:
    ward: str
    shift_date: date
    shift_type: str
    raw_text: str = ""

    @property
    def start(self) -> datetime:
        return datetime.combine(self.shift_date, SHIFT_TIMES[self.shift_type][0])

    @property
    def end(self) -> datetime:
        end = datetime.combine(self.shift_date, SHIFT_TIMES[self.shift_type][1])
        return end + timedelta(days=1) if end <= self.start else end

    @property
    def hours(self) -> float:
        return SHIFT_TIMES[self.shift_type][2]

    def label(self) -> str:
        return f"Ward {self.ward} {self.shift_type} shift {self.shift_date.isoformat()}"

    def to_dict(self) -> dict[str, Any]:
        return {"ward": self.ward, "shift_date": self.shift_date.isoformat(), "shift_type": self.shift_type}


@dataclass(frozen=True)
class Candidate:
    """One concrete action to try, e.g. offer_shift_to_staff(staff_id=S001)."""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    origin: str = "planner"  # planner | memory | default

    def key(self) -> tuple[str, tuple]:
        return self.action, tuple(sorted(self.params.items()))

    def label(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.action}({args})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
