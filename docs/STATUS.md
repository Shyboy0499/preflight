# Preflight: status handoff

Updated 2026-10-23 (round 2: roles, Nightshift client, demo video) by the Claude Code session building the repo. Rewritten and
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
| **Round 2:** four named roles (Planner, Verifier, Investigator, Memory) in `app/agents.py`; `role` on every trace event; Verifier refusals go back to the Planner for one re-plan before backfill and agency | `8e87c4d` |
| **Round 2:** Meterless Nightshift as the primary AI provider (runtime contract: bearer token, `meterless-app` header, `meterless/auto`, 180 s, `/v1/models` status); NVIDIA direct only as a fallback; `--provider-status` | `18e265c` |
| **Round 2:** World Model demand view now carries the ward's agency policy (was `agency_allowed=None`; found while recording) | `5702ce3` |
| **Round 2:** AI-generated demo video tooling (`scripts/video/`) and narration (`demo/script.md`) | `e4ade39`, latest |

## 2. Current state

- **Tests:** `pytest` → `97 passed, 1 warning` (2026-10-23, local).
- **Judge quick start** (README, 3 commands): cloned from GitHub into a clean environment, it ran in
  **50 s** and printed the expected A → withdraw → agency output.
- **Sample mode** (DuckDB + rule-based generator) runs end to end and needs no credentials.
- **Live mode** was run against local Zetaris 2.4.3 + NVIDIA `nemotron-3.5-lightning-30b-a3b`, with
  three sources: `PREFLIGHT_FILES` (CSV), `preflight_pg` (Postgres leave), `PREFLIGHT_API` (REST skills).
- **Nightshift is not yet proven live.** It isn't installed on this machine; `--provider-status`
  reports OFFLINE. The code and tests follow the published contract. Waiting on the user to install
  Nightshift, add the NVIDIA key inside it, and put the runtime token in `.env.local`.
- **The 2026-10-23 live run used NVIDIA direct**, before the Nightshift provider existed. The README says so.
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
| **People-safety violations** (leave, skill, hours, rest) | **0** | **279 (23.2%)** |
| Unnecessary bookings only | 0 | 89 (7.4%) |
| Any rule broken | 0 | 368 (30.7%) |

The naive baseline is now exactly "the Planner's round-1 top pick", which moved it from 278 to 279.
31 naive bookings were both unnecessary and unsafe, so they count under people-safety (279 + 89 = 368).

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
- **Roles:** the Planner and Investigator are the LLM; the Verifier is SQL over Zetaris; Memory is
  H-MEM. The re-plan runs once per request, only when round 1 leaves a gap. The rule-based Planner's
  re-plan skips staff whose facts match the refusal reasons (a heuristic; the Verifier still checks).
- **Provider:** Nightshift first, then NVIDIA direct, then rules. The NVIDIA key belongs in
  Nightshift. A token set together with an NVIDIA base URL is a config error.
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

- **Demo video v2 done: `demo/preflight-demo.mp4`** (gitignored; upload it with the submission). 3:10,
  1080p30, 15 scenes in Demo Video Guide order. Narration: `demo/script.md`.
  - v2 changes, from review:
    - recorded at 4K with smooth pan-and-zoom onto the narrated region (body text about 28 px);
    - a fixed 120 px caption strip, so captions never cover footage;
    - a 4.5 s real hold on the "Withdrawn: Priya … approved annual leave" banner and the leave row,
      then the naive column next to it;
    - the toggle card wraps, trace line 7 is shortened, and the impact line was added.
  - Live takes: A (`run_99d013b277`, 7.4 s) and B (`run_d1c05ce47b`, 31.7 s). In B, Memory recalls
    Priya and her offer is withdrawn. The Verifier then refuses Tom (skill), Sam (hour cap) and Raj
    (rest), and the re-plan's Kai, Noor, Ellie and Omar plus backfill Zoe (rest). Agency for 1.
  - The impact line says "under a minute", not "under thirty seconds": live B took 31.7 s.
  - Not shown: the Investigator, a third "already covered" run, and the live eval (the scoreboard
    reads `evals/results/latest.md`).
- **Blocked on the user (optional):** install Nightshift and set `METERLESS_RUNTIME_TOKEN` to prove the
  primary provider live. Until then, live mode uses the documented NVIDIA-direct fallback.
- The demo state is reset (leave off, no open actions, memory cleared).
- Keep this file updated and pushed after each milestone.
