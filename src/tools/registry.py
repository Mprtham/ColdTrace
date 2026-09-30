"""The six tools the agent can call: schema for the LLM, validated arguments, execution,
and two views of every result — the full JSON for the audit log and a compact view for
the LLM's context (a 7B local model cannot read 50 full rows per call).

The LLM sees only the parameters in each Args model. Connection, clock, and clients come
from ToolContext and are never model-controlled.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import httpx
import psycopg
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient

from src.tools import telemetry as tel
from src.tools.sop import search_sop
from src.tools.weather import fetch_route_conditions

CargoType = Literal["fresh_perishables", "frozen", "vaccines", "insulin"]
LLM_ROW_LIMIT = 25
HISTORY_ROW_LIMIT = 48  # 8 hours at 10-minute readings


@dataclass
class ToolContext:
    conn: psycopg.Connection[dict[str, Any]] | None = None
    as_of: datetime | None = None
    qdrant: QdrantClient | None = None
    http: httpx.Client | None = None
    sop_as_of: date | None = None


@dataclass(frozen=True)
class ToolOutput:
    full: dict[str, Any]  # audit log
    for_llm: dict[str, Any]  # model context
    quality_flags: dict[str, int]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[BaseModel]
    run: Callable[[Any, ToolContext], ToolOutput]

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function schema, accepted by bind_tools() for Ollama and DeepSeek."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args.model_json_schema(),
            },
        }


# --- Argument models (the only thing the LLM controls) ---------------------------------


class FleetStatusArgs(BaseModel):
    risk_level: Literal["high", "medium", "low"] | None = Field(
        None, description="Only trucks at this risk level; omit for the whole fleet."
    )


class TruckTelemetryArgs(BaseModel):
    lat: float = Field(description="Latitude of the area centre.")
    lon: float = Field(description="Longitude of the area centre.")
    radius_km: float = Field(100.0, description="Search radius in km (max 1000).")


class TemperatureHistoryArgs(BaseModel):
    shipment_id: str = Field(description="Shipment ID, e.g. SHP-014-03.")
    hours: int = Field(6, description="How many hours back (1-72).")


class HighRiskArgs(BaseModel):
    delay_prob_threshold: float = Field(0.65, description="Minimum delay probability (0-1).")


class RouteConditionsArgs(BaseModel):
    from_lat: float
    from_lon: float
    to_lat: float
    to_lon: float
    waypoints: int = Field(5, description="Points sampled along the route (2-10).")


class SearchSopArgs(BaseModel):
    query: str = Field(description="What the procedure should cover, in plain words.")
    cargo_type: CargoType | None = Field(None, description="Limit to rules for this cargo.")


# --- Runners ---------------------------------------------------------------------------


def _telemetry(
    result: tel.TelemetryResult, limit: int = LLM_ROW_LIMIT, problems_first: bool = True
) -> ToolOutput:
    def problem_first(r: tel.TruckReading) -> tuple[int, str]:
        ok = r.data_quality_flag == "CLEAN" and r.temp_in_range and r.risk_level == "low"
        return (1 if ok else 0, r.truck_id)

    rows = sorted(result.readings, key=problem_first) if problems_first else result.readings
    compact = [
        {
            "truck_id": r.truck_id,
            "shipment_id": r.shipment_id,
            "cargo_type": r.cargo_type,
            "recorded_at": r.recorded_at.strftime("%Y-%m-%d %H:%M UTC"),
            "temperature_c": r.temperature_c,
            "safe_range_c": [r.min_temp_c, r.max_temp_c],
            "temp_in_range": r.temp_in_range,
            "condition": r.cargo_condition_code,
            "delay_probability": r.delay_probability,
            "risk_level": r.risk_level,
            "trip_status": r.trip_status,
            "data_quality_flag": r.data_quality_flag,
            **({"all_flags": r.data_quality_flags} if len(r.data_quality_flags) > 1 else {}),
            **({"distance_km": r.distance_km} if r.distance_km is not None else {}),
            **(
                {"position": [r.lat, r.lon]}
                if result.tool_name != "get_temperature_history"
                else {}
            ),
        }
        for r in rows
    ]
    for_llm = {
        "as_of": result.as_of.strftime("%Y-%m-%d %H:%M UTC"),
        "count": len(compact),
        "quality_flags": result.quality_flags,
        "readings": compact[:limit] if problems_first else compact[-limit:],
    }
    if len(compact) > limit:
        order = "trucks with problems first" if problems_first else "most recent"
        for_llm["note"] = f"showing {limit} of {len(compact)}, {order}"
    return ToolOutput(result.to_json(), for_llm, result.quality_flags)


def _run_fleet(a: FleetStatusArgs, ctx: ToolContext) -> ToolOutput:
    return _telemetry(tel.get_fleet_status(a.risk_level, as_of=ctx.as_of, conn=ctx.conn))


def _run_truck(a: TruckTelemetryArgs, ctx: ToolContext) -> ToolOutput:
    r = tel.get_truck_telemetry(a.lat, a.lon, a.radius_km, as_of=ctx.as_of, conn=ctx.conn)
    return _telemetry(r)


def _run_history(a: TemperatureHistoryArgs, ctx: ToolContext) -> ToolOutput:
    r = tel.get_temperature_history(a.shipment_id, a.hours, as_of=ctx.as_of, conn=ctx.conn)
    return _telemetry(r, limit=HISTORY_ROW_LIMIT, problems_first=False)


def _run_high_risk(a: HighRiskArgs, ctx: ToolContext) -> ToolOutput:
    r = tel.get_high_risk_shipments(a.delay_prob_threshold, as_of=ctx.as_of, conn=ctx.conn)
    return _telemetry(r)


def _run_route(a: RouteConditionsArgs, ctx: ToolContext) -> ToolOutput:
    r = fetch_route_conditions(
        a.from_lat, a.from_lon, a.to_lat, a.to_lon, a.waypoints, client=ctx.http
    )
    for_llm = {
        "route_km": r.route_km,
        "max_ambient_c": r.max_ambient_c,
        "max_gust_kmh": r.max_gust_kmh,
        "hazards": r.hazards,
        "waypoints": [
            {"km": w.km_from_start, "temp_c": w.temperature_c, "conditions": w.conditions}
            for w in r.waypoints
        ],
    }
    return ToolOutput(r.to_json(), for_llm, {})


def _run_sop(a: SearchSopArgs, ctx: ToolContext) -> ToolOutput:
    results = search_sop(a.query, a.cargo_type, as_of_date=ctx.sop_as_of, client=ctx.qdrant)
    chunks = [
        {
            "citation": sop_citation(r.version, r.section),
            "section": r.section,
            "version": r.version,
            "effective_date": r.effective_date,
            "cargo_type_scope": r.cargo_type_scope,
            "content": r.content,
            "score": round(r.score, 3),
        }
        for r in results
    ]
    full = {"tool_name": "search_sop", "results": chunks}
    return ToolOutput(
        full, {"results": [{k: c[k] for k in ("citation", "content")} for c in chunks]}, {}
    )


def sop_citation(version: str, section: str) -> str:
    return f"SOP v{version} §{section}"


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "get_fleet_status",
            "Latest reading for every truck (temperature, risk, data quality). "
            "Optionally only trucks at one risk level.",
            FleetStatusArgs,
            _run_fleet,
        ),
        ToolSpec(
            "get_truck_telemetry",
            "Latest reading for every truck within radius_km of a point, nearest first.",
            TruckTelemetryArgs,
            _run_truck,
        ),
        ToolSpec(
            "get_temperature_history",
            "Every temperature reading for one shipment over the last N hours.",
            TemperatureHistoryArgs,
            _run_history,
        ),
        ToolSpec(
            "get_high_risk_shipments",
            "Current shipments with delay probability at or above a threshold.",
            HighRiskArgs,
            _run_high_risk,
        ),
        ToolSpec(
            "fetch_route_conditions",
            "Current weather at points along the straight line between two locations, "
            "with named hazards (heat, freeze, wind, storms).",
            RouteConditionsArgs,
            _run_route,
        ),
        ToolSpec(
            "search_sop",
            "Search the current Standard Operating Procedure. Returns rules with their "
            "citation, e.g. 'SOP v3.0 §1.1'.",
            SearchSopArgs,
            _run_sop,
        ),
    )
}
