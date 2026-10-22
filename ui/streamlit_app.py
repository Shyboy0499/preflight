"""Streamlit UI: streamlit run ui/streamlit_app.py"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app.adapters.zetaris import DataSourceUnavailable  # noqa: E402
from app.agent import PreflightAgent  # noqa: E402
from app.config import ConfigError, load_config  # noqa: E402

st.set_page_config(page_title="Preflight", page_icon="✈️", layout="wide")

try:
    base = load_config()
except ConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()
st.title("Preflight")
st.caption("A shift-cover agent that checks before it acts. Every action is gated by SQL precondition checks against live data.")

with st.sidebar:
    st.subheader("Setup")
    if base.sample_mode:
        scenario = st.radio(
            "Sample data",
            ["scenario_a", "scenario_b"],
            format_func=lambda s: {"scenario_a": "A: Priya available", "scenario_b": "B: Priya on leave (leave system only)"}[s],
        )
        st.caption(f"SAMPLE_MODE: DuckDB over synthetic CSVs. Today = {base.today}.")
    else:
        scenario = base.sample_scenario
        st.caption("LIVE: Zetaris REST + configured LLM.")
    if st.button("Forget learned memories"):
        shutil.rmtree(base.state_dir, ignore_errors=True)
        st.success("H-MEM state cleared.")
    st.markdown("**Try**")
    examples = [
        "Ward 4B needs night cover tomorrow",
        "Ward 4B day shift tomorrow - do we need anyone?",
        "Ward 9Z needs cover tonight",
    ]
    for ex in examples:
        st.code(ex, language=None)

text = st.text_input("Cover request", "Ward 4B needs night cover tomorrow")
go = st.button("Run Preflight", type="primary")

if go:
    cfg = load_config(sample_scenario=scenario)
    try:
        agent = PreflightAgent(cfg)
    except DataSourceUnavailable as exc:
        st.error(f"Data source unavailable: {exc}")
        st.stop()
    with st.spinner("Checking before acting..."):
        st.session_state["result"] = agent.run(text)

result = st.session_state.get("result")
if result:
    st.caption(f"run `{result.run_id}` · data `{result.data_source}` · llm `{result.generator}` · trace in `logs/trace.jsonl`")
    if result.request is None:
        st.warning(result.summary)
        st.stop()
    st.markdown(f"**Parsed:** {result.request.label()} (via {result.parse_path})")

    naive_col, preflight_col = st.columns(2)
    with naive_col:
        st.subheader("Naive agent")
        if result.naive:
            msg = f"Books **{result.naive['staff_name']}** · `{result.naive['would_fire']}`"
            if result.naive["unsafe"]:
                st.error(msg + "\n\nWithout checking: " + "; ".join(result.naive["violations"]))
            else:
                st.info(msg)
        else:
            st.info("Would not book anyone.")
    with preflight_col:
        st.subheader("Preflight")
        if result.decision == "fired":
            st.success(result.fired["message"])
        elif result.decision == "unverifiable":
            st.error("No action: live data could not be verified.")
        else:
            st.warning("No action: nothing passed every check. Escalated to the manager.")

    st.subheader("Candidates and precondition checks")
    for a in result.attempts:
        icon = "✅" if a.passed else "⛔"
        sid = a.candidate.params.get("staff_id")
        name = f" · {result.world['names'].get(sid, sid)}" if sid else ""
        with st.expander(f"{icon} {a.candidate.label()}{name} · from {a.candidate.origin}", expanded=a.passed or bool(sid)):
            st.caption(a.candidate.reason)
            for r in a.results:
                mark = {"pass": "✅", "fail": "⛔", "error": "⚠️"}[r.status]
                st.markdown(f"{mark} **{r.name}**: {r.explanation}")
                if r.evidence and not r.passed:
                    st.dataframe(r.evidence, hide_index=True, width="stretch")
                if r.provenance:
                    st.caption(f"{r.provenance['source']} · query `{r.provenance['query']}` · id {r.provenance['query_id'][:8]}")

    if result.investigation:
        st.subheader("Investigation for the manager")
        st.text(result.investigation)

    mem_col, world_col = st.columns(2)
    with mem_col:
        st.subheader("H-MEM recall")
        returned = result.memory_trace["returned"] if result.memory_trace else []
        if not returned:
            st.caption("No memories above threshold.")
        for m in returned:
            st.markdown(f"`{m['score']:.2f}` {m['content']}")
        if result.learned_memory_id:
            st.success(f"Saved as reusable check-gated action: `{result.learned_memory_id}`")
    with world_col:
        st.subheader("World Model facts (from Zetaris)")
        report = result.world["report"]
        st.caption(f"{report['observations']} observations from {report['queries_run']} queries")
        st.text(result.world["context"]["text"])
        with st.expander("Provenance (queries)"):
            st.dataframe(report["provenance"], hide_index=True, width="stretch")
