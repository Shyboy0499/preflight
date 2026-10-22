"""Flip the live demo between scenario A and B by toggling one leave row in Postgres.

    python scripts/demo_toggle_leave.py status
    python scripts/demo_toggle_leave.py on     # Priya on approved annual leave -> scenario B
    python scripts/demo_toggle_leave.py off    # remove it                      -> scenario A

Writes go straight to the Postgres "leave system", as an HR clerk would. The
agent itself stays read-only. After each change it reads Priya's leave back
through Zetaris to show the agent will see the new state.
"""

from __future__ import annotations

import argparse

from _infra import DEMO_LEAVE, LEAVE_COLUMNS, PG_DATASOURCE, pg_connect

KEY = ("staff_id", "leave_type", "start_date")


def current(conn) -> list[tuple]:
    return conn.execute(
        "SELECT staff_id, leave_type, start_date, end_date, status FROM leave WHERE staff_id = %s ORDER BY start_date",
        [DEMO_LEAVE["staff_id"]],
    ).fetchall()


def zetaris_view() -> str:
    from app.adapters.zetaris import DataSourceUnavailable, ZetarisRestSource
    from app.config import SAMPLE_TODAY, load_config
    from app.preconditions.checks import STAFF_NOT_ON_LEAVE
    from app.models import Candidate, ShiftRequest
    from app.preconditions.gate import build_params
    from datetime import date

    try:
        src = ZetarisRestSource(load_config(sample_mode=False, today=SAMPLE_TODAY))
        params = build_params(ShiftRequest("4B", date(2026, 10, 27), "night"), Candidate("offer_shift_to_staff", {"staff_id": DEMO_LEAVE["staff_id"]}))
        rows = src.run(STAFF_NOT_ON_LEAVE.query, params).rows
    except DataSourceUnavailable as exc:
        return f"could not read through Zetaris: {exc}"
    return f"staff_not_on_leave would {'FAIL' if rows else 'PASS'} ({len(rows)} leave row(s) via {src.tables['leave']})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["status", "on", "off"])
    args = parser.parse_args()

    with pg_connect() as conn:
        if args.action == "on":
            cols = ", ".join(LEAVE_COLUMNS)
            conn.execute(f"INSERT INTO leave ({cols}) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                         [DEMO_LEAVE[c] for c in LEAVE_COLUMNS])
        elif args.action == "off":
            conn.execute("DELETE FROM leave WHERE staff_id = %s AND leave_type = %s AND start_date = %s",
                         [DEMO_LEAVE[c] for c in KEY])
        rows = current(conn)

    scenario = "B (Priya on leave)" if rows else "A (Priya available)"
    print(f"Postgres leave rows for {DEMO_LEAVE['staff_id']}: {rows or 'none'}  -> scenario {scenario}")
    print(f"Zetaris ({PG_DATASOURCE}): {zetaris_view()}")


if __name__ == "__main__":
    main()
