# Sample data (synthetic)

Every name, ID and number here is made up for the demo. No real staff or patient data.

Both scenarios describe Ward 4B on Tue 2026-10-27 (with `DEMO_TODAY=2026-10-26`, "tomorrow").
Jess Liu (S004) has called in sick for the night shift, so 4B nights drop to 3 of 4 required nurses.

| Scenario | Only difference | Expected Preflight result for "Ward 4B needs night cover tomorrow" |
|---|---|---|
| `scenario_a` | — | Offer the shift to Priya Raman (S001): every check passes |
| `scenario_b` | `leave.csv`: Priya has approved annual leave 26–30 Oct | Refuse Priya (on leave), Sam (hour cap), Tom (skill), Raj (rest gap after his 3A day shift); escalate to agency |

The leave record lives only in the leave system (`leave.csv`), not in the roster, which is the trap a
roster-only agent falls into.

Tables: `staff`, `staff_skills`, `roster`, `leave`, `demand`, `wards`. In live mode the same tables are
expected in Zetaris under `ZETARIS_TABLE_PREFIX` (e.g. `preflight.staff`).
