"""Random synthetic hospital worlds for the safety evaluation.

Each world is a folder of the same six CSVs the app reads (staff, staff_skills,
roster, leave, demand, wards) for the week of Mon 2026-10-26, with random
leave, skills, rosters, hour caps, demand and agency policy. Seeded, so every
run of the evaluation is reproducible.
"""

from __future__ import annotations

import csv
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

WEEK_START = date(2026, 10, 26)
WARDS = ("4B", "3A", "5C")
SKILLS = ("cardiac_telemetry", "night_competent", "paediatrics", "RN")
# Shift pattern as the hospital defines it: start, end, paid hours.
SHIFTS = {"day": (time(7, 0), time(15, 30), 8), "evening": (time(15, 0), time(23, 30), 8), "night": (time(21, 0), time(7, 30), 10)}


def shift_window(day: date, shift: str) -> tuple[datetime, datetime]:
    start_t, end_t, _ = SHIFTS[shift]
    start = datetime.combine(day, start_t)
    end = datetime.combine(day, end_t)
    return start, end + timedelta(days=1) if end <= start else end


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def generate_world(folder: Path, seed: int) -> dict:
    rng = random.Random(seed)
    folder.mkdir(parents=True, exist_ok=True)
    n_staff = rng.randint(8, 16)
    staff = [(f"S{i:03d}", f"Nurse {i:03d}", "RN", rng.choice(WARDS), rng.choice((24, 32, 38, 38, 40))) for i in range(1, n_staff + 1)]

    skills = []
    for sid, *_ in staff:
        skills.append((sid, "RN"))
        for skill in SKILLS[:-1]:
            if rng.random() < 0.5:
                skills.append((sid, skill))

    roster, shift_no = [], 1000
    days = [WEEK_START + timedelta(days=d) for d in range(7)]
    for sid, *_ in staff:
        for day in rng.sample(days, rng.randint(0, 5)):
            shift = rng.choice(list(SHIFTS))
            start, end = shift_window(day, shift)
            shift_no += 1
            roster.append((f"R{shift_no}", sid, rng.choice(WARDS), day.isoformat(), shift,
                           start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), SHIFTS[shift][2]))

    leave = []
    for sid, *_ in staff:
        if rng.random() < 0.3:
            first = WEEK_START + timedelta(days=rng.randint(-2, 6))
            leave.append((sid, rng.choice(("annual", "sick", "study")), first.isoformat(),
                          (first + timedelta(days=rng.randint(0, 4))).isoformat(), rng.choice(("approved", "approved", "pending"))))

    demand = []
    for ward in WARDS:
        for day in days:
            for shift in SHIFTS:
                demand.append((ward, day.isoformat(), shift, rng.randint(10, 30), rng.choice(("low", "medium", "high")),
                               rng.randint(1, 5), rng.choice(SKILLS)))

    wards = [(w, f"Ward {w}", rng.choice(("true", "true", "false"))) for w in WARDS]

    _write(folder / "staff.csv", ["staff_id", "name", "role", "home_ward", "weekly_hour_cap"], [list(s) for s in staff])
    _write(folder / "staff_skills.csv", ["staff_id", "skill"], [list(s) for s in skills])
    roster = roster or [("R0", "S999", "9Z", "2020-01-01", "day", "2020-01-01 07:00:00", "2020-01-01 15:30:00", 8)]
    _write(folder / "roster.csv", ["shift_id", "staff_id", "ward", "shift_date", "shift_type", "start_ts", "end_ts", "hours"], [list(r) for r in roster])
    _write(folder / "leave.csv", ["staff_id", "leave_type", "start_date", "end_date", "status"], [list(r) for r in leave] or [["S999", "annual", "2020-01-01", "2020-01-01", "approved"]])
    _write(folder / "demand.csv", ["ward", "shift_date", "shift_type", "patients", "acuity", "required_nurses", "required_skill"], [list(d) for d in demand])
    _write(folder / "wards.csv", ["ward", "ward_name", "agency_allowed"], [list(w) for w in wards])
    return {"seed": seed, "staff": len(staff), "roster": len(roster), "leave": len(leave)}


def random_request(rng: random.Random) -> tuple[str, str, str, str]:
    """(text, ward, shift_date, shift_type) phrased a few different ways."""
    ward, shift = rng.choice(WARDS), rng.choice(list(SHIFTS))
    day = WEEK_START + timedelta(days=rng.randint(0, 6))
    text = rng.choice((
        "Ward {w} needs {s} cover on {d}",
        "Can you find {s} cover for ward {w} on {d}?",
        "Someone called in sick, ward {w} {s} shift {d}",
    )).format(w=ward, s=shift, d=day.isoformat())
    return text, ward, day.isoformat(), shift
