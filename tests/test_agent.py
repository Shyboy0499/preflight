import json
from pathlib import Path

import pytest

from app.adapters.zetaris import DataSourceUnavailable, DuckDBSampleSource
from app.agent import PreflightAgent
from app.config import load_config

DATA = Path(__file__).resolve().parent.parent / "data" / "sample"
NIGHT = "Ward 4B needs night cover tomorrow"


@pytest.fixture
def make_agent(tmp_path):
    def _make(scenario="scenario_a", **kw):
        cfg = load_config(sample_mode=True, sample_scenario=scenario, logs_dir=tmp_path / "logs", state_dir=tmp_path / "state")
        return PreflightAgent(cfg, **kw)

    return _make


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_scenario_a_offers_shift_to_priya(make_agent):
    result = make_agent("scenario_a").run(NIGHT)
    assert result.decision == "fired"
    assert result.fired["action"] == "offer_shift_to_staff"
    assert result.fired["params"] == {"staff_id": "S001"}
    assert result.naive["unsafe"] is False


def test_scenario_b_refuses_priya_with_evidence_and_escalates(make_agent):
    result = make_agent("scenario_b").run(NIGHT)
    assert result.decision == "fired"
    assert result.fired["action"] == "escalate_to_agency"
    priya = next(a for a in result.attempts if a.candidate.params.get("staff_id") == "S001")
    assert [r.name for r in priya.failed] == ["staff_not_on_leave"]
    assert priya.failed[0].evidence[0]["leave_type"] == "annual"
    assert result.naive == {
        "would_fire": "offer_shift_to_staff(staff_id=S001)",
        "staff_name": "Priya Raman",
        "unsafe": True,
        "violations": ["staff_not_on_leave: on approved annual leave 2026-10-26 to 2026-10-30"],
    }
    refusals = {a.candidate.params.get("staff_id"): a.failed[0].name for a in result.attempts if not a.passed and a.candidate.params}
    assert refusals == {
        "S001": "staff_not_on_leave",
        "S003": "under_weekly_hour_cap",
        "S002": "has_required_skill",
        "S008": "no_conflicting_shift",
    }


def test_day_shift_needs_no_cover(make_agent):
    result = make_agent().run("Ward 4B day shift tomorrow - do we need anyone?")
    assert result.decision == "fired"
    assert result.fired["action"] == "no_cover_needed"


def test_unknown_ward_takes_no_action_and_investigates(make_agent):
    result = make_agent().run("Ward 9Z needs cover tonight")
    assert result.decision == "no_safe_action"
    assert result.fired is None
    assert "Do not book anyone" in result.investigation


def test_unparseable_request_does_nothing(make_agent):
    result = make_agent().run("someone called in sick")
    assert result.decision == "unparsed"
    assert result.attempts == []


def test_every_run_writes_trace_decision_and_outbox(make_agent, tmp_path):
    result = make_agent("scenario_b").run(NIGHT)
    logs = tmp_path / "logs"
    kinds = [e["kind"] for e in read_jsonl(logs / "trace.jsonl") if e["run_id"] == result.run_id]
    assert kinds[:5] == ["request", "parsed", "world_ingest", "memory_recall", "plan"]
    assert kinds.count("gate") == len(result.attempts)
    assert kinds[-1] == "decision"
    decision = read_jsonl(logs / "decisions.jsonl")[-1]
    assert decision["decision"] == "fired"
    assert decision["refused"][1]["failed"][0]["evidence"][0]["staff_id"] == "S001"
    assert read_jsonl(logs / "outbox.jsonl")[-1]["action"] == "escalate_to_agency"
    ingest = next(e for e in read_jsonl(logs / "trace.jsonl") if e["kind"] == "world_ingest")
    assert ingest["report"]["queries_run"] == 5
    assert all(p["source"].startswith("duckdb-sample") for p in ingest["report"]["provenance"])


def test_memory_is_learned_recalled_and_penalised_but_never_skips_checks(make_agent, tmp_path):
    first = make_agent("scenario_a").run(NIGHT)
    learned = first.learned_memory_id
    assert learned

    second = make_agent("scenario_b").run(NIGHT)  # fresh process, same state dir
    assert learned in [m["id"] for m in second.memory_trace["returned"]]
    priya = next(a for a in second.attempts if a.candidate.params.get("staff_id") == "S001")
    assert priya.candidate.origin == "memory"
    assert len(priya.results) == 5 and not priya.passed  # all checks still ran

    ledger = read_jsonl(tmp_path / "logs" / "hmem_ledger.jsonl")
    feedback = [e for e in ledger if e["memory_id"] == learned and e["action"] == "feedback"]
    assert feedback and feedback[-1]["signal"] == "not_helpful"


def test_repeat_success_reinforces_existing_memory(make_agent):
    agent = make_agent("scenario_a")
    a = agent.run(NIGHT).learned_memory_id
    b = agent.run(NIGHT).learned_memory_id
    assert a == b
    assert agent.memory.get(a).confidence == pytest.approx(0.75)


def test_hallucinated_or_garbage_plans_are_dropped(make_agent):
    def liar(prompt, system_prompt):
        if "plan_candidates" in system_prompt:
            return '{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "S999"}},' \
                   ' {"action": "delete_roster", "params": {}}]}'
        if "parse_request" in system_prompt:
            return "not json at all"
        return "investigation text"

    result = make_agent("scenario_b", generator=liar).run(NIGHT)
    assert result.parse_path == "rules"
    tried = [a.candidate.label() for a in result.attempts]
    assert tried == ["no_cover_needed()", "escalate_to_agency()"]
    assert result.fired["action"] == "escalate_to_agency"


class _Down:
    name = "zetaris"
    table_prefix = "preflight"

    def run(self, query, params=None):
        raise DataSourceUnavailable("zetaris: timed out")


def test_unreachable_data_source_means_no_action(make_agent):
    result = make_agent(source=_Down()).run(NIGHT)
    assert result.decision == "unverifiable"
    assert result.fired is None
    assert result.world["report"]["errors"]


class _AfterTheEvent(__import__("datetime").date):
    @classmethod
    def today(cls):
        return cls(2027, 3, 1)


def test_sample_runs_after_the_event_still_give_the_ab_results(make_agent, monkeypatch):
    import app.config

    monkeypatch.setattr(app.config, "date", _AfterTheEvent)
    monkeypatch.delenv("DEMO_TODAY", raising=False)
    a = make_agent("scenario_a")
    assert a.cfg.today.isoformat() == "2026-10-26"
    assert a.run(NIGHT).fired["params"] == {"staff_id": "S001"}
    b = make_agent("scenario_b").run(NIGHT)
    assert b.fired["action"] == "escalate_to_agency"
    assert b.naive["unsafe"] is True


def test_live_mode_without_demo_today_refuses_to_start(monkeypatch):
    from app.config import ConfigError

    monkeypatch.delenv("DEMO_TODAY", raising=False)
    monkeypatch.setenv("DEMO_TODAY", "")
    with pytest.raises(ConfigError, match="DEMO_TODAY"):
        load_config(sample_mode=False)
