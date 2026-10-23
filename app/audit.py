"""Append-only JSONL audit trail.

logs/trace.jsonl      every step of every run (request, facts, recall, plan, checks, decision)
logs/decisions.jsonl  one line per run: the decision and why
logs/outbox.jsonl     actions that fired (simulated delivery)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.roles import role_of


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")


class AuditLog:
    def __init__(self, logs_dir: Path, run_id: str):
        self.logs_dir = logs_dir
        self.run_id = run_id
        self.seq = 0
        self.events: list[dict[str, Any]] = []

    def event(self, kind: str, **data: Any) -> None:
        self.seq += 1
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "seq": self.seq,
            "kind": kind,
            "role": role_of(kind),
            **data,
        }
        self.events.append(record)
        _write(self.logs_dir / "trace.jsonl", record)

    def decision(self, record: dict[str, Any]) -> None:
        _write(self.logs_dir / "decisions.jsonl", {"run_id": self.run_id, **record})

    def outbox(self, record: dict[str, Any]) -> None:
        _write(self.logs_dir / "outbox.jsonl", {"run_id": self.run_id, **record})
