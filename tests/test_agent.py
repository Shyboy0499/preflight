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
    def _make(scenario="scenario_a", logs="logs", **kw):
        cfg = load_config(sample_mode=True, sample_scenario=scenario, logs_dir=tmp_path / logs, state_dir=tmp_path / "state")
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
    assert {k: refusals[k] for k in ("S001", "S003", "S002", "S008")} == {
        "S001": "staff_not_on_leave",
        "S003": "under_weekly_hour_cap",
        "S002": "has_required_skill",
        "S008": "no_conflicting_shift",
    }
    # backfilled staff (on the 4B day shift) were checked too, and refused for the rest gap
    backfill = [a for a in result.attempts if a.candidate.origin == "backfill"]
    assert backfill and all([r.name for r in a.failed] == ["no_conflicting_shift"] for a in backfill)


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

    second = make_agent("scenario_b", logs="logs_b").run(NIGHT)  # same memory, separate outbox
    assert learned in [m["id"] for m in second.memory_trace["returned"]]
    priya = next(a for a in second.attempts if a.candidate.params.get("staff_id") == "S001")
    assert priya.candidate.origin == "memory"
    assert len(priya.results) == 6 and not priya.passed  # all checks still ran (5 + pending-leave advisory)

    ledger = read_jsonl(tmp_path / "logs_b" / "hmem_ledger.jsonl")
    feedback = [e for e in ledger if e["memory_id"] == learned and e["action"] == "feedback"]
    assert feedback and feedback[-1]["signal"] == "not_helpful"


def test_repeat_success_reinforces_existing_memory(make_agent):
    a = make_agent("scenario_a").run(NIGHT).learned_memory_id
    agent = make_agent("scenario_a", logs="logs_2")  # same memory, fresh outbox
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
    tried = [a.candidate for a in result.attempts]
    assert not any(c.params.get("staff_id") == "S999" or c.action == "delete_roster" for c in tried)
    assert {c.origin for c in tried} == {"default", "backfill"}  # nothing from the bogus plan
    assert result.fired["action"] == "escalate_to_agency"


class _Down:
    name = "zetaris"
    tables = {}

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


def test_recalled_staff_are_gated_even_when_the_generator_skips_them(make_agent):
    make_agent("scenario_a").run(NIGHT)  # learns: 4B night -> offer to S001

    def skips_priya(prompt, system_prompt):
        if "plan_candidates" in system_prompt:
            return '{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "S003"}}]}'
        from app.adapters.llm import rule_based_generator
        return rule_based_generator(prompt, system_prompt)

    result = make_agent("scenario_b", logs="logs_b", generator=skips_priya).run(NIGHT)
    first_offer = next(a for a in result.attempts if a.candidate.action == "offer_shift_to_staff")
    assert first_offer.candidate.params == {"staff_id": "S001"} and first_offer.candidate.origin == "memory"
    assert [r.name for r in first_offer.failed] == ["staff_not_on_leave"]
    assert result.naive["would_fire"] == "offer_shift_to_staff(staff_id=S001)" and result.naive["unsafe"]
    assert result.fired["action"] == "escalate_to_agency"


EVENING = "Ward 4B needs evening cover tomorrow"


def test_multi_nurse_shortfall_is_filled_by_offers_then_agency_for_the_rest(make_agent):
    result = make_agent("scenario_a").run(EVENING)
    assert result.decision == "fired"
    assert [(a["action"], a["params"], a["nurses"]) for a in result.actions] == [
        ("offer_shift_to_staff", {"staff_id": "S001"}, 1),
        ("escalate_to_agency", {}, 3),
    ]
    assert result.shortfall_remaining == 0
    agency_gate = next(a for a in result.attempts if a.candidate.action == "escalate_to_agency")
    staffing = next(r for r in agency_gate.results if r.name == "ward_actually_understaffed")
    assert "+ 1 offered in this run" in staffing.explanation


def test_single_gap_stops_after_one_offer(make_agent):
    result = make_agent("scenario_a").run(NIGHT)
    assert [a["action"] for a in result.actions] == ["offer_shift_to_staff"]
    assert result.shortfall_remaining == 0


def test_shortfall_left_uncovered_is_reported_not_hidden(make_agent, tmp_path):
    import shutil

    data = tmp_path / "no_agency"
    shutil.copytree(DATA / "scenario_a", data)
    (data / "wards.csv").write_text("ward,ward_name,agency_allowed\n4B,Cardiology,false\n")
    result = make_agent(source=DuckDBSampleSource(data)).run(EVENING)
    assert result.decision == "partially_covered"
    assert [a["action"] for a in result.actions] == ["offer_shift_to_staff"]
    assert result.shortfall_remaining == 3
    assert "STILL SHORT by 3" in result.summary
    assert result.investigation


def test_stops_trying_once_the_ward_is_known_not_short(make_agent):
    result = make_agent().run("Ward 9Z needs cover tonight")
    assert [a.candidate.action for a in result.attempts] == ["no_cover_needed", "offer_shift_to_staff"]
    assert result.decision == "no_safe_action" and not result.actions



def test_repeating_a_request_does_not_send_a_second_offer(make_agent):
    make_agent("scenario_a").run(NIGHT)
    again = make_agent("scenario_a").run(NIGHT)
    assert again.decision == "already_covered"
    assert again.actions == [] and again.fired is None
    assert [(r["status"], r["record"]["params"]) for r in again.rechecks] == [("kept", {"staff_id": "S001"})]
    assert "ALREADY COVERED" in again.summary


def test_new_leave_withdraws_the_open_offer_with_evidence_and_recovers(make_agent, tmp_path):
    make_agent("scenario_a").run(NIGHT)  # offer sent to Priya
    result = make_agent("scenario_b").run(NIGHT)  # same world, Priya's leave now recorded
    (recheck,) = result.rechecks
    assert recheck["status"] == "withdrawn"
    assert [r.name for r in recheck["gate"].failed] == ["staff_not_on_leave"]
    assert recheck["gate"].failed[0].evidence[0]["leave_type"] == "annual"
    withdrawn, agency = result.actions
    assert withdrawn["action"] == "withdraw" and withdrawn["ref"] == recheck["record"]["action_id"]
    assert agency["action"] == "escalate_to_agency" and agency["nurses"] == 1
    assert all(a.candidate.params.get("staff_id") != "S001" for a in result.attempts)  # not re-offered
    assert result.naive["unsafe"] is True  # a naive agent would still book her
    outbox = read_jsonl(tmp_path / "logs" / "outbox.jsonl")
    assert [r["action"] for r in outbox] == ["offer_shift_to_staff", "withdraw", "escalate_to_agency"]
    # and the next request sees the agency request as cover
    assert make_agent("scenario_b").run(NIGHT).decision == "already_covered"


def test_hmem_memory_changes_what_the_agent_does(make_agent):
    """H-MEM is load-bearing: identical data and LLM output, different behaviour with memory.

    The generator always ranks Tom (S002, lacks the required skill) first. Without memory the
    agent tries Tom, is refused, and only reaches Priya by backfill. With a memory learned from
    an earlier successful run, it goes to Priya first and never contacts Tom.
    """
    def ranks_tom_first(prompt, system_prompt):
        if "plan_candidates" in system_prompt:
            return '{"candidates": [{"action": "offer_shift_to_staff", "params": {"staff_id": "S002"}}]}'
        from app.adapters.llm import rule_based_generator
        return rule_based_generator(prompt, system_prompt)

    without = make_agent("scenario_a", logs="no_mem", generator=ranks_tom_first).run(NIGHT)  # empty memory

    make_agent("scenario_a", logs="learn").run(NIGHT)  # a run that fires: H-MEM learns "4B night -> S001"
    with_memory = make_agent("scenario_a", logs="with_mem", generator=ranks_tom_first).run(NIGHT)

    offers = lambda r: [a.candidate.params["staff_id"] for a in r.attempts if a.candidate.action == "offer_shift_to_staff"]
    assert offers(without)[:2] == ["S002", "S001"]  # tried Tom, refused; Priya via backfill
    assert offers(with_memory) == ["S001"]  # memory: straight to Priya, Tom never contacted
    assert without.fired["params"] == with_memory.fired["params"] == {"staff_id": "S001"}
    assert any(m["content"].startswith("Ward 4B night cover") for m in with_memory.memory_trace["returned"])
    assert without.memory_trace["returned"] == []



def test_pending_leave_warns_but_does_not_block(make_agent, tmp_path):
    import shutil

    data = tmp_path / "pending"
    shutil.copytree(DATA / "scenario_a", data)
    with (data / "leave.csv").open("a") as f:
        f.write("S001,annual,2026-10-27,2026-10-29,pending\n")
    result = make_agent(source=DuckDBSampleSource(data)).run(NIGHT)
    assert result.fired["params"] == {"staff_id": "S001"}  # not blocked
    assert result.fired["warnings"] == [
        "WARNING: has a pending annual leave request 2026-10-27 to 2026-10-29 (not approved, so not blocking)"
    ]
    assert "WARNING: has a pending annual leave request" in result.summary


def test_recheck_trims_an_oversized_agency_request(make_agent, tmp_path):
    import shutil

    data = tmp_path / "trim"
    shutil.copytree(DATA / "scenario_a", data)
    first = make_agent(source=DuckDBSampleSource(data)).run("Ward 4B needs evening cover tomorrow")
    assert [a["nurses"] for a in first.actions] == [1, 3]  # Priya + agency for 3
    with (data / "roster.csv").open("a") as f:  # a nurse gets rostered onto the evening shift directly
        f.write("R2000,S007,4B,2026-10-27,evening,2026-10-27 15:00:00,2026-10-27 23:30:00,8\n")
    again = make_agent(source=DuckDBSampleSource(data)).run("Ward 4B needs evening cover tomorrow")
    (amend,) = [a for a in again.actions if a["action"] == "amend"]
    assert amend["nurses"] == 2 and "reduced from 3 to 2" in amend["message"]
    assert again.decision == "already_covered"
    third = make_agent(source=DuckDBSampleSource(data)).run("Ward 4B needs evening cover tomorrow")
    assert third.decision == "already_covered" and not third.actions  # amendment persisted, nothing re-amended
