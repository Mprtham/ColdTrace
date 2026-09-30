"""The router → gather → recommend graph, driven by a scripted LLM so every branch is
deterministic. Tools are the real registry specs with fake runners (no DB, no network),
except the run_query test, which writes a real audit row."""

import dataclasses
import json
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from psycopg.rows import dict_row

from src.audit import verify_chain
from src.db import role_url
from src.orchestrator import (
    INTENT_TOOLS,
    MAX_GATHER_ROUNDS,
    answer,
    build_graph,
    key_facts,
    run_query,
)
from src.tools.registry import TOOLS, ToolContext, ToolOutput, ToolSpec


class ScriptedLLM:
    """Replays canned AIMessages; records what it was shown and which tools it was given."""

    def __init__(self, *replies: AIMessage) -> None:
        self.replies = list(replies)
        self.bound: list[list[str]] = []
        self.seen: list[list[BaseMessage]] = []

    def bind_tools(self, tools: list[dict[str, Any]]) -> "ScriptedLLM":
        self.bound.append([t["function"]["name"] for t in tools])
        return self

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        return self.replies.pop(0)


def route(intent: str) -> AIMessage:
    return AIMessage(json.dumps({"intent": intent}), usage_metadata=_usage(100, 5))


def call(name: str, args: dict[str, Any], id_: str = "c1") -> AIMessage:
    return AIMessage(
        "", tool_calls=[{"name": name, "args": args, "id": id_}], usage_metadata=_usage(200, 20)
    )


def done(summary: str = "gathered") -> AIMessage:
    return AIMessage(summary, usage_metadata=_usage(300, 10))


def verdict(**fields: Any) -> AIMessage:
    body = {
        "recommendation": "Contact the driver of TRK-009 within 5 minutes.",
        "sop_clause_cited": "SOP v3.0 §1.1",
        "confidence": "high",
        "caveats": [],
    } | fields
    return AIMessage(json.dumps(body), usage_metadata=_usage(400, 60))


def _usage(i: int, o: int) -> Any:
    return {"input_tokens": i, "output_tokens": o, "total_tokens": i + o}


READING = {
    "truck_id": "TRK-009",
    "shipment_id": "SHP-009-11",
    "cargo_type": "fresh_perishables",
    "temperature_c": 5.2,
    "min_temp_c": 0.0,
    "max_temp_c": 4.0,
    "temp_in_range": False,
    "delay_probability": 0.738,
    "data_quality_flag": "CLEAN",
    "data_quality_flags": [],
}
SOP_HIT = {
    "citation": "SOP v3.0 §1.1",
    "section": "1.1",
    "version": "3.0",
    "content": "escalate after 30 minutes",
}


def fake_tools(flags: dict[str, int] | None = None) -> dict[str, ToolSpec]:
    def telemetry_run(args: Any, ctx: ToolContext) -> ToolOutput:
        if getattr(args, "radius_km", 0) > 1000:
            raise ValueError("radius_km must be in (0, 1000.0], got 5000.0")
        body = {"readings": [READING], "quality_flags": flags or {}}
        return ToolOutput({"tool_name": "telemetry", **body}, body, flags or {})

    def sop_run(args: Any, ctx: ToolContext) -> ToolOutput:
        return ToolOutput({"tool_name": "search_sop", "results": [SOP_HIT]}, {"results": []}, {})

    runs: dict[str, Callable[[Any, ToolContext], ToolOutput]] = {
        name: telemetry_run for name in TOOLS
    } | {"search_sop": sop_run}
    return {name: dataclasses.replace(spec, run=runs[name]) for name, spec in TOOLS.items()}


# --- Router ----------------------------------------------------------------------------


def test_graph_has_router_gather_recommend() -> None:
    graph = build_graph(ScriptedLLM(), fake_tools(), ToolContext())
    assert {"router", "gather", "recommend"} <= set(graph.get_graph().nodes)


def test_intent_restricts_tools_offered() -> None:
    llm = ScriptedLLM(route("sop_question"), done(), verdict())
    result = answer("What is the escalation rule?", llm=llm, tools=fake_tools())
    assert result.intent == "sop_question"
    assert llm.bound == [["search_sop"]]


def test_unknown_intent_falls_back_to_all_tools() -> None:
    llm = ScriptedLLM(AIMessage("not json at all"), done(), verdict())
    result = answer("hello?", llm=llm, tools=fake_tools())
    assert result.intent == "general"
    assert sorted(llm.bound[0]) == sorted(TOOLS)


def test_every_intent_maps_to_real_tools() -> None:
    assert all(set(tools) <= set(TOOLS) for tools in INTENT_TOOLS.values())


# --- Gather ----------------------------------------------------------------------------


def test_gather_records_tool_calls_as_evidence() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done(),
        verdict(sop_clause_cited=None),
    )
    result = answer("Trucks near LA with problems?", llm=llm, tools=fake_tools())
    (tc,) = result.tool_calls
    assert tc["tool_name"] == "get_truck_telemetry"
    assert tc["input"] == {"lat": 34.05, "lon": -118.24, "radius_km": 50}
    assert tc["output"]["readings"] == [READING]
    assert tc["error"] is None
    assert "for_llm" not in tc  # audit form only


def test_tool_error_goes_back_to_model_which_corrects_itself() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 5000}, "c1"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 500}, "c2"),
        done(),
        verdict(sop_clause_cited=None),
    )
    result = answer("Trucks near LA?", llm=llm, tools=fake_tools())
    first, second = result.tool_calls
    assert "radius_km must be in" in first["error"]
    assert second["error"] is None
    # The retry round saw the error as a ToolMessage.
    tool_msgs = [m for m in llm.seen[2] if isinstance(m, ToolMessage)]
    assert "radius_km must be in" in str(tool_msgs[-1].content)


def test_invalid_argument_types_reported_not_raised() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": "north", "lon": -118.24}),
        done(),
        verdict(sop_clause_cited=None),
    )
    (tc,) = answer("?", llm=llm, tools=fake_tools()).tool_calls
    assert tc["error"].startswith("invalid arguments: lat")


def test_tool_outside_routed_set_rejected() -> None:
    llm = ScriptedLLM(
        route("sop_question"), call("get_fleet_status", {}), done(), verdict(sop_clause_cited=None)
    )
    (tc,) = answer("What does the SOP say?", llm=llm, tools=fake_tools()).tool_calls
    assert "not available for this question" in tc["error"]
    assert tc["output"] is None


def test_gather_stops_after_max_rounds() -> None:
    loops = [call("get_fleet_status", {}, f"c{i}") for i in range(MAX_GATHER_ROUNDS)]
    llm = ScriptedLLM(route("fleet_status"), *loops, verdict(sop_clause_cited=None))
    result = answer("status?", llm=llm, tools=fake_tools())
    assert len(result.tool_calls) == MAX_GATHER_ROUNDS
    assert llm.replies == []


# --- Recommend -------------------------------------------------------------------------


def test_recommend_sees_question_and_evidence_only() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done("SECRET GATHER TRANSCRIPT"),
        verdict(sop_clause_cited=None),
    )
    answer("Trucks near LA?", llm=llm, tools=fake_tools())
    recommend_input = llm.seen[-1]
    assert len(recommend_input) == 2
    human = recommend_input[1]
    assert isinstance(human, HumanMessage)
    assert "TRK-009" in str(human.content)
    assert "SECRET GATHER TRANSCRIPT" not in str(human.content)


def test_recommend_gets_key_facts_computed_by_code() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done(),
        verdict(sop_clause_cited=None),
    )
    answer("Trucks near LA?", llm=llm, tools=fake_tools())
    assert (
        "- TRK-009 (fresh_perishables, shipment SHP-009-11): temperature 5.2°C outside safe "
        "range 0.0..4.0°C; delay probability 0.738 >= 0.65"
    ) in str(llm.seen[-1][1].content)


def test_key_facts_say_so_when_nothing_is_wrong() -> None:
    assert key_facts([]) == [
        "No reading shows a temperature breach, delay >= 0.65, or a quality flag."
    ]


def test_truck_not_in_evidence_flagged_and_confidence_dropped() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done(),
        verdict(recommendation="Escalate TRK-040 now.", sop_clause_cited=None),
    )
    result = answer("?", llm=llm, tools=fake_tools())
    assert result.confidence == "low"
    assert any("TRK-040" in c and "unverified" in c for c in result.caveats)


def test_citation_from_evidence_kept_and_normalised() -> None:
    llm = ScriptedLLM(
        route("sop_question"),
        call("search_sop", {"query": "escalation"}),
        done(),
        verdict(
            sop_clause_cited="SOP v3.0 §1.1 — Temperature Breach",
            recommendation="Escalate after 30 minutes of breach.",
        ),
    )
    result = answer("Escalation rule?", llm=llm, tools=fake_tools())
    assert result.sop_clause_cited == "SOP v3.0 §1.1"
    assert result.confidence == "high"


def test_citation_not_in_evidence_removed() -> None:
    llm = ScriptedLLM(
        route("sop_question"),
        call("search_sop", {"query": "escalation"}),
        done(),
        verdict(
            sop_clause_cited="SOP v2.4 §1.1", recommendation="Escalate after 30 minutes of breach."
        ),
    )
    result = answer("Escalation rule?", llm=llm, tools=fake_tools())
    assert result.sop_clause_cited is None
    assert result.confidence == "medium"
    assert any("SOP v2.4 §1.1" in c and "removed" in c for c in result.caveats)


def test_flagged_evidence_forces_caveat_and_caps_confidence() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done(),
        verdict(sop_clause_cited=None, confidence="high", caveats=[]),
    )
    result = answer("?", llm=llm, tools=fake_tools(flags={"STALE_SENSOR": 3}))
    assert result.quality_flags == ["STALE_SENSOR"]
    assert result.confidence == "medium"
    assert any("STALE_SENSOR" in c for c in result.caveats)


def test_unstructured_verdict_kept_as_text_with_low_confidence() -> None:
    llm = ScriptedLLM(route("sop_question"), done(), AIMessage("Just call the driver."))
    result = answer("?", llm=llm, tools=fake_tools())
    assert result.recommendation == "Just call the driver."
    assert result.confidence == "low"
    assert result.sop_clause_cited is None


def test_tokens_summed_across_all_llm_calls() -> None:
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        done(),
        verdict(sop_clause_cited=None),
    )
    result = answer("?", llm=llm, tools=fake_tools())
    assert (result.tokens_in, result.tokens_out) == (100 + 200 + 300 + 400, 5 + 20 + 10 + 60)


# --- run_query: the audited path -------------------------------------------------------


def test_run_query_writes_one_chained_audit_row(fresh_db: Callable[[], str]) -> None:
    uri = fresh_db()
    llm = ScriptedLLM(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        call("search_sop", {"query": "breach", "cargo_type": "fresh_perishables"}, "c2"),
        done(),
        verdict(),
    )
    with psycopg.connect(
        role_url(uri, "coldtrace_agent", None), row_factory=dict_row, autocommit=True
    ) as conn:
        out = run_query(
            "Trucks near LA with temperature problems?",
            llm=llm,
            tools=fake_tools(flags={"STALE_FEED": 1}),
            conn=conn,
        )
        row = conn.execute(
            "SELECT * FROM audit_log WHERE log_id = %s", (out.audit.log_id,)
        ).fetchone()
        assert verify_chain(conn).ok

    assert row is not None
    assert row["dispatcher_question"] == "Trucks near LA with temperature problems?"
    assert [t["tool_name"] for t in row["tool_calls"]] == ["get_truck_telemetry", "search_sop"]
    assert row["data_quality_flags"] == ["STALE_FEED"]
    assert row["sop_clause_cited"] == "SOP v3.0 §1.1"
    assert row["human_decision"] == "pending"
    assert (row["token_count_in"], row["token_count_out"]) == (
        100 + 200 + 200 + 300 + 400,
        5 + 20 + 20 + 10 + 60,
    )
    assert str(row["session_id"]) == out.session_id


@pytest.mark.parametrize("intent", sorted(INTENT_TOOLS))
def test_each_intent_runs_end_to_end(intent: str) -> None:
    llm = ScriptedLLM(route(intent), done(), verdict(sop_clause_cited=None))
    assert answer("?", llm=llm, tools=fake_tools()).intent == intent
