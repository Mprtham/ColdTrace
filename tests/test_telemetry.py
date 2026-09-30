"""The four parameterised telemetry tools against a real PostgreSQL loaded with the
synthetic fleet, connected as coldtrace_agent — the same identity the app uses."""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from scripts.generate_data import END_AT, Dataset, Fault, generate, write
from scripts.ingest_telemetry import load
from src.config import Settings
from src.db import role_url
from src.tools import telemetry
from src.tools.telemetry import (
    classify_risk,
    get_fleet_status,
    get_high_risk_shipments,
    get_temperature_history,
    get_truck_telemetry,
)

SHIFT_TO = datetime(2027, 1, 15, 9, 30, tzinfo=UTC)
OFFSET = SHIFT_TO - END_AT
LA = (34.0522, -118.2437)
Conn = psycopg.Connection[dict[str, Any]]


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate()


@pytest.fixture(scope="module")
def db(fresh_db: Callable[[], str], tmp_path_factory: pytest.TempPathFactory) -> str:
    uri = fresh_db()
    out: Path = tmp_path_factory.mktemp("synthetic")
    write(generate(), out)
    load(role_url(uri, "coldtrace_admin", None), out, shift_to=SHIFT_TO)
    return uri


@pytest.fixture
def conn(db: str) -> Iterator[Conn]:
    with psycopg.connect(role_url(db, "coldtrace_agent", None), row_factory=dict_row) as c:
        yield c


def _fault(ds: Dataset, fault_type: str) -> Fault:
    return next(f for f in ds.faults if f.fault_type == fault_type)


# --- get_fleet_status ------------------------------------------------------------------


def test_fleet_status_one_latest_reading_per_truck(conn: Conn) -> None:
    result = get_fleet_status(as_of=SHIFT_TO, conn=conn)
    assert len(result.readings) == 50
    assert len({r.truck_id for r in result.readings}) == 50


def test_silent_trucks_still_appear_flagged(ds: Dataset, conn: Conn) -> None:
    dropouts = {
        f.truck_id for f in ds.faults if f.fault_type == "STALE_FEED" and f.end_at == END_AT
    }
    result = get_fleet_status(as_of=SHIFT_TO, conn=conn)
    flagged = {r.truck_id for r in result.readings if r.data_quality_flag == "STALE_FEED"}
    assert flagged == dropouts
    assert result.quality_flags == {"STALE_FEED": len(dropouts)}


def test_fleet_status_risk_filter(conn: Conn) -> None:
    high = get_fleet_status("high", as_of=SHIFT_TO, conn=conn)
    assert high.readings
    assert {r.risk_level for r in high.readings} == {"high"}


def test_fleet_status_rejects_unknown_risk_level(conn: Conn) -> None:
    with pytest.raises(ValueError, match="risk_level"):
        get_fleet_status("critical", as_of=SHIFT_TO, conn=conn)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "delay", "in_range", "expected"),
    [
        ("CRIT", 0.1, False, "high"),
        ("OK", 0.65, True, "high"),
        ("WARN", 0.1, False, "medium"),
        ("OK", 0.1, False, "medium"),
        ("OK", 0.35, True, "medium"),
        ("OK", 0.34, True, "low"),
    ],
)
def test_classify_risk(code: str, delay: float, in_range: bool, expected: str) -> None:
    assert classify_risk(code, delay, in_range) == expected


# --- get_truck_telemetry --------------------------------------------------------------


def test_truck_telemetry_within_radius_nearest_first(conn: Conn) -> None:
    result = get_truck_telemetry(*LA, radius_km=150, as_of=SHIFT_TO, conn=conn)
    distances = [r.distance_km for r in result.readings]
    assert distances
    assert all(d is not None and d <= 150 for d in distances)
    assert distances == sorted(distances)  # type: ignore[type-var]


@pytest.mark.parametrize(
    ("lat", "lon", "radius"), [(91, 0, 10), (0, 181, 10), (34, -118, 0), (34, -118, 1001)]
)
def test_truck_telemetry_rejects_bad_parameters(
    conn: Conn, lat: float, lon: float, radius: float
) -> None:
    with pytest.raises(ValueError):
        get_truck_telemetry(lat, lon, radius, as_of=SHIFT_TO, conn=conn)


# --- get_temperature_history ----------------------------------------------------------


def test_history_returns_only_that_shipment_in_window(conn: Conn) -> None:
    shipment = get_fleet_status(as_of=SHIFT_TO, conn=conn).readings[0].shipment_id
    result = get_temperature_history(shipment, hours=2, as_of=SHIFT_TO, conn=conn)
    assert result.readings
    assert {r.shipment_id for r in result.readings} == {shipment}
    assert all(SHIFT_TO - timedelta(hours=2) < r.recorded_at <= SHIFT_TO for r in result.readings)
    times = [r.recorded_at for r in result.readings]
    assert times == sorted(times)


def test_history_catches_fault_that_began_before_window(ds: Dataset, conn: Conn) -> None:
    # A 100-min stuck run; ask for the last hour only. The run started ~40 min before the
    # window, so a gate that only saw the window would count 6 identical readings (< 8).
    stuck = _fault(ds, "STALE_SENSOR")
    as_of = stuck.end_at + OFFSET
    shipment = next(
        r.shipment_id
        for r in ds.readings
        if r.truck_id == stuck.truck_id and r.recorded_at == stuck.end_at
    )
    result = get_temperature_history(shipment, hours=1, as_of=as_of, conn=conn)
    assert len(result.readings) == 6
    assert all("STALE_SENSOR" in r.data_quality_flags for r in result.readings)


def test_history_unknown_shipment_is_empty(conn: Conn) -> None:
    assert get_temperature_history("SHP-999-99", as_of=SHIFT_TO, conn=conn).readings == []


def test_history_parameter_is_bound_not_interpolated(conn: Conn) -> None:
    injected = "SHP-001-01' OR '1'='1"
    assert get_temperature_history(injected, as_of=SHIFT_TO, conn=conn).readings == []


@pytest.mark.parametrize("hours", [0, 73])
def test_history_rejects_bad_hours(conn: Conn, hours: int) -> None:
    with pytest.raises(ValueError, match="hours"):
        get_temperature_history("SHP-001-01", hours=hours, as_of=SHIFT_TO, conn=conn)


# --- get_high_risk_shipments ----------------------------------------------------------


def test_high_risk_shipments_threshold_and_order(conn: Conn) -> None:
    result = get_high_risk_shipments(0.65, as_of=SHIFT_TO, conn=conn)
    probs = [r.delay_probability for r in result.readings]
    assert probs
    assert all(p >= 0.65 for p in probs)
    assert probs == sorted(probs, reverse=True)


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_high_risk_rejects_bad_threshold(conn: Conn, threshold: float) -> None:
    with pytest.raises(ValueError):
        get_high_risk_shipments(threshold, as_of=SHIFT_TO, conn=conn)


# --- Clock and output ------------------------------------------------------------------


def test_live_clock_marks_static_fleet_stale(conn: Conn) -> None:
    result = get_fleet_status(as_of=SHIFT_TO + timedelta(hours=1), conn=conn)
    assert result.quality_flags == {"STALE_FEED": 50}


def test_replay_clock_uses_newest_reading(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    replay = Settings(_env_file=None, clock_mode="replay")  # type: ignore[call-arg]
    monkeypatch.setattr(telemetry, "get_settings", lambda: replay)
    result = get_fleet_status(conn=conn)
    assert result.as_of == SHIFT_TO


def test_naive_as_of_rejected(conn: Conn) -> None:
    with pytest.raises(ValueError, match="timezone"):
        get_fleet_status(as_of=datetime(2027, 1, 15, 9, 30), conn=conn)


def test_result_is_json_serialisable(conn: Conn) -> None:
    result = get_truck_telemetry(*LA, radius_km=150, as_of=SHIFT_TO, conn=conn)
    payload = json.loads(json.dumps(result.to_json()))
    assert payload["tool_name"] == "get_truck_telemetry"
    assert payload["readings"][0]["distance_km"] is not None
