"""Tests for the data quality gate (docs/DESIGN.md §3.2 B).

Two layers: hand-built series pin each detector's thresholds and edge cases; the
ground-truth test runs the gate over the full synthetic fleet and requires an exact
match with faults.csv — every fault caught, nothing else flagged."""

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.generate_data import END_AT, Dataset, generate
from src.data_quality import (
    QualityFlag,
    apply_quality_checks,
    detect_gps_frozen,
    detect_out_of_range,
    detect_stale_feed,
    detect_stale_sensor,
)

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
STEP = timedelta(minutes=10)


def _series(
    n: int,
    *,
    temps: Sequence[float] | None = None,
    moving: bool = True,
    status: str = "in_transit",
    truck_id: str = "TRK-001",
    start: datetime = T0,
) -> list[dict[str, Any]]:
    """n readings 10 min apart. Moving trucks advance ~1.1 km per reading."""
    return [
        {
            "truck_id": truck_id,
            "recorded_at": start + i * STEP,
            "lat": 34.0 + (0.01 * i if moving else 0.0),
            "lon": -118.0,
            "trip_status": status,
            "temperature_c": temps[i] if temps else 2.0 + 0.1 * (i % 5),
        }
        for i in range(n)
    ]


# --- OUT_OF_RANGE ------------------------------------------------------------------


def test_out_of_range_boundaries_are_valid() -> None:
    rows = _series(4, temps=[-30.0, 60.0, -30.1, 60.1])
    assert detect_out_of_range(rows) == [False, False, True, True]


# --- STALE_SENSOR ------------------------------------------------------------------


def test_stale_sensor_flags_whole_run_at_threshold() -> None:
    rows = _series(10, temps=[1.0] + [3.3] * 8 + [1.0])
    assert detect_stale_sensor(rows) == [False] + [True] * 8 + [False]


def test_stale_sensor_ignores_run_below_threshold() -> None:
    rows = _series(9, temps=[1.0] + [3.3] * 7 + [1.0])
    assert not any(detect_stale_sensor(rows))


def test_stale_sensor_run_at_end_of_series() -> None:
    rows = _series(9, temps=[1.0] + [3.3] * 8)
    assert detect_stale_sensor(rows) == [False] + [True] * 8


# --- GPS_FROZEN --------------------------------------------------------------------


def test_gps_frozen_flags_two_hours_in_transit() -> None:
    rows = _series(13, moving=False)  # 13 readings span exactly 120 min
    assert all(detect_gps_frozen(rows))


def test_gps_frozen_ignores_under_two_hours() -> None:
    rows = _series(12, moving=False)  # 110 min
    assert not any(detect_gps_frozen(rows))


def test_gps_frozen_ignores_parked_truck() -> None:
    rows = _series(20, moving=False, status="at_depot")
    assert not any(detect_gps_frozen(rows))


def test_gps_frozen_tolerates_jitter() -> None:
    rows = _series(13, moving=False)
    for i, r in enumerate(rows):
        r["lat"] += 0.0001 * (i % 2)  # ~11 m wobble, inside the 25 m tolerance
    assert all(detect_gps_frozen(rows))


def test_gps_frozen_run_broken_by_depot_stop() -> None:
    rows = _series(14, moving=False)
    rows[7]["trip_status"] = "at_depot"  # two 70-min in-transit halves
    assert not any(detect_gps_frozen(rows))


# --- STALE_FEED --------------------------------------------------------------------


def test_stale_feed_flags_reading_after_gap() -> None:
    rows = _series(3)
    rows[2]["recorded_at"] += timedelta(minutes=6)  # 16-min gap before reading 2
    assert detect_stale_feed(rows, as_of=rows[-1]["recorded_at"]) == [False, False, True]


def test_stale_feed_allows_exactly_max_gap() -> None:
    rows = _series(2)
    rows[1]["recorded_at"] = rows[0]["recorded_at"] + timedelta(minutes=15)
    assert not any(detect_stale_feed(rows, as_of=rows[-1]["recorded_at"]))


def test_stale_feed_flags_old_latest_reading() -> None:
    rows = _series(3)
    as_of = rows[-1]["recorded_at"] + timedelta(minutes=16)
    assert detect_stale_feed(rows, as_of=as_of) == [False, False, True]


def test_stale_feed_fresh_latest_reading() -> None:
    rows = _series(3)
    as_of = rows[-1]["recorded_at"] + timedelta(minutes=15)
    assert not any(detect_stale_feed(rows, as_of=as_of))


# --- apply_quality_checks ----------------------------------------------------------


def test_apply_on_empty_input() -> None:
    assert apply_quality_checks([], as_of=T0) == []


def test_apply_clean_series() -> None:
    rows = _series(20)
    out = apply_quality_checks(rows, as_of=rows[-1]["recorded_at"])
    assert all(r["data_quality_flag"] == "CLEAN" for r in out)
    assert all(r["data_quality_flags"] == [] for r in out)


def test_apply_multiple_flags_ordered_by_severity() -> None:
    rows = _series(12, temps=[2.0, 2.1, 2.2] + [3.3] * 9)  # stuck from reading 3
    for r in rows[5:]:
        r["recorded_at"] += timedelta(minutes=20)  # 30-min gap before reading 5
    out = apply_quality_checks(rows, as_of=rows[-1]["recorded_at"])
    assert out[5]["data_quality_flags"] == ["STALE_SENSOR", "STALE_FEED"]
    assert out[5]["data_quality_flag"] == "STALE_SENSOR"


def test_apply_keeps_input_order_and_does_not_mutate() -> None:
    a = _series(3, truck_id="TRK-A")
    b = _series(3, truck_id="TRK-B")
    rows = [b[2], a[0], b[0], a[2], b[1], a[1]]  # interleaved, unsorted
    before = [dict(r) for r in rows]
    out = apply_quality_checks(rows, as_of=T0 + 2 * STEP)
    assert [(r["truck_id"], r["recorded_at"]) for r in out] == [
        (r["truck_id"], r["recorded_at"]) for r in rows
    ]
    assert rows == before
    assert all("data_quality_flag" not in r for r in rows)


def test_apply_checks_trucks_independently() -> None:
    # Each truck alone has a 4-reading stuck run; interleaved they must not combine.
    a = _series(4, temps=[3.3] * 4, truck_id="TRK-A")
    b = _series(4, temps=[3.3] * 4, truck_id="TRK-B", start=T0 + timedelta(minutes=5))
    out = apply_quality_checks(a + b, as_of=T0 + 4 * STEP)
    assert all("STALE_SENSOR" not in r["data_quality_flags"] for r in out)


def test_flag_enum_values() -> None:
    assert [str(f) for f in QualityFlag] == [
        "CLEAN",
        "OUT_OF_RANGE",
        "STALE_SENSOR",
        "GPS_FROZEN",
        "STALE_FEED",
    ]


# --- Ground truth: the gate against the full synthetic fleet ---------------------


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate()


def _expected_flags(ds: Dataset) -> dict[tuple[str, datetime], set[str]]:
    """Which (truck, reading time) must carry which flag, derived from faults.csv."""
    series: dict[str, list[datetime]] = {}
    for r in ds.readings:
        series.setdefault(r.truck_id, []).append(r.recorded_at)

    expected: dict[tuple[str, datetime], set[str]] = {}
    for f in ds.faults:
        times = series[f.truck_id]
        if f.fault_type == "TEMP_EXCURSION":
            continue  # real cargo event: must stay CLEAN
        if f.fault_type == "STALE_FEED":
            # Gap: the first reading after it. Dropout: the (now old) latest reading.
            after = [t for t in times if t > f.end_at]
            hit = after[0] if after else times[-1]
            expected.setdefault((f.truck_id, hit), set()).add("STALE_FEED")
        else:
            for t in times:
                if f.start_at <= t <= f.end_at:
                    expected.setdefault((f.truck_id, t), set()).add(f.fault_type)
    return expected


def test_gate_matches_ground_truth_exactly(ds: Dataset) -> None:
    out = apply_quality_checks([asdict(r) for r in ds.readings], as_of=END_AT)
    actual = {
        (r["truck_id"], r["recorded_at"]): set(r["data_quality_flags"])
        for r in out
        if r["data_quality_flags"]
    }
    expected = _expected_flags(ds)
    assert actual == expected
    assert len(actual) == 3 * 10 + 3 * 1 + 3 * 14 + 5  # stuck + OOR + frozen + feed


def test_real_excursions_stay_clean(ds: Dataset) -> None:
    excursion_trucks = {f.truck_id for f in ds.faults if f.fault_type == "TEMP_EXCURSION"}
    rows = [asdict(r) for r in ds.readings if r.truck_id in excursion_trucks]
    out = apply_quality_checks(rows, as_of=END_AT)
    assert {r["data_quality_flag"] for r in out} == {"CLEAN"}
    assert any(r["cargo_condition_code"] != "OK" for r in out)  # the breach is real
