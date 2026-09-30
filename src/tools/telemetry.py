"""Parameterised telemetry tools — docs/DESIGN.md §3.2 Improvement A.

The LLM picks one of these four functions and supplies parameter values; it never writes
SQL. Every value it supplies is validated here and passed to psycopg as a bound parameter.

Each tool is fetch-then-validate, not a plain SELECT wrapper:
  1. fetch each truck's recent history from vw_fleet_with_quality — its latest reading
     plus `Thresholds.lookback` before it, so a truck that has gone silent still appears;
  2. run apply_quality_checks() over that history;
  3. narrow to what the agent asked for.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

import psycopg

from src.config import get_settings
from src.data_quality import DEFAULT_THRESHOLDS, apply_quality_checks
from src.db import agent_connection
from src.geo import haversine_km

RiskLevel = Literal["high", "medium", "low"]
Conn = psycopg.Connection[dict[str, Any]]

HIGH_DELAY_PROBABILITY = 0.65
MEDIUM_DELAY_PROBABILITY = 0.35
MAX_RADIUS_KM = 1000.0
MAX_HISTORY_HOURS = 72


@dataclass(frozen=True)
class TruckReading:
    truck_id: str
    shipment_id: str
    cargo_type: str
    recorded_at: datetime
    lat: float
    lon: float
    trip_status: str
    temperature_c: float
    min_temp_c: float
    max_temp_c: float
    temp_in_range: bool
    cargo_condition_code: str
    delay_probability: float
    route_risk_index: float
    risk_level: RiskLevel
    data_quality_flag: str
    data_quality_flags: list[str]
    distance_km: float | None = None  # set by get_truck_telemetry only


@dataclass(frozen=True)
class TelemetryResult:
    tool_name: str
    as_of: datetime
    readings: list[TruckReading]
    quality_flags: dict[str, int] = field(default_factory=dict)  # flag -> readings raising it

    def to_json(self) -> dict[str, Any]:
        """JSON-safe form for the agent's context and the audit log's tool_calls."""
        return {
            "tool_name": self.tool_name,
            "as_of": self.as_of.isoformat(),
            "quality_flags": self.quality_flags,
            "readings": [
                {**asdict(r), "recorded_at": r.recorded_at.isoformat()} for r in self.readings
            ],
        }


# --- The four tools --------------------------------------------------------------------


def get_fleet_status(
    risk_level: RiskLevel | None = None,
    *,
    as_of: datetime | None = None,
    conn: Conn | None = None,
) -> TelemetryResult:
    """Latest reading for every truck, optionally only those at `risk_level`."""
    if risk_level is not None and risk_level not in get_args(RiskLevel):
        raise ValueError(f"risk_level must be one of {get_args(RiskLevel)}, got {risk_level!r}")
    with _session(conn, as_of) as (c, as_of):
        latest = _latest_per_truck(_fetch_fleet(c, as_of), as_of)
    if risk_level:
        latest = [r for r in latest if r.risk_level == risk_level]
    return _result("get_fleet_status", as_of, latest)


def get_truck_telemetry(
    lat: float,
    lon: float,
    radius_km: float,
    *,
    as_of: datetime | None = None,
    conn: Conn | None = None,
) -> TelemetryResult:
    """Latest reading for every truck within `radius_km` of (lat, lon), nearest first."""
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError(f"({lat}, {lon}) is not a valid coordinate")
    if not 0 < radius_km <= MAX_RADIUS_KM:
        raise ValueError(f"radius_km must be in (0, {MAX_RADIUS_KM}], got {radius_km}")
    with _session(conn, as_of) as (c, as_of):
        fleet = _latest_per_truck(_fetch_fleet(c, as_of), as_of)
    nearby = []
    for r in fleet:
        d = haversine_km((lat, lon), (r.lat, r.lon))
        if d <= radius_km:
            nearby.append(_replace(r, distance_km=round(d, 1)))
    nearby.sort(key=lambda r: r.distance_km or 0.0)
    return _result("get_truck_telemetry", as_of, nearby)


def get_temperature_history(
    shipment_id: str,
    hours: int = 6,
    *,
    as_of: datetime | None = None,
    conn: Conn | None = None,
) -> TelemetryResult:
    """Every reading for `shipment_id` in the last `hours`, oldest first. The quality
    gate sees the truck's full history over hours + lookback, so a fault that began
    before the window (or on the previous shipment) is still detected."""
    if not 1 <= hours <= MAX_HISTORY_HOURS:
        raise ValueError(f"hours must be in [1, {MAX_HISTORY_HOURS}], got {hours}")
    with _session(conn, as_of) as (c, as_of):
        since = as_of - timedelta(hours=hours)
        rows = c.execute(
            """
        SELECT v.* FROM vw_fleet_with_quality v
        WHERE v.truck_id = (
                SELECT truck_id FROM vw_fleet_with_quality WHERE shipment_id = %(shipment_id)s
                LIMIT 1)
          AND v.recorded_at >  %(since)s
          AND v.recorded_at <= %(as_of)s
        ORDER BY v.recorded_at
        """,
            {
                "shipment_id": shipment_id,
                "since": since - DEFAULT_THRESHOLDS.lookback,
                "as_of": as_of,
            },
        ).fetchall()
    readings = [
        r
        for r in _to_readings(apply_quality_checks(rows, as_of))
        if r.shipment_id == shipment_id and r.recorded_at > since
    ]
    return _result("get_temperature_history", as_of, readings)


def get_high_risk_shipments(
    delay_prob_threshold: float = HIGH_DELAY_PROBABILITY,
    *,
    as_of: datetime | None = None,
    conn: Conn | None = None,
) -> TelemetryResult:
    """Current shipments whose latest delay probability is at or above the threshold,
    highest first."""
    if not 0 <= delay_prob_threshold <= 1:
        raise ValueError(f"delay_prob_threshold must be in [0, 1], got {delay_prob_threshold}")
    with _session(conn, as_of) as (c, as_of):
        fleet = _latest_per_truck(_fetch_fleet(c, as_of), as_of)
    risky = [r for r in fleet if r.delay_probability >= delay_prob_threshold]
    risky.sort(key=lambda r: r.delay_probability, reverse=True)
    return _result("get_high_risk_shipments", as_of, risky)


# --- Shared fetch-then-validate ---------------------------------------------------------


def classify_risk(
    cargo_condition_code: str, delay_probability: float, temp_in_range: bool
) -> RiskLevel:
    if cargo_condition_code == "CRIT" or delay_probability >= HIGH_DELAY_PROBABILITY:
        return "high"
    if (
        cargo_condition_code == "WARN"
        or not temp_in_range
        or delay_probability >= MEDIUM_DELAY_PROBABILITY
    ):
        return "medium"
    return "low"


def _fetch_fleet(conn: Conn, as_of: datetime) -> list[dict[str, Any]]:
    """Each truck's latest reading at `as_of` plus the lookback window before it."""
    return conn.execute(
        """
        WITH latest AS (
            SELECT truck_id, max(recorded_at) AS last_at
            FROM vw_fleet_with_quality
            WHERE recorded_at <= %(as_of)s
            GROUP BY truck_id
        )
        SELECT v.* FROM vw_fleet_with_quality v
        JOIN latest l USING (truck_id)
        WHERE v.recorded_at >  l.last_at - %(lookback)s
          AND v.recorded_at <= %(as_of)s
        ORDER BY v.truck_id, v.recorded_at
        """,
        {"as_of": as_of, "lookback": DEFAULT_THRESHOLDS.lookback},
    ).fetchall()


def _latest_per_truck(rows: list[dict[str, Any]], as_of: datetime) -> list[TruckReading]:
    latest: dict[str, TruckReading] = {}
    for r in _to_readings(apply_quality_checks(rows, as_of)):
        if r.truck_id not in latest or r.recorded_at > latest[r.truck_id].recorded_at:
            latest[r.truck_id] = r
    return [latest[t] for t in sorted(latest)]


def _to_readings(rows: list[dict[str, Any]]) -> list[TruckReading]:
    out = []
    for r in rows:
        in_range = r["min_temp_c"] <= r["temperature_c"] <= r["max_temp_c"]
        out.append(
            TruckReading(
                truck_id=r["truck_id"],
                shipment_id=r["shipment_id"],
                cargo_type=r["cargo_type"],
                recorded_at=r["recorded_at"],
                lat=r["lat"],
                lon=r["lon"],
                trip_status=r["trip_status"],
                temperature_c=r["temperature_c"],
                min_temp_c=r["min_temp_c"],
                max_temp_c=r["max_temp_c"],
                temp_in_range=in_range,
                cargo_condition_code=r["cargo_condition_code"],
                delay_probability=r["delay_probability"],
                route_risk_index=r["route_risk_index"],
                risk_level=classify_risk(
                    r["cargo_condition_code"], r["delay_probability"], in_range
                ),
                data_quality_flag=r["data_quality_flag"],
                data_quality_flags=r["data_quality_flags"],
            )
        )
    return out


def _result(tool_name: str, as_of: datetime, readings: list[TruckReading]) -> TelemetryResult:
    counts: dict[str, int] = {}
    for r in readings:
        for flag in r.data_quality_flags:
            counts[flag] = counts.get(flag, 0) + 1
    return TelemetryResult(tool_name, as_of, readings, counts)


@contextmanager
def _session(conn: Conn | None, as_of: datetime | None) -> Iterator[tuple[Conn, datetime]]:
    """The caller's connection (or a fresh coldtrace_agent one) and the resolved as_of."""
    if conn is not None:
        yield conn, resolve_as_of(conn, as_of)
        return
    with agent_connection() as own:
        yield own, resolve_as_of(own, as_of)


def resolve_as_of(conn: Conn, as_of: datetime | None) -> datetime:
    """Explicit as_of wins; otherwise now (live) or the newest reading (replay)."""
    if as_of is not None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return as_of
    if get_settings().clock_mode == "replay":
        row = conn.execute(
            "SELECT max(recorded_at) AS latest FROM vw_fleet_with_quality"
        ).fetchone()
        if row and row["latest"]:
            latest: datetime = row["latest"]
            return latest
    return datetime.now(UTC)


def _replace(r: TruckReading, **changes: Any) -> TruckReading:
    return TruckReading(**{**asdict(r), **changes})
