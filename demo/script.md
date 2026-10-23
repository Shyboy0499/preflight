# Preflight demo video: narration script

AI-generated: voice-over by macOS `say -v "Karen (Premium)"`; screen recording by Playwright driving the real Streamlit UI in live mode (real Zetaris, NVIDIA Nemotron called directly; Meterless Nightshift not used), captured at 4K and shown with pan-and-zoom onto the region being narrated; captions sit in a fixed strip below the footage. Waits are sped up; no output is staged or edited. Built by `scripts/video/` (record_ui.py, make_cards.py, storyboard.py, build.py). Captions are the narration below, burned in.

## Problem and track

**01_title** (image): This is Preflight, our entry for Track 03: Reasoning Architecture.

**02_problem** (image): When a nurse calls in sick, the manager has to find cover fast. Rosters, leave, skills and demand live in different systems, so it is easy to book someone who is on leave, unqualified, over their hours, or without rest.

## Solution

**03_solution** (image): Preflight is a shift-cover agent that checks before it acts. You ask in plain English. It proposes cover, proves every precondition with SQL against live data, and acts only when every check passes. When a check fails, it shows you the evidence.

## Agents and the loop

**04_agents** (pan): Four roles share the work. The Planner, NVIDIA Nemotron called directly, reads the request and ranks staff. The Verifier, a SQL gate over Zetaris, is the only role that can let an action fire. The Investigator explains refusals, and Memory, Meterless H-MEM, recalls what worked before and learns from what fires.

**05_loop** (image): When the Verifier refuses candidates, their reasons go back to the Planner for one re-plan round, before Preflight pays for agency staff. Every step is logged with its role, so the whole loop can be audited.

## Live run

**06_run_a** (frames): Now a live run, on real Zetaris and NVIDIA Nemotron. Ward 4B needs night cover tomorrow. The Planner proposes Priya. The Verifier runs six checks across three systems: understaffing, leave, skill, hours, rest, and pending leave. All of them pass, so Preflight prepares the offer, and the manager presses Send.

**07_toggle** (image): Then HR approves Priya's annual leave, in the Postgres leave system.

**08_run_b** (frames): Same request again. First, Preflight re-checks the offer it already sent. Priya is now on approved leave, so the offer is withdrawn, with the leave record as evidence. A naive agent that books its top pick would have sent her anyway.

**09_run_b_verifier** (frames): The Planner's next picks are refused for real reasons. Tom lacks the cardiac skill, Sam is at his hour cap, and Raj would not get ten hours' rest. Those three reasons go back to the Planner. Its re-plan, and the rest of the pool, are refused on the same rest rule. So Preflight asks the agency for exactly one nurse.

## Zetaris and Meterless

**10_zetaris_ui** (frames): Zetaris federates three sources here: a CSV file source for rosters and demand, a Postgres leave system, and a REST skills service.

**11_zetaris_sql** (image): One check, ward actually understaffed, joins the demand, roster and leave tables across two of those sources in a single SQL statement, sent to Zetaris over REST.

**12_meterless** (frames): Meterless does real work too. H-MEM recalled that Priya had covered this shift, and lowered its confidence when the Verifier refused her. The World Model holds the facts pulled from Zetaris, fifty-five observations from five queries, each with its provenance.

## Output and impact

**13a_impact_line** (frames): For a ward manager, that means a safe cover plan in under a minute, and every no comes with the record that proves it.

**13_impact** (image): To measure reliability, we sent twelve hundred random requests across two hundred synthetic hospital worlds, and re-checked every action with an independent checker. Preflight made zero people-safety violations. A naive agent made them in twenty-three point two percent of requests.

**14_close** (image): Preflight. Check before you act.
