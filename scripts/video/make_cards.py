"""Build the video's cards from real artifacts: the live B run trace, the toggle output, the eval results."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cards import bullets_card, code_card, scoreboard_card, title_card  # noqa: E402

A = Path("demo/assets")
A.mkdir(parents=True, exist_ok=True)
NAMES = {"S001": "Priya", "S002": "Tom", "S003": "Sam", "S008": "Raj", "S009": "Kai", "S010": "Noor",
         "S011": "Ellie", "S012": "Omar", "S013": "Zoe"}

events = [json.loads(l) for l in open("logs/trace.jsonl")]
run_b = [e["run_id"] for e in events if e["kind"] == "request"][-1]
b = [e for e in events if e["run_id"] == run_b]


def who(params: dict) -> str:
    sid = params.get("staff_id")
    return f"{sid} {NAMES.get(sid, '')}".strip() if sid else "agency"


# --- trace card: Planner -> Verifier -> Planner, straight from trace.jsonl -------
lines = [f"logs/trace.jsonl   run {run_b}   (one JSON line per step; condensed)"]
lines.append(f"{'seq':>3}  {'role':<12} {'kind':<16} detail")
for e in b:
    k, r, s = e["kind"], e["role"], e["seq"]
    if k == "memory_recall":
        m = e["trace"]["returned"][0] if e["trace"]["returned"] else None
        lines.append(f"{s:>3}  {r:<12} {k:<16} {'recalled 4B night -> offer S001 Priya (score %.2f)' % m['score'] if m else 'none'}")
    elif k == "plan":
        lines.append(f"{s:>3}  {r:<12} {k:<16} round 1: " + ", ".join(who(c["params"]) for c in e["candidates"]))
    elif k == "recheck":
        bad = [c["name"] for c in e["checks"] if c["status"] != "pass"]
        lines.append(f"{s:>3}  {r:<12} {k:<16} earlier offer {who(e['candidate']['params'])}: {'PASS' if e['passed'] else 'FAIL ' + ', '.join(bad)}")
    elif k == "gate":
        bad = [c["name"] for c in e["checks"] if c["status"] != "pass"]
        lines.append(f"{s:>3}  {r:<12} {k:<16} {who(e['candidate']['params']) if e['candidate']['params'] else e['candidate']['action']}: "
                     + ("PASS" if e["passed"] else "FAIL " + ", ".join(bad)))
    elif k == "replan":
        lines.append(f"{s:>3}  {r:<12} {k:<16} round 2 from {len(e['refusals'])} refusals -> " + ", ".join(who(c["params"]) for c in e["candidates"]))
    elif k == "action_withdrawn":
        lines.append(f"{s:>3}  {r:<12} {k:<16} offer {who(e['params'])} withdrawn: staff_not_on_leave")
    elif k == "action_fired":
        lines.append(f"{s:>3}  {r:<12} {k:<16} {e['message'].split(':')[0]} sent")
    elif k == "memory_feedback":
        lines.append(f"{s:>3}  {r:<12} {k:<16} not_helpful: recalled S001 refused by the Verifier")
replan = next(e for e in b if e["kind"] == "replan")
raw = json.dumps({k: replan[k] for k in ("seq", "kind", "role")} | {"refusals": [{"staff_id": x["staff_id"], "failed_checks": x["failed_checks"]} for x in replan["refusals"][:2]]})
# (raw JSON for the replan step is in logs/trace.jsonl; the card shows the condensed lines only)
code_card(A / "trace.png", "Planner, Verifier, Planner again: the real trace", lines, size=19,
          note="Live run on real Zetaris + NVIDIA Nemotron. Every event carries its role.")

# --- toggle card: the real script output -----------------------------------------
toggle = Path("demo/toggle_on.txt").read_text().strip().splitlines()
code_card(A / "toggle.png", "HR approves Priya's leave (Postgres leave system)",
          ["$ python scripts/demo_toggle_leave.py on", *[re.sub(r"datetime\.date\((\d+), (\d+), (\d+)\)", r"\1-\2-\3", l) for l in toggle]],
          size=28, note="One row written to the leave system. Read back through Zetaris.")

# --- SQL card: the actual cross-source statement from run B ----------------------
sql = next(c["provenance"]["sql"] for e in b if e["kind"] == "gate" for c in e["checks"]
           if c["name"] == "ward_actually_understaffed" and c["provenance"].get("sql"))
sql = re.sub(r"\s+", " ", sql)
wrapped, cur = [], ""
for tok in re.split(r"(?=\b(?:FROM|LEFT JOIN|WHERE|AND|GROUP BY|SELECT)\b)", sql):
    tok = tok.strip()
    if tok:
        wrapped.append(tok[:110])
code_card(A / "sql.png", "One check, one SQL statement, three tables, two sources", wrapped[:22], size=19,
          note="PREFLIGHT_FILES = CSV file source · preflight_pg = Postgres leave system · sent to Zetaris over REST")

# --- scoreboard: from the committed eval results ---------------------------------
md = Path("evals/results/latest.md").read_text()
row = lambda label: re.search(rf"\| {re.escape(label)}[^|]*\| \**(\d+)\** \| \**(\d+)\** \((\d+\.\d)%\)", md)
ppl = row("**People-safety violations**")
unn = row("Unnecessary bookings")
runs = re.search(r"(\d+) runs in", md).group(1)
worlds = re.search(r"(\d+) random worlds", md).group(1)
scoreboard_card(A / "scoreboard.png", [
    ("People-safety violations", ppl.group(1), f"{ppl.group(2)} ({ppl.group(3)}%)"),
    ("Unnecessary bookings", unn.group(1), f"{unn.group(2)} ({unn.group(3)}%)"),
], f"{runs} random requests, {worlds} synthetic hospital worlds",
    "Every action re-checked by an independent oracle (evals/oracle.py). Reproduce: python -m evals.run_safety_eval")

# --- static cards ----------------------------------------------------------------
title_card(A / "title.png", "Preflight", "A shift-cover agent that checks before it acts",
           ["GenAI.works Open Agent Hackathon 2026", "Track 03: Reasoning Architecture"],
           footer="Zetaris · Meterless H-MEM + World Model · NVIDIA Nemotron · built with Claude Code")
bullets_card(A / "problem.png", "The 3 a.m. problem", [
    "A nurse calls in sick. The unit manager has to cover the shift.",
    "Rosters, leave, skills and patient demand live in different systems.",
    "Easy to book someone on leave, without the skill, over their hours, or without rest.",
], note="Track 03: Reasoning Architecture: turn data and reasoning into reliable decisions and actions")
bullets_card(A / "solution.png", "Preflight: check before you act", [
    "Ask in plain English: \"Ward 4B needs night cover tomorrow\".",
    "It proposes cover, then proves every precondition with SQL on live data.",
    "It acts only when every check passes, and shows the evidence when one fails.",
    "It re-checks what it already sent, and withdraws what is no longer valid.",
])
title_card(A / "close.png", "Preflight", "Check before you act.",
           ["github.com/Shyboy0499/preflight", "0 people-safety violations in 1,200 requests"],
           footer="Synthetic data only · NVIDIA Nemotron called directly · Zetaris · Meterless")
print("cards from run", run_b, "->", sorted(p.name for p in A.glob("*.png")))
