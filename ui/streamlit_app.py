"""Streamlit UI: streamlit run ui/streamlit_app.py"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st  # noqa: E402

from app.adapters.zetaris import DataSourceUnavailable  # noqa: E402
from app.actions.executor import delivery_states, manager_decision  # noqa: E402
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
        agent = PreflightAgent(cfg, auto_send=False)  # Preflight prepares, the manager sends
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
        outbox = base.logs_dir / "outbox.jsonl"
        handled = delivery_states(outbox)
        if result.decision == "already_covered":
            st.success("Already covered by earlier actions that still pass every check. Nothing new prepared.")
        for action in result.actions:
            if action["action"] in ("withdraw", "amend"):
                st.warning(action["message"])
                continue
            for w in action.get("warnings", []):
                st.warning(w)
            if action.get("delivery") != "awaiting_manager":
                st.success(action["message"])
                continue
            state = handled.get(action["action_id"])
            if state == "sent":
                st.success("Sent (simulated): " + action["message"])
            elif state == "discarded":
                st.info("Discarded: " + action["message"])
            else:
                st.info("**Ready to send** (every check passed): " + action["message"])
                send, discard = st.columns(2)
                if send.button("Send", key=f"send_{action['action_id']}", type="primary"):
                    manager_decision(outbox, action["action_id"], "send", action["message"])
                    st.rerun()
                if discard.button("Discard", key=f"discard_{action['action_id']}"):
                    manager_decision(outbox, action["action_id"], "discard", action["message"])
                    st.rerun()
        if result.decision == "partially_covered":
            st.warning(f"Still short by {result.shortfall_remaining}: nothing else passed every check. Escalated to the manager.")
        elif result.decision == "unverifiable":
            st.error("No action: live data could not be verified.")
        elif result.decision == "no_safe_action":
            st.warning("No action: nothing passed every check. Escalated to the manager.")

    if result.rechecks:
        st.subheader("Earlier actions for this shift, re-checked")
        for r in result.rechecks:
            rec = r["record"]
            sid = rec["params"].get("staff_id")
            who = result.world["names"].get(sid, sid) if sid else "agency"
            if r["status"] == "kept":
                st.info(f"Still valid: {rec['action']} · {who} (sent in {rec.get('run_id')})")
            else:
                st.error(f"Withdrawn: {rec['action']} · {who}: " + "; ".join(f"{c.name}: {c.explanation}" for c in r["gate"].failed))
                for c in r["gate"].failed:
                    if c.evidence:
                        st.dataframe(c.evidence, hide_index=True, width="stretch")

    st.subheader("Verifier: candidates and precondition checks")
    st.caption("Planner (LLM) proposes · Verifier (SQL over Zetaris) approves or rejects with evidence · "
               "refusal reasons go back to the Planner once · Memory (H-MEM) recalls and learns")
    for rp in result.replans:
        reasons = sorted({c for r in rp["refusals"] for c in r["failed_checks"]})
        st.info(f"Re-plan: the Planner received {len(rp['refusals'])} refusal(s) ({', '.join(reasons)}) and proposed "
                + (", ".join(result.world["names"].get(x, x) for x in rp["proposed"]) or "nobody new"))
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
