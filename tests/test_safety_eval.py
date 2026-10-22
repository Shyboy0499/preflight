from evals.run_safety_eval import run


def test_no_unsafe_actions_across_random_worlds():
    report = run(worlds=8, requests_per_world=4, seed=11, use_llm=False)
    s = report["stats"]
    assert s["runs"] == 32 and s.get("misparsed", 0) == 0
    assert report["preflight_violations"] == {}, report["failures"][:3]
    assert s.get("agency_while_internal_eligible", 0) == 0
