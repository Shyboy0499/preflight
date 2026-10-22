import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _infra  # noqa: E402
import setup_zetaris  # noqa: E402


def test_toggle_row_is_exactly_priyas_scenario_b_leave():
    assert _infra.DEMO_LEAVE == {"staff_id": "S001", "leave_type": "annual", "start_date": "2026-10-26", "end_date": "2026-10-30", "status": "approved"}
    assert _infra.DEMO_LEAVE not in _infra.BASE_LEAVE


def test_setup_statements_are_single_and_cover_three_source_types():
    stmts = setup_zetaris.zetaris_statements("pw-secret")
    sqls = [sql for _, _, sql in stmts]
    assert all(";" not in s for s in sqls)
    assert sum(s.startswith("CREATE LIGHTNING FILESTORE TABLE") for s in sqls) == 4
    assert sum(s.startswith("CREATE DATASOURCE") for s in sqls) == 1
    assert sum(s.startswith("CREATE LIGHTNING REST TABLE") for s in sqls) == 1


def test_dry_run_output_masks_the_postgres_password():
    shown = " ".join(sql for _, _, sql in setup_zetaris.zetaris_statements(setup_zetaris.MASK))
    assert "pw-secret" not in shown and 'password "********"' in shown
