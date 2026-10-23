"""Write demo/storyboard.json and demo/script.md: narration per scene, in the Demo Video Guide order.

Every claim in the live-run scenes matches the recorded run's trace (logs/trace.jsonl) and frames.
"""
from __future__ import annotations

import json
from pathlib import Path

HAS_EXPLORER = (Path("demo/frames/explorer/timeline.json")).exists()
IMPACT_LINE = ("For a ward manager, that means a safe cover plan in under a minute, and every no comes with the record "
               "that proves it.")

scenes = [
    # 1. Problem + track
    {"id": "01_title", "section": "Problem and track", "visual": {"type": "image", "path": "demo/assets/title.png"},
     "narration": "This is Preflight, our entry for Track 03: Reasoning Architecture."},
    {"id": "02_problem", "section": "Problem and track", "visual": {"type": "image", "path": "demo/assets/problem.png"},
     "narration": "When a nurse calls in sick, the manager has to find cover fast. Rosters, leave, skills and demand live in "
                  "different systems, so it is easy to book someone who is on leave, unqualified, over their hours, or without rest."},
    # 2. Solution
    {"id": "03_solution", "section": "Solution", "visual": {"type": "image", "path": "demo/assets/solution.png"},
     "narration": "Preflight is a shift-cover agent that checks before it acts. You ask in plain English. It proposes cover, "
                  "proves every precondition with SQL against live data, and acts only when every check passes. When a check "
                  "fails, it shows you the evidence."},
    # 3. Agents and the loop
    {"id": "04_agents", "section": "Agents and the loop", "visual": {"type": "pan", "path": "demo/assets/architecture.png"},
     "narration": "Four roles share the work. The Planner, NVIDIA Nemotron called directly, reads the request and ranks staff. "
                  "The Verifier, a SQL gate over Zetaris, is the only role that can let an action fire. The Investigator explains "
                  "refusals, and Memory, Meterless H-MEM, recalls what worked before and learns from what fires."},
    {"id": "05_loop", "section": "Agents and the loop", "visual": {"type": "image", "path": "demo/assets/trace.png"},
     "narration": "When the Verifier refuses candidates, their reasons go back to the Planner for one re-plan round, before "
                  "Preflight pays for agency staff. Every step is logged with its role, so the whole loop can be audited."},
    # 4. Live run
    {"id": "06_run_a", "section": "Live run", "visual": {"type": "frames", "dir": "demo/frames/A",
                                                         "tags": ["intro", "typed", "running", "outcome", "verifier", "send", "sent"],
                                                         "fast_tags": ["running"], "fast_max": 2.5},
     "narration": "Now a live run, on real Zetaris and NVIDIA Nemotron. Ward 4B needs night cover tomorrow. The Planner proposes "
                  "Priya. The Verifier runs six checks across three systems: understaffing, leave, skill, hours, rest, and pending "
                  "leave. All of them pass, so Preflight prepares the offer, and the manager presses Send."},
    {"id": "07_toggle", "section": "Live run", "visual": {"type": "image", "path": "demo/assets/toggle.png"},
     "narration": "Then HR approves Priya's annual leave, in the Postgres leave system."},
    {"id": "08_run_b", "section": "Live run", "visual": {"type": "frames", "dir": "demo/frames/B",
                                                         "tags": ["typed", "running", "recheck", "outcome"],
                                                         "fast_tags": ["running"], "fast_max": 2.0,
                                                         "keep_real": ["recheck"]},
     "narration": "Same request again. First, Preflight re-checks the offer it already sent. Priya is now on approved leave, so "
                  "the offer is withdrawn, with the leave record as evidence. A naive agent that books its top pick would have "
                  "sent her anyway."},
    {"id": "09_run_b_verifier", "section": "Live run", "visual": {"type": "frames", "dir": "demo/frames/B",
                                                                  "tags": ["verifier", "send", "sent"]},
     "narration": "The Planner's next picks are refused for real reasons. Tom lacks the cardiac skill, Sam is at his hour "
                  "cap, and Raj would not get ten hours' rest. Those three reasons go back to the Planner. Its re-plan, and "
                  "the rest of the pool, are refused on the same rest rule. So Preflight asks the agency for exactly one "
                  "nurse."},
]
# 5. Zetaris and Meterless doing real work
if HAS_EXPLORER:
    scenes.append({"id": "10_zetaris_ui", "section": "Zetaris and Meterless", "visual": {
        "type": "frames", "dir": "demo/frames/explorer", "tags": ["explorer"]},
        "narration": "Zetaris federates three sources here: a CSV file source for rosters and demand, a Postgres leave system, "
                     "and a REST skills service."})
    zetaris_sql = ("One check, ward actually understaffed, joins the demand, roster and leave tables across two of those "
                   "sources in a single SQL statement, sent to Zetaris over REST.")
else:
    zetaris_sql = ("Zetaris federates three sources: a CSV file source for rosters and demand, a Postgres leave system, and a "
                   "REST skills service. One check joins the demand, roster and leave tables across two of them in a single "
                   "SQL statement.")
scenes += [
    {"id": "11_zetaris_sql", "section": "Zetaris and Meterless", "visual": {"type": "image", "path": "demo/assets/sql.png"},
     "narration": zetaris_sql},
    {"id": "12_meterless", "section": "Zetaris and Meterless", "visual": {"type": "frames", "dir": "demo/frames/B",
                                                                          "tags": ["memory"]},
     "narration": "Meterless does real work too. H-MEM recalled that Priya had covered this shift, and lowered its confidence "
                  "when the Verifier refused her. The World Model holds the facts pulled from Zetaris, fifty-five observations "
                  "from five queries, each with its provenance."},
    # 6. Output and impact
    {"id": "13a_impact_line", "section": "Output and impact", "visual": {"type": "frames", "dir": "demo/frames/B",
                                                                           "tags": ["sent"]},
     "narration": IMPACT_LINE},
    {"id": "13_impact", "section": "Output and impact", "visual": {"type": "image", "path": "demo/assets/scoreboard.png"},
     "narration": "To measure reliability, we sent twelve hundred random requests across two hundred synthetic hospital worlds, "
                  "and re-checked every action with an independent checker. Preflight made zero people-safety violations. A "
                  "naive agent made them in twenty-three point two percent of requests."},
    {"id": "14_close", "section": "Output and impact", "visual": {"type": "image", "path": "demo/assets/close.png"},
     "narration": "Preflight. Check before you act."},
]

Path("demo/storyboard.json").write_text(json.dumps({"voice": "Karen (Premium)", "scenes": scenes}, indent=1))

md = ["# Preflight demo video: narration script", "",
      "AI-generated: voice-over by macOS `say -v \"Karen (Premium)\"`; screen recording by Playwright driving the real "
      "Streamlit UI in live mode (real Zetaris, NVIDIA Nemotron called directly; Meterless Nightshift not used). "
      "Waits are sped up; no output is staged or edited. Built by `scripts/video/` "
      "(record_ui.py, make_cards.py, storyboard.py, build.py). Captions are the narration below, burned in.", ""]
section = None
for s in scenes:
    if s["section"] != section:
        section = s["section"]
        md += [f"## {section}", ""]
    md += [f"**{s['id']}** ({s['visual']['type']}): {s['narration']}", ""]
Path("demo/script.md").write_text("\n".join(md))
words = sum(len(s["narration"].split()) for s in scenes)
print(f"{len(scenes)} scenes, {words} words, explorer scene: {HAS_EXPLORER}")
