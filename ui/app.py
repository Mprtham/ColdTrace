"""ColdTrace dispatcher UI — docs/DESIGN.md §5.2 steps 1, 8, 9.

    uv run streamlit run ui/app.py        (API must be running: uvicorn api.main:app)

Two tabs. Dispatch console: ask a question; each answer shows the Evidence panel (what
the gather node found) beside the Verdict panel (what the recommend node concluded),
with Accept / Override and a token + cost footer. Audit log: the hash-chained history,
filterable by session, tool and decision, with a chain check.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import streamlit as st

from src.config import get_settings
from ui import client as api
from ui.charts import temperature_chart
from ui.view import cost_usd, flag_labels, format_cost, readings_table, summarise_call

TOOL_NAMES = [
    "get_fleet_status",
    "get_truck_telemetry",
    "get_temperature_history",
    "get_high_risk_shipments",
    "fetch_route_conditions",
    "search_sop",
]

st.set_page_config(page_title="ColdTrace", page_icon="❄", layout="wide")
settings = get_settings()
client = api.make_client()
state = st.session_state
state.setdefault("session_id", str(uuid.uuid4()))
state.setdefault("turns", [])
state.setdefault("audit_cursors", [None])


def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except AttributeError:
        return False


def cost_text(tokens_in: int | None, tokens_out: int | None) -> str:
    cost = cost_usd(
        tokens_in, tokens_out, settings.llm_price_in_per_mtok, settings.llm_price_out_per_mtok
    )
    return format_cost(cost, settings.llm_provider)


# --- Sidebar ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### ❄ ColdTrace")
    st.caption("Evidence first, then a verdict. Every decision is hash-chained.")
    health: dict[str, Any] | None
    try:
        health = client.health()
        st.success(f"API connected · {health['llm_provider']} · clock: {health['clock_mode']}")
        if health["clock_mode"] == "replay":
            st.caption("Replay clock: 'now' is the newest reading in the database.")
    except api.ApiError as exc:
        health = None
        st.error(f"API unavailable — {exc.detail}")
    st.caption(f"Session `{state.session_id[:8]}`")
    if st.button("New session", width="stretch"):
        state.session_id = str(uuid.uuid4())
        state.turns = []
        st.rerun()

console, audit_tab = st.tabs(["Dispatch console", "Audit log"])


# --- Dispatch console ------------------------------------------------------------------


def render_evidence(evidence: list[dict[str, Any]], key: str) -> None:
    st.markdown("#### Evidence")
    st.caption("What the gather node found — check this before trusting the verdict.")
    if not evidence:
        st.info("No tools were called for this question.")
    for i, call in enumerate(evidence, 1):
        label = f"{i}. `{call['tool_name']}` — {summarise_call(call)}"
        with st.expander(label, expanded=i == 1):
            st.code(json.dumps(call["input"], ensure_ascii=False), language="json")
            if call.get("error"):
                st.warning(f"Tool error, returned to the model: {call['error']}")
                continue
            out = call["output"] or {}
            if "readings" in out:
                if call["tool_name"] == "get_temperature_history" and out["readings"]:
                    st.altair_chart(
                        temperature_chart(out["readings"], dark=is_dark()),
                        width="stretch",
                    )
                    if any(r["data_quality_flags"] for r in out["readings"]):
                        st.caption("▲ = reading with a data quality flag (see table).")
                rows = readings_table(call)
                if rows:
                    st.dataframe(rows, hide_index=True, width="stretch")
                else:
                    st.caption("No readings matched.")
            elif "results" in out:
                for r in out["results"]:
                    st.markdown(f"**{r['citation']}** · effective {r['effective_date']}")
                    st.caption(r["content"])
            elif "waypoints" in out:
                for h in out["hazards"] or ["No hazards on this route."]:
                    st.markdown(f"- {h}")
                st.dataframe(
                    [
                        {
                            "km": w["km_from_start"],
                            "°C": w["temperature_c"],
                            "gusts km/h": w["wind_gusts_kmh"],
                            "conditions": w["conditions"],
                        }
                        for w in out["waypoints"]
                    ],
                    hide_index=True,
                    width="stretch",
                )


def render_verdict(turn: dict[str, Any], key: str) -> None:
    r = turn["response"]
    st.markdown("#### Verdict")
    st.caption("What the recommend node concluded from that evidence only.")
    st.markdown(r["verdict"])
    st.markdown(f"**SOP cited:** {r['sop_cited'] or '—'}  \n**Confidence:** {r['confidence']}")
    for label in flag_labels(r["data_quality_flags"]):
        st.warning(label.removeprefix("⚠ "), icon="⚠️")  # icon + label, never color alone
    for caveat in r["caveats"]:
        st.caption(f"• {caveat}")

    decision = turn.get("decision")
    if decision:
        verb = "Accepted" if decision["decision"] == "accepted" else "Overridden"
        reason = f" — {decision['reason']}" if decision.get("reason") else ""
        st.info(f"{verb}{reason} (audit row #{decision['log_id']})")
        return
    accept_col, override_col = st.columns(2)
    if accept_col.button("Accept", key=f"accept-{key}", width="stretch"):
        decide(turn, "accepted", None)
    with override_col.popover("Override", width="stretch"):
        reason = st.text_area("Why are you overriding?", key=f"reason-{key}")
        if st.button("Record override", key=f"override-{key}", disabled=not reason.strip()):
            decide(turn, "overridden", reason.strip())


def decide(turn: dict[str, Any], decision: str, reason: str | None) -> None:
    try:
        out = client.decide(turn["response"]["log_id"], decision, reason)
        turn["decision"] = {"decision": decision, "reason": reason, "log_id": out["log_id"]}
        st.rerun()
    except api.ApiError as exc:
        st.error(f"Decision not recorded — {exc.detail}")


def render_turn(turn: dict[str, Any], key: str) -> None:
    with st.chat_message("user"):
        st.markdown(turn["question"])
    with st.chat_message("assistant", avatar="❄"):
        if "error" in turn:
            st.error(turn["error"])
            return
        r = turn["response"]
        left, right = st.columns([3, 2], gap="large")
        with left:
            render_evidence(r["evidence"], key)
        with right:
            render_verdict(turn, key)
        tokens = r["token_counts"]
        as_of = (r.get("as_of") or "")[:16].replace("T", " ")
        st.caption(
            f"intent `{r['intent']}` · clock {as_of or 'now'} UTC · "
            f"tokens {tokens['input']:,} in / {tokens['output']:,} out · "
            f"cost/query {cost_text(tokens['input'], tokens['output'])} · "
            f"audit row #{r['log_id']} `{r['row_hash'][:12]}…`"
        )


with console:
    if not state.turns:
        st.markdown(
            "Ask about the fleet in plain words — for example *“Find any trucks near "
            "Los Angeles with temperature problems and tell me what to do.”*"
        )
    for i, turn in enumerate(state.turns):
        render_turn(turn, str(i))

    question = st.chat_input("Ask about the fleet…", disabled=health is None)
    if question:
        with st.spinner("Gathering evidence and reasoning — a local model can take minutes…"):
            try:
                response = client.query(question, state.session_id)
                state.turns.append({"question": question, "response": response})
            except api.ApiError as exc:
                state.turns.append({"question": question, "error": f"No answer — {exc.detail}"})
        st.rerun()


# --- Audit log -------------------------------------------------------------------------

with audit_tab:
    scope_col, tool_col, decision_col, limit_col, verify_col = st.columns([2, 3, 2, 1, 2])
    scope = scope_col.selectbox("Sessions", ["All sessions", "This session"])
    tool = tool_col.selectbox("Tool used", ["Any tool", *TOOL_NAMES])
    decision_filter = decision_col.selectbox(
        "Decision", ["Any", "pending", "accepted", "overridden"]
    )
    limit = limit_col.selectbox("Rows", [10, 20, 50], index=1)
    filters = (scope, tool, decision_filter, limit)
    if state.get("audit_filters") != filters:  # new filter -> back to the first page
        state.audit_filters = filters
        state.audit_cursors = [None]

    verify_col.write("")
    if verify_col.button("Verify hash chain", width="stretch"):
        try:
            report = client.verify()
            if report["ok"]:
                st.success(f"Chain intact — {report['rows_checked']} rows verified.")
            else:
                st.error(
                    f"Chain broken at row #{report['first_broken_log_id']}: {report['reason']}"
                )
        except api.ApiError as exc:
            st.error(f"Could not verify — {exc.detail}")

    try:
        page = client.audit(
            session_id=state.session_id if scope == "This session" else None,
            tool_name=None if tool == "Any tool" else tool,
            decision=None if decision_filter == "Any" else decision_filter,
            limit=limit,
            cursor=state.audit_cursors[-1],
        )
    except api.ApiError as exc:
        st.error(f"Audit log unavailable — {exc.detail}")
        page = {"items": [], "next_cursor": None}

    items = page["items"]
    st.dataframe(
        [
            {
                "Row": i["log_id"],
                "Time (UTC)": i["created_at"][:16].replace("T", " "),
                "Question": i["dispatcher_question"],
                "Tools": ", ".join(t["tool_name"] for t in i["tools"]),
                "Data quality": ", ".join(i["data_quality_flags"]) or "CLEAN",
                "SOP cited": i["sop_clause_cited"] or "—",
                "Decision": i["current_decision"],
                "Reason": i["decision_reason"] or "",
                "Tokens in/out": f"{i['token_count_in'] or 0:,} / {i['token_count_out'] or 0:,}",
                "Cost": cost_text(i["token_count_in"], i["token_count_out"]),
            }
            for i in items
        ],
        hide_index=True,
        width="stretch",
    )
    newer, older, _ = st.columns([1, 1, 6])
    if newer.button("← Newer", disabled=len(state.audit_cursors) == 1):
        state.audit_cursors.pop()
        st.rerun()
    if older.button("Older →", disabled=page["next_cursor"] is None):
        state.audit_cursors.append(page["next_cursor"])
        st.rerun()

    if items:
        chosen = st.selectbox(
            "Inspect a row",
            [i["log_id"] for i in items],
            format_func=lambda log_id: f"#{log_id}",
        )
        try:
            row = client.audit_row(chosen)
            st.markdown(f"**Question:** {row['dispatcher_question']}")
            st.markdown(f"**Recommendation:** {row['final_recommendation'] or '—'}")
            st.caption(
                f"row_hash `{row['row_hash']}` · previous `{row['previous_row_hash'] or '—'}`"
            )
            for d in row["decisions"]:
                reason = f" — {d['override_reason']}" if d["override_reason"] else ""
                st.caption(f"Decision row #{d['log_id']}: {d['human_decision']}{reason}")
            with st.expander("Tool calls (full JSON, as stored)"):
                st.json(row["tool_calls"], expanded=False)
        except api.ApiError as exc:
            st.error(f"Row unavailable — {exc.detail}")
    else:
        st.caption("No audit rows match these filters.")
