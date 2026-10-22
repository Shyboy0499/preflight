"""CLI: python -m app "Ward 4B needs night cover tomorrow" [--scenario scenario_b] [--json]"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description="Preflight: a shift-cover agent that checks before it acts.")
    parser.add_argument("request", help='e.g. "Ward 4B needs night cover tomorrow"')
    parser.add_argument("--scenario", help="sample data folder under data/sample (SAMPLE_MODE only)")
    parser.add_argument("--json", action="store_true", help="print the full run as JSON")
    parser.add_argument("--reset-memory", action="store_true", help="forget learned H-MEM memories before running")
    args = parser.parse_args(argv)

    if args.scenario:
        os.environ["SAMPLE_SCENARIO"] = args.scenario

    from app.agent import PreflightAgent
    from app.adapters.zetaris import DataSourceUnavailable
    from app.config import load_config

    cfg = load_config()
    if args.reset_memory:
        shutil.rmtree(cfg.state_dir, ignore_errors=True)
    try:
        agent = PreflightAgent(cfg)
    except DataSourceUnavailable as exc:
        print(f"Data source unavailable: {exc}", file=sys.stderr)
        return 2
    result = agent.run(args.request)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
        return 0

    mode = f"SAMPLE ({cfg.sample_scenario})" if cfg.sample_mode else "LIVE"
    print(f"Preflight  run {result.run_id}  mode {mode}  data {result.data_source}  llm {result.generator}")
    print(f"Request    {result.request_text}")
    if result.request:
        print(f"Parsed     {result.request.label()}  (via {result.parse_path})")
    if result.memory_trace:
        recalled = result.memory_trace["returned"]
        print(f"Recalled   {len(recalled)} H-MEM memories" + "".join(f"\n           {m['score']:.2f}  {m['content'][:100]}" for m in recalled))
    if result.world:
        rep = result.world["report"]
        print(f"World      {rep['observations']} observations from {rep['queries_run']} Zetaris queries" + (f"  ERRORS: {rep['errors']}" if rep["errors"] else ""))
    print()
    for a in result.attempts:
        mark = "PASS" if a.passed else "FAIL"
        print(f"[{mark}] {a.candidate.label()}  <{a.candidate.origin}> {a.candidate.reason}")
        for r in a.results:
            sym = {"pass": "  ok ", "fail": "  XX ", "error": "  ?? "}[r.status]
            print(f"{sym} {r.name}: {r.explanation}")
            if not r.passed and r.evidence:
                for row in r.evidence[:3]:
                    print(f"         evidence {row}")
    print()
    print(result.summary)
    if result.investigation:
        print("\nInvestigation (for the manager):\n" + result.investigation)
    print(f"\nTrace: {cfg.logs_dir / 'trace.jsonl'}  (run_id {result.run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
