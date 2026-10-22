# Preflight

**A shift-cover agent that checks before it acts.**

When a nurse calls in sick, a unit manager has to cross-check rosters, leave records, staff skills and
patient demand, which all live in different systems. It's easy to book someone who is on leave, not
qualified or over their hours. Preflight takes a plain-English request such as *"Ward 4B needs night
cover tomorrow"* and:

1. **Plans** candidate actions from a fixed action library (`offer_shift_to_staff`,
   `escalate_to_agency`, `no_cover_needed`), ranked using facts and memory.
2. **Checks** each candidate's preconditions as SQL against live data through **Zetaris**: not on
   leave, has the required skill, under the weekly hour cap, rest gap respected, ward actually
   understaffed, and agency allowed.
3. **Acts only if every check passes.** If any check fails it does not act. It records which check
   failed, with the evidence rows, then tries the next candidate. If nothing passes, it writes an
   investigation for the manager and books nobody.
4. **Logs** every step with provenance as JSONL in `logs/`.
5. **Learns**: each successful resolution is saved to **H-MEM** as a reusable action template that
   still has to pass its checks. A recalled template only changes the order candidates are tried in.
   It never skips a check.

GenAI.works Open Agent Hackathon 2026, Track 3: Reasoning Architecture.

## The demo in one picture

Same request, two data states. The only difference is one row in the leave system:

| | Scenario A | Scenario B (Priya has approved annual leave, recorded only in `leave.csv`) |
|---|---|---|
| Naive agent (top-ranked pick, no checks) | Books Priya ✅ | **Books Priya, who is on leave** ❌ |
| Preflight | Offers to Priya: 5/5 checks pass | Refuses Priya (`staff_not_on_leave`, with the leave row), Sam (`under_weekly_hour_cap`), Tom (`has_required_skill`), Raj (`no_conflicting_shift`), then **escalates to agency** |

In scenario B, H-MEM even *recommends* Priya, because she covered this shift pattern in scenario A.
The gate refuses her anyway and the memory's confidence is lowered in the trust ledger. Memory
suggests; data decides.

## Quick start (sample mode, no credentials)

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
git clone https://github.com/Shyboy0499/preflight.git && cd preflight
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
# or: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
source .venv/bin/activate

SAMPLE_MODE=true python -m app "Ward 4B needs night cover tomorrow"                          # scenario A
SAMPLE_MODE=true python -m app "Ward 4B needs night cover tomorrow" --scenario scenario_b     # scenario B
SAMPLE_MODE=true streamlit run ui/streamlit_app.py                                           # UI
pytest                                                                                       # 63 tests
```

`SAMPLE_MODE=true` (the default) swaps Zetaris for DuckDB over `data/sample/<scenario>/*.csv` and the
LLM for a deterministic rule-based generator. "Today" is fixed at Mon 2026-10-26 to match the
synthetic data, so "tomorrow" is 2026-10-27.

CLI options: `--scenario scenario_a|scenario_b`, `--json` (full run as JSON), `--reset-memory`
(forget learned H-MEM memories).

## Sample inputs and outputs

Run in this order from a fresh clone (memory carries over between runs). The outputs are trimmed.

**1. `"Ward 4B needs night cover tomorrow"` on scenario A: the action fires**
```
Parsed     Ward 4B night shift 2026-10-27  (via generator)
World      54 observations from 5 Zetaris queries
[FAIL] no_cover_needed()
  XX  ward_adequately_staffed: 3 of 4 required nurses available (after approved leave)
[PASS] offer_shift_to_staff(staff_id=S001)  <planner> free that day, has required skill, home ward, 8h this week
  ok  ward_actually_understaffed: 3 of 4 required nurses available (after approved leave)
  ok  staff_not_on_leave: no approved leave on the shift date
  ok  has_required_skill: holds cardiac_telemetry
  ok  under_weekly_hour_cap: 8h rostered + 10h shift vs 38h cap
  ok  no_conflicting_shift: no overlapping shift and rest gap respected
FIRED offer_shift_to_staff(staff_id=S001): Shift offer sent to Priya Raman (S001): Ward 4B night shift 2026-10-27.
```

**2. The same request on scenario B: Priya is refused with evidence, then agency**
```
Recalled   1 H-MEM memories
           0.40  Ward 4B night cover: offer_shift_to_staff(staff_id=S001) passed all checks (...)
[FAIL] offer_shift_to_staff(staff_id=S001)  <memory> covered this shift pattern before (memory), ...
  XX  staff_not_on_leave: on approved annual leave 2026-10-26 to 2026-10-30
         evidence {'staff_id': 'S001', 'leave_type': 'annual', 'start_date': '2026-10-26', 'end_date': '2026-10-30', 'status': 'approved'}
[FAIL] offer_shift_to_staff(staff_id=S003)   XX under_weekly_hour_cap: 38h rostered + 10h shift vs 38h cap
[FAIL] offer_shift_to_staff(staff_id=S002)   XX has_required_skill: missing required skill cardiac_telemetry
[FAIL] offer_shift_to_staff(staff_id=S008)   XX no_conflicting_shift: rostered 3A day 2026-10-27 07:00:00 to 15:30:00 (< 10h rest)
[PASS] escalate_to_agency()
FIRED escalate_to_agency(): Agency request raised for 1 nurse: Ward 4B night shift 2026-10-27.
  a naive agent would have fired offer_shift_to_staff(staff_id=S001) [Priya Raman] despite:
  staff_not_on_leave: on approved annual leave 2026-10-26 to 2026-10-30
```

**3. `"Ward 4B day shift tomorrow - do we need anyone?"`: nobody is contacted**
```
[PASS] no_cover_needed()
  ok  ward_adequately_staffed: 5 of 5 required nurses available (after approved leave)
FIRED no_cover_needed(): No cover needed: Ward 4B day shift 2026-10-27 is fully staffed. Nobody contacted.
```

**4. `"Ward 9Z needs cover tonight"`: no safe action, so it investigates instead**
```
[FAIL] escalate_to_agency()
  XX  ward_actually_understaffed: no demand record for 9Z night 2026-10-26
  XX  agency_allowed_for_ward: ward 9Z not found
NO ACTION: no candidate passed every check. Escalated to the manager with evidence.
Investigation (for the manager):
No action passed its checks for Ward 9Z night shift 2026-10-26. Do not book anyone automatically. ...
```

**5. `"Ward 4B needs evening cover tomorrow"`: 0 of 4 rostered, so the whole gap gets covered**
```
FIRED offer_shift_to_staff(staff_id=S001): Shift offer sent to Priya Raman (S001): Ward 4B evening shift 2026-10-27.
FIRED escalate_to_agency(): Agency request raised for 3 nurses: Ward 4B evening shift 2026-10-27.
  refused offer_shift_to_staff(staff_id=S003) [Sam Okafor]: under_weekly_hour_cap (38h rostered + 8h shift vs 38h cap)
  ...
```
Each offer that fires counts toward the next `ward_actually_understaffed` check
("0 + 1 offered in this run of 4"), so Preflight keeps offering until the gap is closed. The agency
is asked only for the nurses still missing. If nothing else passes, for example when ward policy
forbids agency staff, the run ends `partially_covered` and reports how many nurses are still missing.

A request without a ward, date or shift (for example `"someone called in sick"`) is rejected as
`unparsed` and nothing is done.

## How it works

```
request ─► parse (generator, validated; rule fallback)
        ─► World Model ingest  ◄── 5 read-only SQL queries via Zetaris adapter (provenance per row)
        ─► H-MEM recall        (action library + learned templates, 8-signal ranker)
        ─► plan                (generator ranks staff; validated against library + staff pool)
        ─► for each candidate: precondition gate ── SQL checks via Zetaris ── all pass? ─► fire, learn, stop
                                                                            └─ any fail ─► log evidence, next
        ─► nothing passed: generator writes an investigation for the manager; no action
```

| Path | What |
|---|---|
| `app/agent.py` | The loop above |
| `app/actions/library.py` | Action specs and their fixed check lists |
| `app/actions/executor.py` | Fires an action. Delivery is simulated: it writes to `logs/outbox.jsonl` |
| `app/preconditions/checks.py` | 7 SQL checks; each returns evidence rows and a pass rule |
| `app/preconditions/gate.py` | Runs every check of an action and fails closed on any data error |
| `app/planner.py`, `app/request_parser.py` | Generator prompts, plus validation of their output |
| `app/adapters/zetaris.py` | Zetaris REST source, DuckDB sample source, read-only SQL guard |
| `app/adapters/llm.py` | The single `generator(prompt, system_prompt)`, with fallback |
| `app/meterless/hmem.py`, `app/meterless/world_model.py` | Meterless engines (below) |
| `app/audit.py` | JSONL logs |
| `ui/streamlit_app.py` | Naive agent vs Preflight, with checks, evidence, memory and facts |

### Safety design (where the reasoning becomes reliable)

- **The gate is code, not the LLM.** The generator proposes and ranks candidates. Its output is
  checked against the library and the staff pool, and anything invented (unknown staff, unknown
  actions) is dropped. Only SQL evidence can let an action through.
- **Fail closed.** If Zetaris times out or errors, the check counts as failed and the run ends
  `unverifiable`. Live mode never falls back to the sample CSVs.
- **Checks always query the source directly.** They never read the World Model's cached copy, which
  could be out of date.
- **Memory can't skip checks.** Recalled staff are always tried first, even if the LLM left them out, so a stale memory is caught by the gate rather than quietly trusted. A recalled template that fails gets `not_helpful` feedback in the
  H-MEM trust ledger.
- **Read-only data access.** One statement per call, no semicolons, `SELECT`/`WITH`/`SHOW`/`DESCRIBE`
  only, and parameters rendered as typed literals from validated values. This follows the Zetaris
  `AGENTS.md` rules.
- **Privacy.** Staff names never go to the LLM; prompts use IDs only. `LLM_BASE_URL` can point at a
  local server that uses the same API, so prompts stay on the machine.

## Sponsor technology, and where it is used

### Zetaris (all facts and every check)
`app/adapters/zetaris.py` → `ZetarisRestSource` calls the Zetaris direct REST API:
`POST {ZETARIS_REST_URL}/sql-editor/sqls/run` with a bearer API key, `X-Org-ID` and a UUID
`X-Request-ID`, as documented in [zetaris/getting-started](https://github.com/zetaris/getting-started).
It is used by:
- `app/preconditions/gate.py`: every precondition check (7 SQL queries in `checks.py`)
- `app/meterless/world_model.py`: `IngestPipelineService` (5 scope queries)

In live mode the six tables sit in **three different systems**, and Zetaris federates them:

| Zetaris source | Kind | Tables | Stands in for |
|---|---|---|---|
| `PREFLIGHT_FILES` | Lightning file source (CSV) | `staff`, `roster`, `demand`, `wards` | rostering and patient-demand exports |
| `preflight_pg` | JDBC datasource (Postgres) | `leave` | the leave system |
| `PREFLIGHT_API` | Lightning REST source (`services/skills_api.py`) | `staff_skills` | the HR skills system |

Two checks join across sources in a **single SQL statement**: `ward_actually_understaffed`
(file + Postgres) and `has_required_skill` (file + REST). `tests/test_gate.py` pins this.
`ZETARIS_TABLES` maps each table to its qualified name.

Every query result carries provenance (source, query name, query ID, rendered SQL, fetch time) into
the World Model observations and into each check's evidence. Column names are checked against the
response `headers`. In sample mode `DuckDBSampleSource` runs **the same rendered SQL text** over the
CSVs.

### Meterless engines (2 used)
| Engine | Services implemented | Used where |
|---|---|---|
| **H-MEM** (`app/meterless/hmem.py`) | `Memory` record, `MemoryStoreService`, `MemoryRetrievalService` (8-signal hybrid ranker, top-N/threshold, trace), `TrustLedger`, `format_reinjection` | `agent.py`: seeds the action library, recalls before planning, injects into the planner prompt, saves fired actions as templates, adjusts confidence by feedback. `planner.py`: recalled staff are tried first |
| **World Model** (`app/meterless/world_model.py`) | `WorldStoreService` (entities, edges, append-only observations), `IngestPipelineService` (extract → normalize → resolve → validate → append → project, stable hash IDs), `ContextBuilderService` (read surface, `build_prompt_context` → `{text, provenance}`) | `agent.py`: ingests the request's scope from Zetaris. The planner and investigation prompts read its context. The staff pool projection feeds candidate ranking |

These follow `engines/hmem/HOW_TO_USE_H-MEM.md` and `engines/world-model/HOW_TO_USE_WORLD_MODEL.md`
from [Meterless/Meterless](https://github.com/Meterless/Meterless), implemented in Python for this
project. The World Model docs name the contracts `IngestPipeline` and `ReadSurface`. Here they are
`IngestPipelineService` and `ContextBuilderService`. Not implemented: H-MEM mining, dreaming, sleep
and conflict detection, and World Model enrichment and control plane.

### NVIDIA Nemotron (the generator)
The **generator pattern** is in `app/adapters/llm.py`. One `generator(prompt, system_prompt)` is the
only place a model is called. It goes to NVIDIA's OpenAI-compatible endpoint
(`https://integrate.api.nvidia.com/v1`), with the model from `NVIDIA_MODEL`. Nemotron does three
jobs, and none of them can let an action through on its own:

| Job | Where | What Nemotron does |
|---|---|---|
| Parse the request | `app/request_parser.py` | Free text → `{ward, shift_date, shift_type}` JSON, then validated |
| Plan candidates | `app/planner.py` | Ranks staff using World Model facts and H-MEM memories; unknown staff or actions are dropped |
| Investigate | `app/agent.py` | When nothing passes, explains the refusals for the manager |

Model choice, from a test on 2026-10-22 running both scenarios through these tasks:
`nvidia/nemotron-3.5-lightning-30b-a3b` returned valid JSON on every call, about 5 s per full run,
and gave the expected A/B decisions. `nemotron-3-ultra-550b-a55b` was equally correct but slower.
The super and nano models were not enabled for our account. Reasoning is switched off by default
(`chat_template_kwargs.enable_thinking=false`, `LLM_REASONING` to change it). With reasoning on,
these models used the whole token budget on hidden reasoning and returned empty answers. Empty or
failed replies fall back to the rule-based generator and are logged as `llm_fallback`.

## Live mode (real Zetaris and LLM)

Needs Docker and a local Zetaris instance (see
[zetaris/getting-started](https://github.com/zetaris/getting-started)).

1. `cp .env.example .env.local` (gitignored) and fill in `ZETARIS_ORG_ID` (numeric, Management →
   Organisation), `ZETARIS_API_KEY` (Management → API Tokens), `NVIDIA_API_KEY`, and
   `PREFLIGHT_PG_PASSWORD` (any local password for the demo Postgres).
2. Start the skills service: `uvicorn services.skills_api:app --host 127.0.0.1 --port 8077`
3. `python scripts/setup_zetaris.py` shows a dry run. `python scripts/setup_zetaris.py --apply`
   creates the Postgres container, copies the CSVs, registers the three sources, verifies them and
   prints the `ZETARIS_TABLES` line. It is safe to re-run.
4. In `.env.local` set `SAMPLE_MODE=false`, `DEMO_TODAY=2026-10-26` and the printed `ZETARIS_TABLES`.
   **`DEMO_TODAY` is required in live mode**: the demo data is anchored to 26 Oct 2026, so the real
   date would find no demand. Sample mode never reads the system clock.
5. Run `python -m app "Ward 4B needs night cover tomorrow"` (scenario A).
6. Flip to scenario B live: `python scripts/demo_toggle_leave.py on` adds Priya's approved leave in
   Postgres and reads it back through Zetaris. Run the same request again: Preflight now refuses
   Priya with the leave row as evidence. `demo_toggle_leave.py off` switches back.

If the LLM is unreachable, each call falls back to the rule-based generator and an `llm_fallback`
event is logged. If Zetaris is unreachable, nothing fires.

## Logs

| File | Content |
|---|---|
| `logs/trace.jsonl` | Every step of every run: request, parse, World Model ingest and provenance, memory recall trace, plan, each gate result with evidence, decision |
| `logs/decisions.jsonl` | One line per run: the decision, what was refused and why (with evidence), and the naive agent's choice |
| `logs/outbox.jsonl` | Actions that fired (simulated delivery) |
| `logs/hmem_ledger.jsonl` | H-MEM trust ledger: every memory add, update and feedback |

Learned memories persist in `state/hmem_memories.json` (gitignored).

## Dependencies

Python 3.12, `duckdb` (sample data source), `httpx` (Zetaris REST), `openai` (OpenAI-compatible
client for NVIDIA), `python-dotenv`, `streamlit`, `fastapi` + `uvicorn` (skills service), `psycopg`
(demo Postgres scripts), `pytest`. See `requirements.txt` / `pyproject.toml`. Live mode also needs
Docker.

## Data

All data in `data/sample/` is synthetic. See `data/sample/README.md`.
