from fastapi.testclient import TestClient

from services.skills_api import app


def test_skills_api_serves_the_synthetic_skills_as_a_flat_array():
    client = TestClient(app)
    rows = client.get("/staff_skills").json()
    assert {"staff_id": "S001", "skill": "cardiac_telemetry"} in rows
    assert not any(r["staff_id"] == "S002" and r["skill"] == "cardiac_telemetry" for r in rows)
    assert client.get("/health").json() == {"ok": True, "rows": len(rows)}


def test_skills_api_is_read_only():
    client = TestClient(app)
    assert client.post("/staff_skills", json={}).status_code == 405
