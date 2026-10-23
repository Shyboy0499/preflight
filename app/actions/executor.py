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


def fire(candidate: Candidate, request: ShiftRequest, audit: AuditLog, display_name: str = "", nurses: int = 1,
         warnings: list[str] | None = None, auto_send: bool = True) -> dict[str, Any]:
    """Record an action that passed every check.

    auto_send=True (CLI, eval): delivered at once (simulated).
    auto_send=False (UI): prepared and held for the manager to send or discard;
    it still counts as open cover so a repeat request does not prepare it twice.
    """
    if candidate.action == "offer_shift_to_staff":
        who = f"{display_name} ({candidate.params['staff_id']})" if display_name else candidate.params["staff_id"]
        message = f"Shift offer {'sent ' if auto_send else ''}to {who}: {request.label()}."
    elif candidate.action == "escalate_to_agency":
        message = f"Agency request {'raised ' if auto_send else ''}for {nurses} nurse{'s' if nurses != 1 else ''}: {request.label()}."
    else:
        message = f"No cover needed: {request.label()} is fully staffed. Nobody contacted."
    record = {
        "action_id": f"act_{uuid.uuid4().hex[:10]}",
        "action": candidate.action,
        "params": candidate.params,
        "nurses": nurses if candidate.action != "no_cover_needed" else 0,
        "request": request.to_dict(),
        "message": message,
        "warnings": list(warnings or []),
        "delivery": "simulated" if auto_send or candidate.action == "no_cover_needed" else "awaiting_manager",
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
    amended = {r["ref"]: r["nurses"] for r in records if r.get("action") == "amend"}  # latest wins
    return [
        {**r, "nurses": amended.get(r["action_id"], r.get("nurses"))}
        for r in records
        if r.get("action") in ("offer_shift_to_staff", "escalate_to_agency")
        and r.get("request") == request.to_dict()
        and r.get("action_id") and r["action_id"] not in withdrawn
    ]


def amend_agency(record: dict[str, Any], nurses: int, reason: str, audit: AuditLog) -> dict[str, Any]:
    """Reduce an open agency request that now asks for more nurses than are missing."""
    out = {
        "action_id": f"act_{uuid.uuid4().hex[:10]}",
        "action": "amend",
        "ref": record["action_id"],
        "amended_action": record["action"],
        "params": record["params"],
        "request": record["request"],
        "nurses": nurses,
        "reason": reason,
        "message": f"Agency request reduced from {record.get('nurses')} to {nurses} nurse{'s' if nurses != 1 else ''}: {reason}.",
        "delivery": "simulated",
    }
    audit.outbox(out)
    return out


def manager_decision(outbox_path, action_id: str, decision: str, message: str) -> dict[str, Any]:
    """The manager sends (simulated) or discards a prepared action. Appends to the outbox."""
    import json
    from datetime import datetime, timezone

    assert decision in ("send", "discard")
    out = {
        "action_id": f"act_{uuid.uuid4().hex[:10]}",
        "action": "withdraw" if decision == "discard" else "send",
        "ref": action_id,
        "reason": "discarded by the manager" if decision == "discard" else "sent by the manager",
        "message": ("Discarded by the manager: " if decision == "discard" else "Sent by the manager (simulated): ") + message,
        "delivery": "simulated",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with outbox_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(out) + "\n")
    return out


def delivery_states(outbox_path) -> dict[str, str]:
    """action_id -> 'sent' | 'discarded' for prepared actions the manager has handled."""
    import json

    if not outbox_path.exists():
        return {}
    states: dict[str, str] = {}
    for line in outbox_path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line) if line.strip() else {}
        if r.get("action") == "send":
            states[r["ref"]] = "sent"
        elif r.get("action") == "withdraw" and r.get("reason") == "discarded by the manager":
            states[r["ref"]] = "discarded"
    return states
