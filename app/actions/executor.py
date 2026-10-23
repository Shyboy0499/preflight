"""Fire an action that passed the gate.

Delivery is simulated: the action is written to logs/outbox.jsonl as the
message a real integration (SMS, rostering system, agency portal) would send.
Nothing is written back to Zetaris; the agent's data access stays read-only.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.audit import AuditLog
from app.models import Candidate, ShiftRequest


def fire(candidate: Candidate, request: ShiftRequest, audit: AuditLog, display_name: str = "", nurses: int = 1) -> dict[str, Any]:
    if candidate.action == "offer_shift_to_staff":
        who = f"{display_name} ({candidate.params['staff_id']})" if display_name else candidate.params["staff_id"]
        message = f"Shift offer sent to {who}: {request.label()}."
    elif candidate.action == "escalate_to_agency":
        message = f"Agency request raised for {nurses} nurse{'s' if nurses != 1 else ''}: {request.label()}."
    else:
        message = f"No cover needed: {request.label()} is fully staffed. Nobody contacted."
    record = {
        "action_id": f"act_{uuid.uuid4().hex[:10]}",
        "action": candidate.action,
        "params": candidate.params,
        "nurses": nurses if candidate.action != "no_cover_needed" else 0,
        "request": request.to_dict(),
        "message": message,
        "delivery": "simulated",
    }
    audit.outbox(record)
    return record


def withdraw(record: dict[str, Any], reason: str, audit: AuditLog, display_name: str = "") -> dict[str, Any]:
    """Cancel an earlier action that no longer passes its checks (simulated delivery)."""
    if record["action"] == "offer_shift_to_staff":
        who = f"{display_name} ({record['params']['staff_id']})" if display_name else record["params"]["staff_id"]
        what = f"Shift offer to {who}"
    else:
        what = f"Agency request for {record.get('nurses', 1)} nurse(s)"
    out = {
        "action_id": f"act_{uuid.uuid4().hex[:10]}",
        "action": "withdraw",
        "ref": record["action_id"],
        "withdrawn_action": record["action"],
        "params": record["params"],
        "request": record["request"],
        "reason": reason,
        "message": f"{what} withdrawn: {reason}.",
        "delivery": "simulated",
    }
    audit.outbox(out)
    return out


def open_actions(outbox_path, request: ShiftRequest) -> list[dict[str, Any]]:
    """Offers and agency requests already sent for this exact shift and not withdrawn, oldest first."""
    import json

    if not outbox_path.exists():
        return []
    records = [json.loads(line) for line in outbox_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    withdrawn = {r.get("ref") for r in records if r.get("action") == "withdraw"}
    return [
        r for r in records
        if r.get("action") in ("offer_shift_to_staff", "escalate_to_agency")
        and r.get("request") == request.to_dict()
        and r.get("action_id") and r["action_id"] not in withdrawn
    ]
