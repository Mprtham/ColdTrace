"""LangGraph orchestrator: router → gather → recommend (docs/DESIGN.md §3.2 C, §5.2).

router     classifies the question's intent. The intent decides which tools gather may
           use — real routing, unlike the reference project's fixed three-tool order.
gather     calls tools until it has the evidence. Invalid arguments and tool errors go
           back to the model so it can correct itself (the reference TDD claimed this;
           its code had no such path). Facts only, no recommendations.
recommend  sees only the question and the structured evidence — no tools, no gather
           transcript — and returns a recommendation, SOP citation, confidence, caveats.
           Deterministic guards then drop citations that are not in the evidence and
           force a caveat when evidence carries data quality flags.

run_query() wraps the graph and writes one hash-chained audit row per question.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID, uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from src.audit import AuditEntry, write_agent_row
from src.config import get_settings
from src.db import agent_connection, redact
from src.tools.registry import TOOLS, ToolContext, ToolSpec
from src.tools.telemetry import HIGH_DELAY_PROBABILITY, Conn, resolve_as_of

PROMPTS = Path(__file__).parent / "prompts"
MAX_GATHER_ROUNDS = 4
CONFIDENCE = ("high", "medium", "low")
TRUCK_ID_RE = re.compile(r"\bTRK-\d{3}\b")

INTENT_TOOLS: dict[str, tuple[str, ...]] = {
    "fleet_status": ("get_fleet_status", "get_high_risk_shipments", "search_sop"),
    "temperature_alarm": (
        "get_truck_telemetry",
        "get_temperature_history",
        "get_fleet_status",
        "fetch_route_conditions",
        "search_sop",
    ),
    "route_query": ("get_truck_telemetry", "fetch_route_conditions", "search_sop"),
    "high_risk": ("get_high_risk_shipments", "get_temperature_history", "search_sop"),
    "sop_question": ("search_sop",),
    "general": tuple(TOOLS),
}


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


class AgentState(TypedDict, total=False):
    question: str
    intent: str
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    gather_summary: str
    recommendation: str
    sop_clause_cited: str | None
    confidence: str
    caveats: list[str]
    tokens_in: Annotated[int, operator.add]
    tokens_out: Annotated[int, operator.add]


@dataclass
class AgentResult:
    question: str
    intent: str
    tool_calls: list[dict[str, Any]]  # audit form: tool_name, input, output, quality_flags, error
    gather_summary: str
    recommendation: str
    sop_clause_cited: str | None
    confidence: str
    caveats: list[str]
    quality_flags: list[str]
    tokens_in: int
    tokens_out: int
    as_of: datetime | None = None


@dataclass
class QueryResult:
    session_id: str
    audit: AuditEntry
    result: AgentResult = field(repr=False)


# --- Graph -----------------------------------------------------------------------------


def build_graph(llm: Any, tools: dict[str, ToolSpec], ctx: ToolContext) -> Any:
    def router(state: AgentState) -> dict[str, Any]:
        ai = llm.invoke([SystemMessage(_prompt("router")), HumanMessage(state["question"])])
        intent = parse_json(_text(ai)).get("intent")
        return {
            "intent": intent if intent in INTENT_TOOLS else "general",
            **_usage(ai),
        }

    def gather(state: AgentState) -> dict[str, Any]:
        allowed = {n: tools[n] for n in INTENT_TOOLS[state["intent"]] if n in tools}
        bound = llm.bind_tools([spec.schema() for spec in allowed.values()])
        messages: list[BaseMessage] = [
            SystemMessage(_prompt("gather")),
            HumanMessage(
                f"Dispatcher question: {state['question']}\nRouted intent: {state['intent']}"
            ),
        ]
        calls: list[dict[str, Any]] = []
        tokens = {"tokens_in": 0, "tokens_out": 0}
        summary = ""
        for _ in range(MAX_GATHER_ROUNDS):
            ai = bound.invoke(messages)
            for k, v in _usage(ai).items():
                tokens[k] += v
            messages.append(ai)
            tool_calls = getattr(ai, "tool_calls", None) or []
            if not tool_calls:
                summary = _text(ai).strip()
                break
            for tc in tool_calls:
                record = execute_tool(tc["name"], tc.get("args") or {}, allowed, ctx)
                calls.append(record)
                reply = {"error": record["error"]} if record["error"] else record["for_llm"]
                messages.append(
                    ToolMessage(json.dumps(reply, ensure_ascii=False), tool_call_id=tc["id"])
                )
        return {"tool_calls": calls, "gather_summary": summary, **tokens}

    def recommend(state: AgentState) -> dict[str, Any]:
        calls = state.get("tool_calls", [])
        flags = quality_flags(calls)
        evidence = [
            {"tool": c["tool_name"], "input": c["input"]}
            | ({"error": c["error"]} if c["error"] else {"result": c["for_llm"]})
            for c in calls
        ]
        ai = llm.invoke(
            [
                SystemMessage(_prompt("recommend")),
                HumanMessage(
                    f"Dispatcher question: {state['question']}\n\n"
                    "Key facts (computed from the evidence by code, not by a model):\n"
                    + "\n".join(f"- {fact}" for fact in key_facts(calls))
                    + f"\n\nEvidence (JSON):\n{json.dumps(evidence, ensure_ascii=False)}\n\n"
                    f"Data quality flags in evidence: {', '.join(flags) or 'none'}"
                ),
            ]
        )
        verdict = apply_guards(parse_verdict(_text(ai)), calls, flags)
        return {**verdict, **_usage(ai)}

    graph = StateGraph(AgentState)
    graph.add_node("router", router)
    graph.add_node("gather", gather)
    graph.add_node("recommend", recommend)
    graph.add_edge(START, "router")
    graph.add_edge("router", "gather")
    graph.add_edge("gather", "recommend")
    graph.add_edge("recommend", END)
    return graph.compile()


def execute_tool(
    name: str, args: dict[str, Any], allowed: dict[str, ToolSpec], ctx: ToolContext
) -> dict[str, Any]:
    """Validate and run one tool call. Every failure becomes an error the model can read."""
    record: dict[str, Any] = {
        "tool_name": name,
        "input": args,
        "output": None,
        "for_llm": None,
        "quality_flags": {},
        "error": None,
    }
    spec = allowed.get(name)
    if spec is None:
        record["error"] = (
            f"tool '{name}' is not available for this question; "
            f"available: {', '.join(allowed) or 'none'}"
        )
        return record
    try:
        parsed = spec.args.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        record["error"] = f"invalid arguments: {problems}"
        return record
    try:
        out = spec.run(parsed, ctx)
    except Exception as exc:  # noqa: BLE001 — any tool failure is reported back, not raised
        record["error"] = redact(f"{type(exc).__name__}: {exc}", get_settings().secrets())
        return record
    record.update(output=out.full, for_llm=out.for_llm, quality_flags=out.quality_flags)
    return record


# --- Verdict parsing and guards --------------------------------------------------------


def parse_json(text: str) -> dict[str, Any]:
    """First JSON object in `text`, tolerating code fences and chatter; {} if none."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_verdict(text: str) -> dict[str, Any]:
    data = parse_json(text)
    if not data.get("recommendation"):
        # The model ignored the format. Keep what it said, but do not trust it.
        return {
            "recommendation": text.strip(),
            "sop_clause_cited": None,
            "confidence": "low",
            "caveats": ["The model did not return a structured verdict; read with care."],
        }
    caveats = data.get("caveats") or []
    confidence = data.get("confidence")
    return {
        "recommendation": str(data["recommendation"]),
        "sop_clause_cited": data.get("sop_clause_cited") or None,
        "confidence": confidence if confidence in CONFIDENCE else "low",
        "caveats": [str(c) for c in caveats] if isinstance(caveats, list) else [str(caveats)],
    }


def apply_guards(
    verdict: dict[str, Any], calls: list[dict[str, Any]], flags: list[str]
) -> dict[str, Any]:
    verdict = {**verdict, "caveats": list(verdict["caveats"])}

    # 1. A citation must be one the SOP tool actually returned.
    cited = verdict["sop_clause_cited"]
    if cited:
        match = next((c for c in retrieved_citations(calls) if str(cited).startswith(c)), None)
        if match is None:
            verdict["caveats"].append(
                f"Citation '{cited}' was removed: it is not in the retrieved SOP evidence."
            )
            if verdict["confidence"] == "high":
                verdict["confidence"] = "medium"
        verdict["sop_clause_cited"] = match

    # 2. Every truck the recommendation names must be in the evidence.
    known = evidence_truck_ids(calls)
    unknown = sorted(set(TRUCK_ID_RE.findall(verdict["recommendation"])) - known)
    if unknown:
        verdict["caveats"].append(
            f"The recommendation names {', '.join(unknown)}, which the evidence does not "
            "contain; treat that part as unverified."
        )
        verdict["confidence"] = "low"

    # 3. Flagged evidence always carries a caveat and never high confidence.
    if flags:
        if verdict["confidence"] == "high":
            verdict["confidence"] = "medium"
        said = " ".join([verdict["recommendation"], *verdict["caveats"]])
        if not any(f in said for f in flags):
            verdict["caveats"].append(
                f"Evidence includes data quality flags ({', '.join(flags)}); "
                "verify those readings before acting on them."
            )
    return verdict


def key_facts(calls: list[dict[str, Any]]) -> list[str]:
    """Per-truck facts that matter, stated by code so the model does not have to pair
    numbers with trucks itself (a 7B model misattributed a delay figure in testing)."""
    facts: dict[str, str] = {}
    for c in calls:
        for r in (c["output"] or {}).get("readings", []):
            reasons = []
            if not r["temp_in_range"]:
                reasons.append(
                    f"temperature {r['temperature_c']}°C outside safe range "
                    f"{r['min_temp_c']}..{r['max_temp_c']}°C"
                )
            if r["delay_probability"] >= HIGH_DELAY_PROBABILITY:
                reasons.append(f"delay probability {r['delay_probability']} >= 0.65")
            if r["data_quality_flag"] != "CLEAN":
                reasons.append(f"data quality {', '.join(r['data_quality_flags'])}")
            if reasons:
                facts[r["truck_id"]] = (
                    f"{r['truck_id']} ({r['cargo_type']}, shipment {r['shipment_id']}): "
                    + "; ".join(reasons)
                )
    if not facts:
        return ["No reading shows a temperature breach, delay >= 0.65, or a quality flag."]
    return [facts[t] for t in sorted(facts)][:30]


def evidence_truck_ids(calls: list[dict[str, Any]]) -> set[str]:
    return {r["truck_id"] for c in calls for r in (c["output"] or {}).get("readings", [])}


def retrieved_citations(calls: list[dict[str, Any]]) -> list[str]:
    return [
        r["citation"]
        for c in calls
        if c["tool_name"] == "search_sop" and c["output"]
        for r in c["output"]["results"]
    ]


def quality_flags(calls: list[dict[str, Any]]) -> list[str]:
    return sorted({flag for c in calls for flag in c.get("quality_flags") or {}})


# --- Entry points ----------------------------------------------------------------------


def answer(
    question: str,
    *,
    llm: Any = None,
    tools: dict[str, ToolSpec] | None = None,
    ctx: ToolContext | None = None,
) -> AgentResult:
    """Run the graph. No database write; see run_query() for the audited path."""
    if llm is None:
        from src.llm import get_llm

        llm = get_llm()
    ctx = ctx or ToolContext()
    state = build_graph(llm, tools or TOOLS, ctx).invoke({"question": question})
    calls = state.get("tool_calls", [])
    return AgentResult(
        question=question,
        intent=state["intent"],
        tool_calls=[{k: v for k, v in c.items() if k != "for_llm"} for c in calls],
        gather_summary=state.get("gather_summary", ""),
        recommendation=state["recommendation"],
        sop_clause_cited=state["sop_clause_cited"],
        confidence=state["confidence"],
        caveats=state["caveats"],
        quality_flags=quality_flags(calls),
        tokens_in=state.get("tokens_in", 0),
        tokens_out=state.get("tokens_out", 0),
        as_of=ctx.as_of,
    )


def run_query(
    question: str,
    session_id: UUID | str | None = None,
    *,
    llm: Any = None,
    tools: dict[str, ToolSpec] | None = None,
    conn: Conn | None = None,
    ctx: ToolContext | None = None,
    as_of: datetime | None = None,
) -> QueryResult:
    """Answer a dispatcher question and write its audit row (docs/DESIGN.md §5.2 steps 3-7)."""
    session = str(session_id or uuid4())
    with _connection(conn) as c:
        ctx = ctx or ToolContext()
        ctx.conn = c
        ctx.as_of = resolve_as_of(c, as_of or ctx.as_of)
        result = answer(question, llm=llm, tools=tools, ctx=ctx)
        entry = write_agent_row(
            c,
            session_id=session,
            dispatcher_question=question,
            tool_calls=result.tool_calls,
            data_quality_flags=result.quality_flags,
            final_recommendation=result.recommendation,
            sop_clause_cited=result.sop_clause_cited,
            token_count_in=result.tokens_in,
            token_count_out=result.tokens_out,
        )
    return QueryResult(session, entry, result)


@contextmanager
def _connection(conn: Conn | None) -> Iterator[Conn]:
    if conn is not None:
        yield conn
    else:
        with agent_connection() as own:
            yield own


def _text(message: BaseMessage | AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part if isinstance(part, str) else str(part.get("text", "")) for part in content)


def _usage(message: BaseMessage) -> dict[str, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return {
        "tokens_in": int(meta.get("input_tokens", 0)),
        "tokens_out": int(meta.get("output_tokens", 0)),
    }
