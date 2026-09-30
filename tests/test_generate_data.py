"""The synthetic dataset is the ground truth the quality gate is judged against, so these
tests pin down two things: every injected fault is really in the data, and the clean data
contains nothing that looks like a fault by accident."""

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pytest

from scripts.generate_data import (
    END_AT,
    N_FAULTS,
    STEP,
    Dataset,
    Fault,
    Reading,
    generate,
    write,
)


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate()


@pytest.fixture(scope="module")
def by_truck(ds: Dataset) -> dict[str, list[Reading]]:
    out: dict[str, list[Reading]] = defaultdict(list)
    for r in ds.readings:
        out[r.truck_id].append(r)
    return out


def _faults(ds: Dataset, fault_type: str) -> list[Fault]:
    return [f for f in ds.faults if f.fault_type == fault_type]


def _window(rs: list[Reading], f: Fault) -> list[Reading]:
    return [r for r in rs if f.start_at <= r.recorded_at <= f.end_at]


def test_shape(ds: Dataset) -> None:
    assert len(ds.trucks) == 50
    missing = sum(f.n_readings for f in _faults(ds, "STALE_FEED"))
    assert len(ds.readings) == 50 * 72 * 6 - missing
    assert max(r.recorded_at for r in ds.readings) == END_AT


def test_same_seed_gives_identical_files(tmp_path: Path) -> None:
    write(generate(seed=42), tmp_path / "a")
    write(generate(seed=42), tmp_path / "b")
    for name in ("trucks.csv", "telemetry.csv", "faults.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_different_seed_gives_different_data() -> None:
    assert generate(seed=1).readings != generate(seed=2).readings


def test_every_fault_on_its_own_truck(ds: Dataset) -> None:
    assert len(ds.faults) == sum(N_FAULTS.values())
    assert len({f.truck_id for f in ds.faults}) == len(ds.faults)


def test_stale_sensor_repeats_value_while_gps_moves(
    ds: Dataset, by_truck: dict[str, list[Reading]]
) -> None:
    for f in _faults(ds, "STALE_SENSOR"):
        w = _window(by_truck[f.truck_id], f)
        assert len(w) >= 8
        assert len({r.temperature_c for r in w}) == 1
        assert len({(r.lat, r.lon) for r in w}) == len(w)


def test_out_of_range_is_single_reading_outside_sensor_limits(
    ds: Dataset, by_truck: dict[str, list[Reading]]
) -> None:
    for f in _faults(ds, "OUT_OF_RANGE"):
        (r,) = _window(by_truck[f.truck_id], f)
        assert not -30 <= r.temperature_c <= 60


def test_gps_freeze_is_in_transit_for_two_hours(
    ds: Dataset, by_truck: dict[str, list[Reading]]
) -> None:
    for f in _faults(ds, "GPS_FROZEN"):
        w = _window(by_truck[f.truck_id], f)
        assert w[-1].recorded_at - w[0].recorded_at >= timedelta(minutes=120)
        assert len({(r.lat, r.lon) for r in w}) == 1
        assert all(r.trip_status == "in_transit" for r in w)


def test_feed_gaps_and_dropouts(ds: Dataset, by_truck: dict[str, list[Reading]]) -> None:
    for f in _faults(ds, "STALE_FEED"):
        rs = by_truck[f.truck_id]
        assert _window(rs, f) == []
        if f.end_at == END_AT:  # dropout: feed never comes back
            assert END_AT - rs[-1].recorded_at == timedelta(minutes=60)
        else:  # mid-route gap of 90+ min between the surrounding readings
            before = max(r.recorded_at for r in rs if r.recorded_at < f.start_at)
            after = min(r.recorded_at for r in rs if r.recorded_at > f.end_at)
            assert after - before > timedelta(minutes=90)


def test_temperature_excursion_is_a_real_breach(
    ds: Dataset, by_truck: dict[str, list[Reading]]
) -> None:
    trucks = {t.truck_id: t for t in ds.trucks}
    for f in _faults(ds, "TEMP_EXCURSION"):
        w = _window(by_truck[f.truck_id], f)
        assert max(r.temperature_c for r in w) > trucks[f.truck_id].max_temp_c
        assert any(r.cargo_condition_code != "OK" for r in w)


# --- No accidental faults: the clean trucks must look clean. ---------------------------


def _clean(ds: Dataset, by_truck: dict[str, list[Reading]]) -> list[list[Reading]]:
    faulty = {f.truck_id for f in ds.faults if f.fault_type != "TEMP_EXCURSION"}
    return [rs for tid, rs in by_truck.items() if tid not in faulty]


def test_clean_data_has_no_stuck_runs(ds: Dataset, by_truck: dict[str, list[Reading]]) -> None:
    for rs in _clean(ds, by_truck):
        run = 1
        for prev, cur in zip(rs, rs[1:], strict=False):
            run = run + 1 if cur.temperature_c == prev.temperature_c else 1
            assert run < 8, f"{cur.truck_id} has an accidental stuck run at {cur.recorded_at}"


def test_clean_data_within_sensor_range(ds: Dataset, by_truck: dict[str, list[Reading]]) -> None:
    for rs in _clean(ds, by_truck):
        assert all(-30 <= r.temperature_c <= 60 for r in rs)


def test_clean_data_never_frozen_in_transit(
    ds: Dataset, by_truck: dict[str, list[Reading]]
) -> None:
    for rs in _clean(ds, by_truck):
        for prev, cur in zip(rs, rs[1:], strict=False):
            if cur.trip_status == prev.trip_status == "in_transit":
                assert (cur.lat, cur.lon) != (prev.lat, prev.lon)


def test_clean_data_has_no_feed_gaps(ds: Dataset, by_truck: dict[str, list[Reading]]) -> None:
    for rs in _clean(ds, by_truck):
        assert all(b.recorded_at - a.recorded_at == STEP for a, b in zip(rs, rs[1:], strict=False))
        assert rs[-1].recorded_at == END_AT


def test_high_risk_shipments_exist(ds: Dataset) -> None:
    risky = {r.truck_id for r in ds.readings if r.delay_probability >= 0.65}
    assert len(risky) >= 5
