#!/usr/bin/env bash
# Reset the live demo to its starting state (scenario A):
#   Priya's leave removed from the Postgres leave system, open actions archived, H-MEM memory cleared.
# Usage: scripts/demo_reset.sh            (live mode: needs the preflight-postgres container)
#        scripts/demo_reset.sh --sample   (sample mode: only archives open actions and clears memory)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
LOGS="${PREFLIGHT_LOGS_DIR:-logs}"
STATE="${PREFLIGHT_STATE_DIR:-state}"

if [[ "${1:-}" != "--sample" ]]; then
  "$PY" scripts/demo_toggle_leave.py off
fi
if [[ -f "$LOGS/outbox.jsonl" ]]; then
  mv "$LOGS/outbox.jsonl" "$LOGS/outbox.archived-$(date +%s).jsonl"
  echo "Open actions archived ($LOGS/outbox.jsonl)"
fi
rm -rf "$STATE"
echo "H-MEM memory cleared ($STATE/). Demo reset: next run starts at scenario A."
