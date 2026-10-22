"""Staff skills service: the "HR skills system" Zetaris reads as a REST source.

Run:  uvicorn services.skills_api:app --host 127.0.0.1 --port 8077
Zetaris (in Docker) reaches it at http://host.docker.internal:8077/staff_skills.

Read-only, synthetic data (data/sample/scenario_a/staff_skills.csv, identical
in both scenarios). Returns a top-level JSON array so Zetaris can infer the schema.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

from fastapi import FastAPI

SKILLS_CSV = Path(
    os.environ.get("SKILLS_CSV")
    or Path(__file__).resolve().parent.parent / "data" / "sample" / "scenario_a" / "staff_skills.csv"
)

app = FastAPI(title="Preflight staff skills (synthetic)", docs_url=None, redoc_url=None)


def load_skills() -> list[dict[str, str]]:
    with SKILLS_CSV.open(newline="", encoding="utf-8") as f:
        return [{"staff_id": r["staff_id"], "skill": r["skill"]} for r in csv.DictReader(f)]


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "rows": len(load_skills())}


@app.get("/staff_skills")
def staff_skills() -> list[dict[str, str]]:
    return load_skills()
