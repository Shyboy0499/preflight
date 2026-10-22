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
    parser.add_argument("--verbose", action="store_true", help="show every check of every candidate")
    args = parser.parse_args(argv)

    if args.scenario:
        os.environ["SAMPLE_SCENARIO"] = args.scenario

    from app.agent import PreflightAgent
    from app.adapters.zetaris import DataSourceUnavailable
    from app.config import ConfigError, load_config

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
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
    names = (result.world or {}).get("names", {})
    for a in result.attempts:
        sid = a.candidate.params.get("staff_id")
        who = f" [{names.get(sid, sid)}]" if sid else ""
        if not a.passed and not args.verbose:  # compact: one line, the failed checks and first evidence row
            failed = "; ".join(f"{r.name}: {r.explanation}" for r in a.failed)
            print(f"[FAIL] {a.candidate.label()}{who} <{a.candidate.origin}>  {failed}")
            row = next((r.evidence[0] for r in a.failed if r.evidence), None)
            if row and a.candidate.origin != "backfill":
                print(f"         evidence {row}")
            continue
        print(f"[{'PASS' if a.passed else 'FAIL'}] {a.candidate.label()}{who} <{a.candidate.origin}> {a.candidate.reason}")
        for r in a.results:
            sym = {"pass": "  ok ", "fail": "  XX ", "error": "  ?? "}[r.status]
            print(f"{sym} {r.name}: {r.explanation}")
            if not r.passed and r.evidence:
                for row in r.evidence[:3]:
                    print(f"         evidence {row}")
    print()
    # Refusals are listed above; print the outcome lines only.
    print("\n".join(line for line in result.summary.splitlines() if not line.startswith("  refused ")))
    if result.investigation:
        print("\nInvestigation (for the manager):\n" + result.investigation)
    print(f"\nTrace: {cfg.logs_dir / 'trace.jsonl'}  (run_id {result.run_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
