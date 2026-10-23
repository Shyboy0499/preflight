# Preflight: status handoff

Updated 2026-10-23 by the Claude Code session building the repo. Keep this file short; it is
rewritten and pushed after each milestone. No secrets here: `.env.local` holds them and is gitignored.

## 1. Done (feature → commit)

| Feature | Commit |
|---|---|
| Precondition gate: 7 SQL checks with evidence rows, fail closed; DuckDB sample source + Zetaris REST adapter (read-only guard, typed literals) | `512699d` |
| Meterless H-MEM (store, 8-signal retrieval, trust ledger) and World Model (store, 6-stage ingest, context builder with provenance) | `465d948` |
| Agent loop, single injected `generator()`, planner validated against library and pool, CLI, JSONL audit | `279ccdc` |
| Streamlit UI: naive agent vs Preflight, checks with evidence | `f570e52` |
| Zetaris response `headers` used and verified against declared columns | `0ca5550` |
| Live mode requires `DEMO_TODAY`; sample mode proven clock-independent | `0e44d42` |
| Per-table qualified names (`ZETARIS_TABLES`): checks join across sources in one SQL statement | `459bcfa` |
| Nemotron reasoning switched off for structured tasks (was returning empty answers) | `537fbf1` |
| Live infra: skills FastAPI service, idempotent `setup_zetaris.py`, `demo_toggle_leave.py` | `8401ea2`, verified `cc2eca4` |
| Memory-recalled staff are always gated, even when the LLM skips them | `10c0064` |
| Cover the whole gap: offers count as pending cover, agency asked for exactly the rest | `52bf0d8` |
| Randomised safety evaluation with an independent oracle; backfill from the staff pool before agency | `fe9d293` |
| Compact CLI, early stop when the ward is not short, README samples from real runs | `181c616` |
| Re-check open actions before acting: keep (counts as cover) or withdraw with evidence; no duplicate sends | `ca6b4cb` |
| Live end-to-end on three sources; concurrent checks per candidate; timestamp normalisation | `cc2eca4` |

## 2. Current state

- **Tests:** `pytest` → `71 passed, 1 warning in 4.18s` (2026-10-23, local). The same 71 pass in a fresh
  clone with a clean environment and no `.env.local`.
- **Sample mode** (`SAMPLE_MODE=true`, DuckDB + rule-based generator): runs end to end and needs no credentials.
- **Live mode:** run against a local Zetaris 2.4.3 (Docker) and NVIDIA `nemotron-3.5-lightning-30b-a3b`.
  Zetaris federates three sources:
  - `PREFLIGHT_FILES` (CSV file source): staff, roster, demand, wards
  - `preflight_pg` (Postgres JDBC): leave
  - `PREFLIGHT_API` (REST, `services/skills_api.py`): staff_skills
- **Written but not run live:** the `--llm` mode of the safety eval (the eval used the rule-based
  generator), the `partially_covered` path against real Zetaris (unit-tested only), and withdrawing an
  agency request.

## 3. Live run evidence (2026-10-23, real Zetaris + Nemotron, trimmed)

```
$ python scripts/demo_toggle_leave.py off ; python -m app "Ward 4B needs night cover tomorrow" --reset-memory --reset-actions
Preflight  run run_961c8349f2  mode LIVE  data zetaris  llm openai-compatible:nvidia/nemotron-3.5-lightning-30b-a3b
[FAIL] no_cover_needed()  ward_adequately_staffed: 3 of 4 required nurses available (after approved leave)
[PASS] offer_shift_to_staff(staff_id=S001) [Priya Raman] <planner> Home ward 4B, has cardiac_telemetry skill, ...
  ok  ward_actually_understaffed / staff_not_on_leave / has_required_skill / under_weekly_hour_cap / no_conflicting_shift
FIRED offer_shift_to_staff(staff_id=S001): Shift offer sent to Priya Raman (S001): Ward 4B night shift 2026-10-27.   (~8 s)

$ python scripts/demo_toggle_leave.py on
Postgres leave rows for S001: [('S001', 'annual', 2026-10-26, 2026-10-30, 'approved')]  -> scenario B (Priya on leave)
Zetaris (preflight_pg): staff_not_on_leave would FAIL (1 leave row(s) via preflight_pg.leave)

$ python -m app "Ward 4B needs night cover tomorrow"
[WITHDRAW] offer_shift_to_staff(staff_id=S001) [Priya Raman] sent in run_961c8349f2: staff_not_on_leave: on approved annual leave 2026-10-26 to 2026-10-30
         evidence {'staff_id': 'S001', 'leave_type': 'annual', 'start_date': '2026-10-26', 'end_date': '2026-10-30', 'status': 'approved'}
[FAIL] offer_shift_to_staff(staff_id=S002) [Tom Becker]   has_required_skill: missing required skill cardiac_telemetry
[FAIL] offer_shift_to_staff(staff_id=S008) [Raj Patel]    no_conflicting_shift: rostered 3A day 07:00-15:30 (< 10h rest)
[FAIL] offer_shift_to_staff(staff_id=S003) [Sam Okafor]   under_weekly_hour_cap: 38h rostered + 10h shift vs 38h cap
[FAIL] ... 6 more staff (day shift that day) refused on no_conflicting_shift
[PASS] escalate_to_agency()
WITHDRAWN offer_shift_to_staff(staff_id=S001): ... withdrawn: staff_not_on_leave (on approved annual leave ...)
FIRED escalate_to_agency(): Agency request raised for 1 nurse: Ward 4B night shift 2026-10-27.   (~19 s)
  a naive agent would have fired offer_shift_to_staff(staff_id=S001) [Priya Raman] despite: staff_not_on_leave ...
```
Running the same request again prints `ALREADY COVERED`, and nothing is sent twice. The Streamlit UI was
smoke-tested in live mode with no exceptions. The demo state was reset afterwards: leave off, open
actions archived, memory cleared.

## 4. Key numbers (scoreboard)

`python -m evals.run_safety_eval --worlds 200 --requests 6` (seed 7, about 100 s) runs 200 random synthetic
worlds with 6 requests each. Every action is re-checked by `evals/oracle.py`, plain Python that shares
no code with the SQL checks. Results are in `evals/results/latest.md`.

| | Preflight | Naive (top-ranked pick, no checks) |
|---|---|---|
| Actions taken | 2434 | 1200 |
| Unsafe actions | **0** | **367 (30.6%)** |

- Naive violations by rule: rest gap 122, hour cap 122, booked when not short 120, on leave 60, missing skill 35.
- Preflight outcomes: fired 1029, partially_covered 101, already_covered 46, no_safe_action 24.
- Misparsed requests: 0.
- Agency used while an eligible in-house nurse existed: 0. The first version of the eval found 12%,
  which led to the backfill fix.
- 119 open actions re-checked and kept.

## 5. Decisions and trade-offs a reviewer should know

- **The LLM proposes; code decides.** Nemotron parses, ranks and explains. Only SQL evidence lets an
  action fire, and invented staff or actions are dropped.
- **Fail closed.** A Zetaris error or timeout counts as a failed check. Live mode never falls back to the CSVs.
- **The World Model is context only.** The gate always queries the sources directly, never the cache.
- **Memory never skips checks.** Recalled staff are always tried and gated, and a stale memory gets
  `not_helpful` in the trust ledger.
- **Open actions live in the agent's own outbox** (`logs/outbox.jsonl`), not in Zetaris. The agent's
  Zetaris access stays strictly read-only. Setup uses a separate `execute_admin` path, and a test
  enforces that `app/` never calls it.
- **Delivery is simulated.** Offers and agency requests are written to the outbox; no SMS or roster write-back.
- **Nemotron model:** `lightning` was chosen after testing. `super` and `nano` returned 404 on this account;
  `ultra` was equally correct but slower. Reasoning is off (`LLM_REASONING` to change).
- **Staff names never reach the LLM;** prompts use IDs only.
- **Speed:** each candidate's checks run concurrently. Checking candidates ahead of time was tried and
  reverted, because it was slower against local Zetaris.
- **Backfill:** up to 12 staff are checked before the agency (costs queries; saves agency spend).
- **`DEMO_TODAY` is required in live mode,** because the demo data is anchored to 2026-10-26.
- **Zetaris constraints found live:** local file tables only under `customer-data/shared`;
  descriptions may use only `[A-Za-z0-9_.,-]`.
- **No third-party code copied.** Zetaris and Meterless docs (Apache-2.0) were followed for interfaces;
  see `PRE_EXISTING.md`.

## 6. Known gaps and risks (fresh clone / judges)

- **Live mode needs a local Zetaris hackathon image, Docker Desktop and the running skills service.**
  It relies on `host.docker.internal` (Docker Desktop; Linux needs `extra_hosts`), container
  `zetaris-driver-1` and network `zetaris_default`. Judges will most likely only run sample mode.
- **State carries across runs** (`state/`, `logs/outbox.jsonl`). Repeating a request answers
  `already_covered`. Use `--reset-actions --reset-memory` before a demo.
- **Live LLM ranking varies between runs.** Decisions don't, because the gate decides. README samples
  come from the rule-based generator.
- **Kept agency requests are never trimmed** if fewer nurses are needed later (over-supply is possible).
- **Pending (unapproved) leave doesn't block,** and shift times are fixed in code (`app/models.py`).
- **The eval's worlds and oracle come from the same rules spec,** so a misread rule would be shared. The
  naive baseline counts "booked when not short" (120 of its 367) as unsafe.
- **Unconfirmed:** the hackathon deadline and track name (the Meterless guide says 15–20 Oct, the brief
  says 27 Oct, Track 3), the `metadata.json` schema (guessed), and Cursor usage (not yet described).

## 7. Open questions (second opinion welcome)

1. Should a manager approve each action before it fires (human in the loop), or is simulated
   auto-send with evidence the right demo?
2. Should a re-check trim an over-sized agency request, and should pending leave block or only warn?
3. Is the naive baseline fair? Should "booked when not short" be reported separately?
4. Demo narrative: lead with the live leave toggle (withdrawal with evidence) or the 0 vs 30.6% scoreboard?
5. Is three local sources the right Zetaris story, or should the README also show a sample query
   joining the provided NY_TAXI/TPCDS data?

## 8. Next

- **Demo, deck, submission text and UI polish are handled by the separate reviewing Claude chat,**
  working from this repo on GitHub. To run the live demo it needs the user's machine: Docker with
  Zetaris, `.env.local`, and the skills service (`uvicorn services.skills_api:app --host 127.0.0.1 --port 8077`).
  Demo order: `--reset-memory --reset-actions` → A → `demo_toggle_leave.py on` → B → B again → `streamlit run ui/streamlit_app.py`.
- This session: apply review feedback, fix anything found, and update and push this file after each milestone.
- Optional, if time allows: run the eval with `--llm` on a small N; trim agency requests on re-check;
  add a live quick-start checklist to the README.
