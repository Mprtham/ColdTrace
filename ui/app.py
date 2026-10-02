"""ColdTrace dispatcher UI, per docs/DESIGN.md §5.2 steps 1, 8, 9.

    uv run streamlit run ui/app.py        (API must be running: uvicorn api.main:app)

Two tabs. Dispatch console: ask a question; each answer shows the Evidence panel (what
the gather node found) beside the Verdict panel (what the recommend node concluded),
with Accept / Override and a token + cost footer. Audit log: the hash-chained history,
filterable by session, tool and decision, with a chain check.
"""

from __future__ import annotations

import json
import secrets
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
# Colours and base dark mode live in .streamlit/config.toml; this adds fonts and accents.
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Inter:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
h1, h2, h3, h4 { font-family: 'Space Grotesk', sans-serif; }

.stApp { background-color: #0F0F0F; color: #E2E8F0; }
section[data-testid="stSidebar"] {
    background-color: #111622;
    border-right: 1px solid #1E293B;
}
.stButton button {
    background: transparent;
    border: 1px solid #22C55E;
    color: #22C55E;
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    border-radius: 4px;
}
.stButton button:hover { background: #22C55E; color: #0F0F0F; }
.stTabs [data-baseweb="tab"] {
    font-family: 'Space Grotesk', sans-serif;
    font-weight: 600;
    color: #64748B;
}
.stTabs [aria-selected="true"] { color: #22C55E !important; }
div[data-testid="stExpander"] {
    border: 1px solid #1E293B !important;
    border-radius: 4px !important;
    background: #111622 !important;
}
</style>
""",
    unsafe_allow_html=True,
)
settings = get_settings()

if settings.ui_password and not st.session_state.get("unlocked"):
    st.markdown("### ❄ ColdTrace")
    entered = st.text_input("Password", type="password")
    if entered and secrets.compare_digest(entered, settings.ui_password):
        st.session_state.unlocked = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    st.stop()

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

HEADING_STYLE = "font-family:Space Grotesk;font-weight:600;color:#E2E8F0;font-size:0.85rem"


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


with st.sidebar:
    html("<h2 style='font-family:Space Grotesk;color:#22C55E;margin-bottom:0'>❄ ColdTrace</h2>")
    html(
        "<p style='color:#64748B;font-size:0.8rem;margin-top:4px'>"
        "Evidence first. Verdict second.</p>"
    )

    st.divider()

    health: dict[str, Any] | None
    try:
        health = client.health()
        st.success(f"API connected · {health['llm_provider']} · clock: {health['clock_mode']}")
        if health["clock_mode"] == "replay":
            st.caption("Replay clock: 'now' is the newest reading in the database.")
    except api.ApiError as exc:
        health = None
        st.error(f"API unavailable: {exc.detail}")

    st.caption(f"Session `{state.session_id[:8]}`")
    if st.button("New session", width="stretch"):
        state.session_id = str(uuid.uuid4())
        state.turns = []
        st.rerun()

    st.divider()

    html(f"<p style='{HEADING_STYLE}'>STACK</p>")
    html(
        "<p style='color:#64748B;font-size:0.75rem;font-family:JetBrains Mono,monospace'>"
        "LangGraph · FastAPI · PostgreSQL (Neon)<br>"
        "Qdrant · bge-small-en-v1.5 · Groq API<br>"
        "Streamlit · Render</p>"
    )

    st.divider()

    link = "color:#22C55E;text-decoration:none"
    html(
        "<p style='color:#334155;font-size:0.72rem;text-align:center;font-family:Space Grotesk'>"
        f"Built by <a href='https://linkedin.com/in/prathameshmishra07' style='{link}'>"
        "Prathamesh Mishra</a> · "
        f"<a href='https://github.com/Mprtham/ColdTrace' style='{link}'>GitHub</a></p>"
    )

# --- Landing ---------------------------------------------------------------------------

EXAMPLE_QUESTIONS = [
    "Find any trucks near Los Angeles with temperature problems and tell me what to do.",
    "Show the temperature history for shipment SHP-026-04. Is its sensor trustworthy?",
    "What does the SOP say about escalation thresholds for fresh perishables?",
]


def render_landing() -> None:
    html(
        """
<style>
@keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:0.4} }
.lc-eyebrow {
    display:inline-flex;align-items:center;gap:8px;
    background:#1a2e1a;border:1px solid #22C55E33;
    padding:6px 14px;border-radius:999px;margin-bottom:20px;
}
.lc-eyebrow-dot {
    width:7px;height:7px;border-radius:50%;background:#22C55E;
    animation:pulse-dot 2s ease-in-out infinite;
}
.lc-eyebrow-text {
    font-family:'Space Grotesk',sans-serif;font-size:0.72rem;
    font-weight:600;color:#22C55E;letter-spacing:0.12em;text-transform:uppercase;
}
.lc-hero-wrap { text-align:center; padding:40px 0 36px 0; }
.lc-hero-h1 {
    font-family:'Space Grotesk',sans-serif;font-size:2.6rem;
    font-weight:700;color:#F1F5F9;line-height:1.12;margin:0 0 16px 0;
}
.lc-hero-sub {
    font-size:0.95rem;color:#64748B;max-width:520px;
    margin:0 auto;line-height:1.75;
}
.lc-step-row {
    display:grid;grid-template-columns:repeat(3,1fr);gap:1px;
    background:#1E293B;border:1px solid #1E293B;border-radius:6px;
    overflow:hidden;margin:32px 0;
}
.lc-step {
    background:#0F0F0F;padding:28px 24px;position:relative;
}
.lc-step-num {
    font-family:'JetBrains Mono',monospace;font-size:0.68rem;
    color:#22C55E;letter-spacing:0.15em;margin-bottom:10px;
    display:flex;align-items:center;gap:8px;
}
.lc-step-num::after {
    content:'';flex:1;height:1px;background:#22C55E33;
}
.lc-step-title {
    font-family:'Space Grotesk',sans-serif;font-size:0.95rem;
    font-weight:600;color:#E2E8F0;margin-bottom:10px;
}
.lc-step-body {
    font-size:0.8rem;color:#64748B;line-height:1.7;
}
.lc-caps-row {
    display:grid;grid-template-columns:1fr 1fr;gap:24px;margin:8px 0 32px 0;
}
.lc-cap-block { padding:20px 0; }
.lc-cap-label {
    font-family:'Space Grotesk',sans-serif;font-size:0.68rem;
    font-weight:700;color:#22C55E;letter-spacing:0.15em;
    text-transform:uppercase;margin-bottom:12px;
    padding-bottom:8px;border-bottom:1px solid #1E293B;
}
.lc-cap-item {
    font-size:0.8rem;color:#64748B;line-height:1;
    padding:6px 0 6px 16px;position:relative;
}
.lc-cap-item::before { content:"·";color:#22C55E;position:absolute;left:0;font-size:1rem; }
.lc-divider { border:none;border-top:1px solid #1E293B;margin:8px 0 24px 0; }
.lc-try-label {
    font-family:'Space Grotesk',sans-serif;font-size:0.68rem;
    font-weight:700;color:#475569;letter-spacing:0.15em;
    text-transform:uppercase;text-align:center;margin-bottom:16px;
}
</style>

<div class="lc-hero-wrap">
    <div style="display:flex;justify-content:center;margin-bottom:20px">
        <div class="lc-eyebrow">
            <div class="lc-eyebrow-dot"></div>
            <span class="lc-eyebrow-text">FDE Portfolio Project</span>
        </div>
    </div>
    <h1 class="lc-hero-h1">Built for the person making<br>the call at 3am</h1>
    <p class="lc-hero-sub">
        Cold chain systems generate records. Most of them arrive too late,
        in a form nobody can act on. This copilot shows its reasoning
        before it tells you what to do.
    </p>
</div>

<div class="lc-step-row">
    <div class="lc-step">
        <div class="lc-step-num">01</div>
        <div class="lc-step-title">The problem I saw</div>
        <div class="lc-step-body">
            A dispatcher monitoring 50 refrigerated trucks has to check sensor data,
            look up route conditions, cross-reference a 40-page SOP, and make a call,
            all at once, at 2am. Most cold chain dashboards are built for Monday morning
            review, not for the person who has to act right now.
        </div>
    </div>
    <div class="lc-step">
        <div class="lc-step-num">02</div>
        <div class="lc-step-title">What I did differently</div>
        <div class="lc-step-body">
            I found an FDE reference project and read every file line by line,
            then compared the code against its own Technical Design Document.
            Seven gaps. The audit log that claimed immutability could not write.
            The security layer checked one string. The agent ran a fixed script,
            not reasoning. I closed each gap with a traceable decision.
        </div>
    </div>
    <div class="lc-step">
        <div class="lc-step-num">03</div>
        <div class="lc-step-title">How it works</div>
        <div class="lc-step-body">
            Your question is classified by intent. A gather node collects only
            the evidence that question needs. A data quality gate runs before
            the AI sees any sensor reading. A separate node reasons over the
            evidence only. Every decision is logged to a hash-chained,
            append-only audit trail.
        </div>
    </div>
</div>

<div class="lc-caps-row">
    <div class="lc-cap-block">
        <div class="lc-cap-label">What it can do</div>
        <div class="lc-cap-item">Find trucks near a location with temperature problems</div>
        <div class="lc-cap-item">Check whether a sensor reading is trustworthy or stuck</div>
        <div class="lc-cap-item">Identify high-risk shipments by delay probability</div>
        <div class="lc-cap-item">Retrieve the correct SOP rule for a cargo type</div>
        <div class="lc-cap-item">Show route weather conditions along the truck path</div>
        <div class="lc-cap-item">Log and verify every decision with a hash chain</div>
    </div>
    <div class="lc-cap-block">
        <div class="lc-cap-label">Honest limitations</div>
        <div class="lc-cap-item">Runs on synthetic data, not a live IoT fleet feed</div>
        <div class="lc-cap-item">
            Audit log detects tampering but is not a certified immutable store
        </div>
        <div class="lc-cap-item">SOP is a short mock document, not a real carrier rulebook</div>
        <div class="lc-cap-item">Single tenant with no user authentication</div>
        <div class="lc-cap-item">Groq free tier has rate limits under concurrent load</div>
    </div>
</div>

<hr class="lc-divider">
<div class="lc-try-label">Try asking</div>
"""
    )

    for i, (col, q) in enumerate(zip(st.columns(3), EXAMPLE_QUESTIONS, strict=True)):
        # Hands the question to the chat input path below, so it runs as a real query.
        if col.button(q, width="stretch", key=f"eq-{i}", disabled=health is None):
            state.pending_question = q
            st.rerun()

    html("<hr class='lc-divider'>")


if not state.turns:
    render_landing()

console, audit_tab = st.tabs(["Dispatch console", "Audit log"])


# --- Dispatch console ------------------------------------------------------------------


def render_evidence(evidence: list[dict[str, Any]], key: str) -> None:
    st.markdown("#### Evidence")
    st.caption("What the gather node found. Check this before trusting the verdict.")
    if not evidence:
        st.info("No tools were called for this question.")
    for i, call in enumerate(evidence, 1):
        label = f"{i}. `{call['tool_name']}`: {summarise_call(call)}"
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
    st.markdown(f"**SOP cited:** {r['sop_cited'] or 'none'}  \n**Confidence:** {r['confidence']}")
    for label in flag_labels(r["data_quality_flags"]):
        st.warning(label.removeprefix("⚠ "), icon="⚠️")  # icon + label, never color alone
    for caveat in r["caveats"]:
        st.caption(f"• {caveat}")

    decision = turn.get("decision")
    if decision:
        verb = "Accepted" if decision["decision"] == "accepted" else "Overridden"
        reason = f": {decision['reason']}" if decision.get("reason") else ""
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
        st.error(f"Decision not recorded: {exc.detail}")


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
            "Ask about the fleet in plain words, for example *“Find any trucks near "
            "Los Angeles with temperature problems and tell me what to do.”*"
        )
    for i, turn in enumerate(state.turns):
        render_turn(turn, str(i))

    question = st.chat_input("Ask about the fleet…", disabled=health is None)
    question = question or state.pop("pending_question", None)
    if question:
        with st.spinner("Gathering evidence and reasoning. A local model can take minutes…"):
            try:
                response = client.query(question, state.session_id)
                state.turns.append({"question": question, "response": response})
            except api.ApiError as exc:
                state.turns.append({"question": question, "error": f"No answer: {exc.detail}"})
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
                st.success(f"Chain intact: {report['rows_checked']} rows verified.")
            else:
                st.error(
                    f"Chain broken at row #{report['first_broken_log_id']}: {report['reason']}"
                )
        except api.ApiError as exc:
            st.error(f"Could not verify: {exc.detail}")

    try:
        page = client.audit(
            session_id=state.session_id if scope == "This session" else None,
            tool_name=None if tool == "Any tool" else tool,
            decision=None if decision_filter == "Any" else decision_filter,
            limit=limit,
            cursor=state.audit_cursors[-1],
        )
    except api.ApiError as exc:
        st.error(f"Audit log unavailable: {exc.detail}")
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
                "SOP cited": i["sop_clause_cited"] or "none",
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
            st.markdown(f"**Recommendation:** {row['final_recommendation'] or 'none'}")
            st.caption(
                f"row_hash `{row['row_hash']}` · previous `{row['previous_row_hash'] or 'none'}`"
            )
            for d in row["decisions"]:
                reason = f": {d['override_reason']}" if d["override_reason"] else ""
                st.caption(f"Decision row #{d['log_id']}: {d['human_decision']}{reason}")
            with st.expander("Tool calls (full JSON, as stored)"):
                st.json(row["tool_calls"], expanded=False)
        except api.ApiError as exc:
            st.error(f"Row unavailable: {exc.detail}")
    else:
        st.caption("No audit rows match these filters.")
