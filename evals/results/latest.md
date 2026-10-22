# Safety evaluation

200 random worlds x 6 requests (seed 7, rule-based generator), 1200 runs in 94.5 s.
Every action is re-checked by `evals/oracle.py`, an independent plain-Python implementation of the rules.

| | Preflight | Naive agent (top pick, no checks) |
|---|---|---|
| Actions taken | 2553 | 1200 |
| Unsafe actions | **0** | **367** (30.6%) |
| Runs with any rule broken | 0 of 1200 | |

Naive violations by rule: rest_conflict 122, over_hour_cap 122, ward_not_short 120, on_leave 60, missing_skill 35

Preflight outcomes: fired 1075, no_safe_action 24, partially_covered 101
Misparsed requests: 0. Agency used while an eligible internal nurse existed outside the shortlist (over-caution, not unsafe): 0.
