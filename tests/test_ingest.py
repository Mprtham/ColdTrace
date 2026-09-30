"""Phase 1 checkpoint, automated: generate → ingest as coldtrace_admin → read the view as
coldtrace_agent → run the gate → the flags match faults.csv exactly."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from scripts.generate_data import END_AT, generate, write
from scripts.ingest_telemetry import current_slot, load
from src.data_quality import apply_quality_checks
from src.db import role_url
from tests.test_data_quality import _expected_flags

SHIFT_TO = datetime(2027, 1, 15, 9, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("synthetic")
    write(generate(), out)
    return out


@pytest.fixture(scope="module")
def loaded(fresh_db: Callable[[], str], data_dir: Path) -> str:
    uri = fresh_db()
    load(role_url(uri, "coldtrace_admin", None), data_dir, shift_to=SHIFT_TO)
    return uri


def test_current_slot_floors_to_ten_minutes() -> None:
    assert current_slot(datetime(2026, 9, 30, 14, 37, 12, 5, tzinfo=UTC)) == datetime(
        2026, 9, 30, 14, 30, tzinfo=UTC
    )


def test_load_is_idempotent_and_shifts_timestamps(loaded: str, data_dir: Path) -> None:
    admin = role_url(loaded, "coldtrace_admin", None)
    result = load(admin, data_dir, shift_to=SHIFT_TO)  # second load over existing data
    assert (result.trucks, result.readings) == (50, len(generate().readings))
    assert result.offset == SHIFT_TO - END_AT
    with psycopg.connect(loaded) as c:
        count, latest = c.execute("SELECT count(*), max(recorded_at) FROM telemetry").fetchone()  # type: ignore[misc]
    assert count == result.readings
    assert latest == SHIFT_TO


def test_gate_over_view_matches_ground_truth(loaded: str) -> None:
    with psycopg.connect(role_url(loaded, "coldtrace_agent", None), row_factory=dict_row) as c:
        rows = c.execute("SELECT * FROM vw_fleet_with_quality").fetchall()

    out = apply_quality_checks(rows, as_of=SHIFT_TO)
    offset = SHIFT_TO - END_AT
    actual = {
        (r["truck_id"], r["recorded_at"] - offset): set(r["data_quality_flags"])
        for r in out
        if r["data_quality_flags"]
    }
    assert actual == _expected_flags(generate())
