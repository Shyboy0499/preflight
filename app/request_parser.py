"""Turn a plain-English cover request into a ShiftRequest.

The generator extracts the fields; the result is validated here and the
deterministic rules are used whenever the model's answer is unusable.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from app.adapters.llm import Generator, extract_json
from app.models import SHIFT_TYPES, ShiftRequest

SYSTEM = """TASK: parse_request
You extract a hospital shift-cover request into JSON.
Reply with only: {"ward": "<ward code>", "shift_date": "YYYY-MM-DD", "shift_type": "day|evening|night"}
Use null for anything the text does not say. Resolve relative dates from "today"."""

_WARD = re.compile(r"\bward\s+([A-Za-z0-9]{1,6})\b", re.I)
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WARD_CODE = re.compile(r"^[A-Z0-9]{1,6}$")


def parse_with_rules(text: str, today_iso: str) -> dict[str, Any]:
    today = date.fromisoformat(today_iso)
    t = text.lower()
    ward = m.group(1).upper() if (m := _WARD.search(text)) else None

    shift_date = None
    if m := _ISO.search(text):
        shift_date = m.group(1)
    elif "tomorrow" in t:
        shift_date = (today + timedelta(days=1)).isoformat()
    elif "tonight" in t or "today" in t:
        shift_date = today.isoformat()
    else:
        for i, day in enumerate(_WEEKDAYS):
            if day in t:
                shift_date = (today + timedelta(days=(i - today.weekday()) % 7 or 7)).isoformat()
                break

    shift_type = None
    if "night" in t or "tonight" in t:
        shift_type = "night"
    elif "evening" in t or "late shift" in t or " pm" in t:
        shift_type = "evening"
    elif re.search(r"\b(day|morning|early)\b", t):
        shift_type = "day"
    return {"ward": ward, "shift_date": shift_date, "shift_type": shift_type}


def _validate(fields: dict[str, Any], text: str, today: date) -> ShiftRequest | None:
    try:
        ward = str(fields["ward"]).upper()
        shift_date = date.fromisoformat(str(fields["shift_date"]))
        shift_type = str(fields["shift_type"]).lower()
    except (KeyError, TypeError, ValueError):
        return None
    if not _WARD_CODE.match(ward) or shift_type not in SHIFT_TYPES:
        return None
    if not (today - timedelta(days=1) <= shift_date <= today + timedelta(days=30)):
        return None
    return ShiftRequest(ward, shift_date, shift_type, text)


def parse_request(text: str, today: date, generator: Generator) -> tuple[ShiftRequest | None, str]:
    """Returns (request or None, which path produced it)."""
    payload = json.dumps({"text": text, "today": today.isoformat()})
    try:
        fields = extract_json(generator(f"today: {today.isoformat()}\n<input>{payload}</input>", SYSTEM))
        parsed = _validate(fields, text, today)
        if parsed:
            return parsed, "generator"
    except (ValueError, TypeError):
        pass
    return _validate(parse_with_rules(text, today.isoformat()), text, today), "rules"
