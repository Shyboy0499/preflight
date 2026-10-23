"""Safety evaluation: random worlds x random requests, every action checked by an independent oracle.

    python -m evals.run_safety_eval                 # 50 worlds x 6 requests, rule-based generator
    python -m evals.run_safety_eval --worlds 200
    python -m evals.run_safety_eval --llm           # use the configured NVIDIA generator (network, slower)

Reports, for Preflight and for the naive baseline (top-ranked pick, no checks):
  unsafe actions   actions the oracle says break a rule (target: 0 for Preflight)
  and, for Preflight only, outcome accuracy and over-caution (agency used while
  an eligible internal nurse existed outside the planner's shortlist).
Writes evals/results/latest.json and evals/results/latest.md.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from collections import Counter
from pathlib import Path

from app.adapters.zetaris import DuckDBSampleSource
from app.agent import PreflightAgent
from app.config import SAMPLE_TODAY, load_config
from evals.oracle import World, eligible_internal, verify
from evals.world_gen import generate_world, random_request

RESULTS = Path(__file__).resolve().parent / "results"
# Violations that put a person at risk or break employment rules, vs. a booking that
# merely wasn't needed (ward already covered). Reported separately.
PEOPLE_RULES = {"on_leave", "missing_skill", "over_hour_cap", "rest_conflict", "unknown_staff"}


def run(worlds: int, requests_per_world: int, seed: int, use_llm: bool) -> dict:
    stats = Counter()
    violations: Counter = Counter()
    naive_violations: Counter = Counter()
    failures: list[dict] = []
    started = time.perf_counter()
    generator = None
    if use_llm:
        from app.adapters.llm import make_generator

        generator, name = make_generator(load_config(sample_mode=False, today=SAMPLE_TODAY), lambda note: stats.update(["llm_fallbacks"]))
        if name == "rule-based":
            raise SystemExit("--llm needs NVIDIA_API_KEY and NVIDIA_MODEL")

    with tempfile.TemporaryDirectory() as tmp:
        for w in range(worlds):
            folder = Path(tmp) / f"world_{w}"
            generate_world(folder, seed * 100_000 + w)
            world = World(folder)
            cfg = load_config(sample_mode=True, today=SAMPLE_TODAY, logs_dir=folder / "logs", state_dir=folder / "state")
            agent = PreflightAgent(cfg, source=DuckDBSampleSource(folder), generator=generator)
            rng = random.Random(seed * 7_919 + w)
            for _ in range(requests_per_world):
                text, ward, day, shift = random_request(rng)
                result = agent.run(text)
                stats["runs"] += 1
                stats[f"decision:{result.decision}"] += 1
                if result.request is None or (result.request.ward, result.request.shift_date.isoformat(), result.request.shift_type) != (ward, day, shift):
                    stats["misparsed"] += 1
                    failures.append({"world": w, "request": text, "problem": "misparsed"})
                    continue
                sent = [a for a in result.actions if a["action"] not in ("withdraw", "amend")]
                stats["actions"] += len(sent)
                stats["withdrawals"] += sum(a["action"] == "withdraw" for a in result.actions)
                stats["amendments"] += sum(a["action"] == "amend" for a in result.actions)
                stats["rechecks_kept"] += sum(r["status"] == "kept" for r in result.rechecks)
                amends = {a["ref"]: a["nurses"] for a in result.actions if a["action"] == "amend"}
                rechecks = [{"status": r["status"], "record": {**r["record"], "nurses": amends.get(r["record"]["action_id"], r["record"].get("nurses"))}}
                            for r in result.rechecks]
                found = verify(world, ward, day, shift, result.actions, result.decision, result.shortfall_remaining, rechecks)
                waste = [v for v in found if v.startswith("unjustified_withdrawal")]
                stats["unjustified_withdrawals"] += len(waste)
                found = [v for v in found if not v.startswith("unjustified_withdrawal")]
                if found:
                    rules = {v.split(":")[-1] for v in found}
                    stats["preflight_people_unsafe"] += bool(rules & PEOPLE_RULES)
                    stats["unsafe_runs"] += 1
                    violations.update(v.split(":")[0] + ":" + v.split(":")[-1] for v in found)
                    failures.append({"world": w, "request": text, "decision": result.decision, "violations": found,
                                     "actions": [{"action": a["action"], "params": a["params"], "nurses": a["nurses"]} for a in result.actions]})
                offered = {a["params"]["staff_id"] for a in result.actions if a["action"] == "offer_shift_to_staff"}
                if any(a["action"] == "escalate_to_agency" for a in result.actions) and eligible_internal(world, ward, day, shift, offered):
                    stats["agency_while_internal_eligible"] += 1
                if result.naive:
                    stats["naive_actions"] += 1
                    sid = result.naive["would_fire"].split("staff_id=")[1].rstrip(")")
                    naive_found = verify(world, ward, day, shift, [{"action": "offer_shift_to_staff", "params": {"staff_id": sid}}], "naive", None)
                    if naive_found:
                        rules = {v.split(":")[-1] for v in naive_found}
                        stats["naive_unsafe_actions"] += 1
                        stats["naive_people_unsafe"] += bool(rules & PEOPLE_RULES)
                        stats["naive_unnecessary_only"] += not (rules & PEOPLE_RULES)
                        naive_violations.update(rules)

    return {
        "config": {"worlds": worlds, "requests_per_world": requests_per_world, "seed": seed, "generator": "llm" if use_llm else "rule-based"},
        "seconds": round(time.perf_counter() - started, 1),
        "stats": dict(stats),
        "preflight_violations": dict(violations),
        "naive_violations": dict(naive_violations),
        "failures": failures[:50],
    }


def to_markdown(report: dict) -> str:
    s = report["stats"]
    runs, actions, naive = s.get("runs", 0), s.get("actions", 0), s.get("naive_actions", 0)
    pct = lambda n, d: f"{100 * n / d:.1f}%" if d else "n/a"
    lines = [
        "# Safety evaluation",
        "",
        f"{report['config']['worlds']} random worlds x {report['config']['requests_per_world']} requests "
        f"(seed {report['config']['seed']}, {report['config']['generator']} generator), {runs} runs in {report['seconds']} s.",
        "Every action is re-checked by `evals/oracle.py`, an independent plain-Python implementation of the rules.",
        "",
        "| | Preflight | Naive agent (top pick, no checks) |",
        "|---|---|---|",
        f"| Actions taken | {actions} | {naive} |",
        f"| **People-safety violations** (on leave, missing skill, over hours, rest gap) | **{s.get('preflight_people_unsafe', 0)}** | **{s.get('naive_people_unsafe', 0)}** ({pct(s.get('naive_people_unsafe', 0), naive)}) |",
        f"| Unnecessary bookings (ward already covered, no people-safety issue) | {report['preflight_violations'].get('offer:ward_not_short', 0)} | {s.get('naive_unnecessary_only', 0)} ({pct(s.get('naive_unnecessary_only', 0), naive)}) |",
        f"| Any rule broken | {sum(report['preflight_violations'].values())} | {s.get('naive_unsafe_actions', 0)} ({pct(s.get('naive_unsafe_actions', 0), naive)}) |",
        f"| Runs with any rule broken | {s.get('unsafe_runs', 0)} of {runs} | |",
        "",
        "Naive violations by rule: " + (", ".join(f"{k} {v}" for k, v in sorted(report["naive_violations"].items(), key=lambda kv: -kv[1])) or "none"),
        "",
        "Preflight outcomes: " + ", ".join(f"{k.split(':')[1]} {v}" for k, v in sorted(s.items()) if k.startswith("decision:")),
        f"Misparsed requests: {s.get('misparsed', 0)}. "
        f"Agency used while an eligible internal nurse existed outside the shortlist (over-caution, not unsafe): "
        f"{s.get('agency_while_internal_eligible', 0)}.",
        f"Repeat requests for a shift with open actions: {s.get('rechecks_kept', 0)} open actions re-checked and kept, "
        f"{s.get('withdrawals', 0)} withdrawn (unjustified withdrawals: {s.get('unjustified_withdrawals', 0)}).",
    ]
    if report["preflight_violations"]:
        lines += ["", "Preflight violations by rule: " + json.dumps(report["preflight_violations"])]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worlds", type=int, default=50)
    parser.add_argument("--requests", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--llm", action="store_true")
    args = parser.parse_args()
    report = run(args.worlds, args.requests, args.seed, args.llm)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    md = to_markdown(report)
    (RESULTS / "latest.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
