"""Fire an action that passed the gate.

Delivery is simulated: the action is written to logs/outbox.jsonl as the
message a real integration (SMS, rostering system, agency portal) would send.
Nothing is written back to Zetaris; the agent's data access stays read-only.
"""

from __future__ import annotations

from typing import Any

from app.audit import AuditLog
from app.models import Candidate, ShiftRequest


def fire(candidate: Candidate, request: ShiftRequest, audit: AuditLog, display_name: str = "") -> dict[str, Any]:
    if candidate.action == "offer_shift_to_staff":
        who = f"{display_name} ({candidate.params['staff_id']})" if display_name else candidate.params["staff_id"]
        message = f"Shift offer sent to {who}: {request.label()}."
    elif candidate.action == "escalate_to_agency":
        message = f"Agency request raised for 1 nurse: {request.label()}."
    else:
        message = f"No cover needed: {request.label()} is fully staffed. Nobody contacted."
    record = {
        "action": candidate.action,
        "params": candidate.params,
        "request": request.to_dict(),
        "message": message,
        "delivery": "simulated",
    }
    audit.outbox(record)
    return record
