"""Shared settings for the live-demo infrastructure scripts.

Live layout (three systems, joined by Zetaris):
  PREFLIGHT_FILES  Lightning file source over CSVs in the Zetaris customer-data volume:
                   staff, roster, demand, wards
  preflight_pg     JDBC datasource: the "leave system", a Postgres container (leave)
  PREFLIGHT_API    Lightning REST source: the "HR skills system", services/skills_api.py
                   (staff_skills)
"""

from __future__ import annotations

import csv
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env.local", override=False)

SAMPLE_A = ROOT / "data" / "sample" / "scenario_a"
SAMPLE_B = ROOT / "data" / "sample" / "scenario_b"

ZETARIS_NETWORK = os.environ.get("ZETARIS_DOCKER_NETWORK", "zetaris_default")
ZETARIS_DRIVER = os.environ.get("ZETARIS_DRIVER_CONTAINER", "zetaris-driver-1")
CUSTOMER_DATA_DIR = "/home/zetaris/customer-data/preflight"

FILE_DB = "PREFLIGHT_FILES"
API_DB = "PREFLIGHT_API"
PG_DATASOURCE = "preflight_pg"
FILE_TABLES = ("staff", "roster", "demand", "wards")

PG_CONTAINER = "preflight-postgres"
PG_VOLUME = "preflight_pgdata"
PG_IMAGE = "postgres:16-alpine"
PG_DB = PG_USER = "preflight"
PG_HOST_PORT = int(os.environ.get("PREFLIGHT_PG_PORT", "55432"))

SKILLS_PORT = int(os.environ.get("SKILLS_API_PORT", "8077"))
SKILLS_URL_FOR_ZETARIS = os.environ.get(
    "SKILLS_API_URL_FOR_ZETARIS", f"http://host.docker.internal:{SKILLS_PORT}/staff_skills"
)

LEAVE_COLUMNS = ("staff_id", "leave_type", "start_date", "end_date", "status")
LEAVE_DDL = """
CREATE TABLE IF NOT EXISTS leave (
    staff_id   text NOT NULL,
    leave_type text NOT NULL,
    start_date date NOT NULL,
    end_date   date NOT NULL,
    status     text NOT NULL,
    PRIMARY KEY (staff_id, leave_type, start_date)
)
"""


def leave_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


BASE_LEAVE = leave_rows(SAMPLE_A / "leave.csv")
# The single row that turns scenario A into scenario B (Priya's approved annual leave).
DEMO_LEAVE = next(r for r in leave_rows(SAMPLE_B / "leave.csv") if r not in BASE_LEAVE)


def pg_password(required: bool = True) -> str:
    pw = os.environ.get("PREFLIGHT_PG_PASSWORD", "")
    if required and not pw:
        sys.exit("Set PREFLIGHT_PG_PASSWORD in .env.local (any local-only password for the demo Postgres).")
    return pw


def pg_connect():
    import psycopg

    return psycopg.connect(
        host="127.0.0.1", port=PG_HOST_PORT, dbname=PG_DB, user=PG_USER, password=pg_password(), connect_timeout=5
    )


def docker(*args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check, env=env)
