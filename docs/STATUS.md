# Preflight: status handoff

Updated 2026-10-23 (after review round 1) by the Claude Code session building the repo. Rewritten and
pushed after each milestone. No secrets here: `.env.local` holds them and is gitignored.
Deadline: **27 Oct 2026 23:45 UTC** (Wed 28 Oct 10:45 Sydney). Track: **Track 03: Reasoning Architecture**.

## 1. Done (feature → commit)

| Feature | Commit |
|---|---|
| Precondition gate (SQL checks, evidence rows, fail closed); DuckDB sample source + Zetaris REST adapter | `512699d` |
| Meterless H-MEM (store, 8-signal retrieval, trust ledger) + World Model (store, ingest, context with provenance) | `465d948` |
| Agent loop, single `generator()`, validated planner, CLI, JSONL audit | `279ccdc` |
| Streamlit UI | `f570e52` |
| Zetaris `headers` verified against declared columns | `0ca5550` |
| `DEMO_TODAY` required live; sample mode clock-independent | `0e44d42` |
| Per-table names: checks join across sources in one SQL statement | `459bcfa` |
| Nemotron reasoning off (it was returning empty answers) | `537fbf1` |
| Live infra: skills service, `setup_zetaris.py`, `demo_toggle_leave.py` | `8401ea2`, `cc2eca4` |
| Memory-recalled staff always gated | `10c0064` |
| Cover the whole gap; agency for exactly the rest | `52bf0d8` |
| Safety eval with an independent oracle; backfill before agency | `fe9d293` |
| Compact CLI, early stop | `181c616` |
| Re-check open actions: keep, or withdraw with evidence; no duplicate sends | `ca6b4cb` |
| Live three-source run verified; concurrent checks per candidate | `cc2eca4` |
| **Review round 1:** H-MEM behaviour test (same data and LLM, different path with memory) | `639b45f` |
| **Review round 1:** pending leave becomes a warning (never blocks); re-check trims an over-sized agency request | `edf00b3` |
| **Review round 1:** UI "Preflight prepares, manager sends" (Send / Discard, simulated); fixed "No action" shown next to "Already covered" | `6e42642` |
| **Review round 1:** judge quick start, Mermaid architecture, `docs/live-run-2026-10-23.md`, `scripts/demo_reset.sh`, split scoreboard, track and Cursor facts | `29414eb` |

## 2. Current state

- **Tests:** `pytest` → `76 passed, 1 warning in 4.78s` (2026-10-23, local).
- **Judge quick start** (README, 3 commands): cloned from GitHub into a clean environment, it ran in
  **50 s** and printed the expected A → withdraw → agency output.
- **Sample mode** (DuckDB + rule-based generator) runs end to end and needs no credentials.
- **Live mode** was run against local Zetaris 2.4.3 + NVIDIA `nemotron-3.5-lightning-30b-a3b`, with
  three sources: `PREFLIGHT_FILES` (CSV), `preflight_pg` (Postgres leave), `PREFLIGHT_API` (REST skills).
- **Not yet run live:**
  - this round's UI Send/Discard (tested in sample mode and pytest only);
  - the pending-leave warning and the agency trim (unit-tested only);
  - the eval with `--llm`.

## 3. Live run evidence

Committed: [`docs/live-run-2026-10-23.md`](live-run-2026-10-23.md). It contains:
- the CLI output of scenario A (~8 s), the leave toggle, and scenario B (~19 s), where the earlier
  offer is withdrawn with the Postgres leave row and the agency is asked for 1 nurse;
- the actual cross-source SQL (`PREFLIGHT_FILES.demand` + `.roster` + `preflight_pg.leave` in one
  statement; file + REST for skills);
- a condensed `trace.jsonl` and `decisions.jsonl` for both runs.

The demo was reset afterwards (`scripts/demo_reset.sh`).

## 4. Key numbers (scoreboard)

`python -m evals.run_safety_eval --worlds 200 --requests 6` (seed 7, ~100 s) → `evals/results/latest.md`.
Every action is re-checked by `evals/oracle.py`, which shares no code with the SQL checks.

| | Preflight | Naive (top pick, no checks) |
|---|---|---|
| Actions | 2434 | 1200 |
| **People-safety violations** (leave, skill, hours, rest) | **0** | **278 (23.2%)** |
| Unnecessary bookings only | 0 | 89 (7.4%) |
| Any rule broken | 0 | 367 (30.6%) |

Correction to the review's 247/120 split: 31 naive bookings were both unnecessary and unsafe, so
they count under people-safety (278 + 89 = 367).

## 5. Decisions and trade-offs

- **The LLM proposes; code decides.** Invented staff or actions are dropped. Zetaris errors count as
  failures, with no fallback to the CSVs in live mode.
- **The World Model is context only;** the gate always queries the sources. **Memory never skips checks.**
- **Open actions live in the agent's outbox,** not Zetaris (agent access to Zetaris is read-only;
  setup uses a separate admin path, test-enforced).
- **UI:** prepared actions reserve cover until sent or discarded, so a repeat request prepares nothing
  new and discarding frees the shift. The CLI and the eval send automatically.
- **Advisory checks:** a failure gives `warn`, which passes and shows the warning. An advisory that
  can't be verified errors, and the action is blocked (fail closed).
- **Model:** `lightning`, with reasoning off. `super`/`nano` returned 404 on this account; `ultra` was slower.
- **Staff names never go to the LLM.** No third-party code was copied (`PRE_EXISTING.md`).
- **Built with Claude Code; Cursor not used** (now optional per the organisers).

## 6. Known gaps and risks

- **Live mode needs** a local Zetaris image, Docker Desktop (`host.docker.internal`), the skills
  service running, and `ZETARIS_TABLES`/`DEMO_TODAY` in `.env.local`. Judges will most likely use
  sample mode plus the committed live evidence.
- **State carries across runs** (`state/`, `logs/outbox.jsonl`). Run `scripts/demo_reset.sh` before any demo.
- **The UI's prepared-but-unsent actions block repeat requests** until sent or discarded. This is
  intended, but could surprise a demo operator.
- **Live LLM ranking varies between runs;** decisions don't.
- **Shift times are fixed in code.** The eval's worlds and oracle come from the same rules spec.
- The 1 pytest warning is a Starlette/httpx deprecation in the FastAPI `TestClient`; it's harmless.

## 7. Open questions

1. UI: should Send also be required for withdrawals and amendments? Currently they are automatic,
   since they cancel or reduce something already sent.
2. Is the 50 s quick start acceptable, or should the README also offer a `uv` variant (~10 s install)?
3. Should the deck lead with the people-safety number (0 vs 23.2%) or "any rule broken" (0 vs 30.6%)?

## 8. Next

- The reviewing Claude chat handles the demo, deck, submission text and UI polish from GitHub. The
  live demo needs the user's machine:
  - Docker with Zetaris, and `.env.local`;
  - the skills service: `uvicorn services.skills_api:app --host 127.0.0.1 --port 8077`;
  - order: `scripts/demo_reset.sh` → A → `demo_toggle_leave.py on` → B → B again → UI.
- This session: apply review feedback and update and push this file after each milestone.
- Optional: run the eval with `--llm` on a small N; run the new UI Send step once against live Zetaris.
