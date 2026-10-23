from datetime import date
from pathlib import Path

import pytest

from app.adapters.zetaris import DuckDBSampleSource
from app.meterless.hmem import Entity, Memory, MemoryRetrievalService, MemoryStoreService, RetrievalQuery, TrustLedger, format_reinjection
from app.meterless.world_model import ContextBuilderService, IngestPipelineService, WorldStoreService, stable_entity_id
from app.models import Candidate, ShiftRequest
from app.preconditions.gate import build_params

DATA = Path(__file__).resolve().parent.parent / "data" / "sample"


@pytest.fixture
def store(tmp_path):
    s = MemoryStoreService(tmp_path / "mem.json", TrustLedger(tmp_path / "ledger.jsonl"))
    s.hydrate()
    return s


def mem(content, **kw):
    kw.setdefault("provenance", {"origin": "user_explicit", "learnedAt": 0, "label": "test"})
    return Memory.new(content, type="factual", layer="working", source="test", confidence=0.8, **kw)


def test_hmem_rejects_writes_without_source_or_provenance(store):
    with pytest.raises(ValueError):
        store.add(Memory.new("x", type="factual", layer="working", source="", confidence=1, provenance={}), actor="t")
    with pytest.raises(ValueError):
        store.add(Memory.new("x", type="factual", layer="working", source="s", confidence=1), actor="t")


def test_hmem_persists_and_feedback_moves_confidence(store, tmp_path):
    m = store.add(mem("Ward 4B night shift needs cardiac telemetry"), actor="t")
    store.apply_feedback(m.id, "wrong", actor="t", trace_id="tr")
    reloaded = MemoryStoreService(tmp_path / "mem.json", store.ledger)
    reloaded.hydrate()
    assert reloaded.get(m.id).confidence == pytest.approx(0.6)
    assert "review" in reloaded.get(m.id).tags
    assert [e["action"] for e in store.ledger.history(m.id)] == ["add", "feedback", "update"]


def test_hmem_ranker_prefers_relevant_memory_and_formats_reinjection(store):
    store.add(mem("Ward 4B night cover offer to S001", domain="rostering/4B", tags=["ward:4B"], entities=[Entity("Ward 4B", "ward")]), actor="t")
    store.add(mem("Cafeteria closes at 8pm on weekends", domain="facilities"), actor="t")
    result = MemoryRetrievalService(store).query(
        RetrievalQuery("Ward 4B night cover", tags=["ward:4B"], domain="rostering/4B", entities=[Entity("Ward 4B", "ward")], threshold=0.25)
    )
    assert [m.content for m in result.memories] == ["Ward 4B night cover offer to S001"]
    assert set(result.trace["returned"][0]["signals"]) == {"semantic", "keyword", "tags", "domain", "entities", "layer", "recency", "superseded"}
    assert format_reinjection(result.memories).startswith("[rostering/4B] (working) Ward 4B night cover")


def test_stable_ids_are_deterministic():
    assert stable_entity_id("  S001 ", "staff") == stable_entity_id("s001", "staff")
    assert stable_entity_id("S001", "staff") != stable_entity_id("S001", "skill")


def test_world_model_ingest_has_provenance_and_projects_staff_pool():
    request = ShiftRequest("4B", date(2026, 10, 27), "night")
    world = WorldStoreService()
    report = IngestPipelineService(DuckDBSampleSource(DATA / "scenario_b"), world).ingest_scope(request, build_params(request, Candidate("scope")))
    assert report.complete and report.queries_run == 5
    reader = ContextBuilderService(world)
    pool = {p["staff_id"]: p for p in reader.staff_pool(request)}
    assert pool["S001"]["leave_on_date"] == ["annual approved 2026-10-26 to 2026-10-30"]
    assert pool["S005"]["already_on_this_shift"] is True
    assert pool["S002"]["has_required_skill"] is False
    ctx = reader.build_prompt_context(request)
    assert "Priya" not in ctx["text"]  # names stay local
    assert ctx["provenance"] and all(p["query_id"] for p in ctx["provenance"])
    obs = world.observation(pool["S001"]["observations"][0])
    assert obs.sql.startswith("SELECT") and obs.source == "duckdb-sample:scenario_b"


def test_world_model_demand_view_carries_ward_policy():
    request = ShiftRequest("4B", date(2026, 10, 27), "night")
    world = WorldStoreService()
    IngestPipelineService(DuckDBSampleSource(DATA / "scenario_a"), world).ingest_scope(request, build_params(request, Candidate("scope")))
    reader = ContextBuilderService(world)
    assert reader.demand(request)["agency_allowed"] in (True, "true")
    assert "agency_allowed=None" not in reader.build_prompt_context(request)["text"]
