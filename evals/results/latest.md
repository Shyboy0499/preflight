# Safety evaluation

200 random worlds x 6 requests (seed 7, rule-based generator), 1200 runs in 101.5 s.
Every action is re-checked by `evals/oracle.py`, an independent plain-Python implementation of the rules.

| | Preflight | Naive agent (top pick, no checks) |
|---|---|---|
| Actions taken | 2434 | 1200 |
| **People-safety violations** (on leave, missing skill, over hours, rest gap) | **0** | **279** (23.2%) |
| Unnecessary bookings (ward already covered, no people-safety issue) | 0 | 89 (7.4%) |
| Any rule broken | 0 | 368 (30.7%) |
| Runs with any rule broken | 0 of 1200 | |

Naive violations by rule: rest_conflict 122, over_hour_cap 122, ward_not_short 120, on_leave 60, missing_skill 36

Preflight outcomes: already_covered 46, fired 1029, no_safe_action 24, partially_covered 101
Misparsed requests: 0. Agency used while an eligible internal nurse existed outside the shortlist (over-caution, not unsafe): 0.
Repeat requests for a shift with open actions: 119 open actions re-checked and kept, 0 withdrawn (unjustified withdrawals: 0).
