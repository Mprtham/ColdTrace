"""Load data/synthetic/*.csv into PostgreSQL as coldtrace_admin.

    uv run python -m scripts.ingest_telemetry            # replay: latest reading = now
    uv run python -m scripts.ingest_telemetry --no-shift # keep generated timestamps

The generated fleet ends at a fixed END_AT so it is reproducible. For a live-looking demo
the loader shifts every timestamp by one offset so the newest reading lands at the current
10-minute mark; relative timing (and so every fault) is unchanged. Re-run to refresh.

Idempotent: truncates trucks and telemetry, then bulk-loads with COPY in one transaction.
Readings are loaded exactly as generated, faults included — the gate annotates, never drops.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

from src.config import get_settings
from src.db import role_url, scrubbed_errors

DATA_DIR = Path("data/synthetic")
STEP = timedelta(minutes=10)

TRUCK_COLS = (
    "truck_id",
    "plate_number",
    "cargo_type",
    "min_temp_c",
    "max_temp_c",
    "home_depot",
    "created_at",
)
TELEMETRY_COLS = (
    "truck_id",
    "shipment_id",
    "recorded_at",
    "ingested_at",
    "lat",
    "lon",
    "trip_status",
    "temperature_c",
    "cargo_condition_code",
    "delay_probability",
    "route_risk_index",
)
TIME_COLS = {"created_at", "recorded_at", "ingested_at"}


@dataclass
class IngestResult:
    trucks: int
    readings: int
    offset: timedelta
    latest_reading: datetime


def current_slot(now: datetime | None = None) -> datetime:
    """`now` floored to the 10-minute reading grid."""
    now = now or datetime.now(UTC)
    return now - timedelta(
        minutes=now.minute % 10, seconds=now.second, microseconds=now.microsecond
    )


def _read(path: Path, cols: tuple[str, ...], offset: timedelta) -> list[tuple[object, ...]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [
            tuple(
                datetime.fromisoformat(row[c]) + offset if c in TIME_COLS else row[c] for c in cols
            )
            for row in csv.DictReader(fh)
        ]


def load(
    conninfo: str, data_dir: Path = DATA_DIR, shift_to: datetime | None = None
) -> IngestResult:
    with (data_dir / "telemetry.csv").open(encoding="utf-8", newline="") as fh:
        latest = max(datetime.fromisoformat(r["recorded_at"]) for r in csv.DictReader(fh))
    offset = shift_to - latest if shift_to else timedelta(0)

    trucks = _read(data_dir / "trucks.csv", TRUCK_COLS, offset)
    readings = _read(data_dir / "telemetry.csv", TELEMETRY_COLS, offset)

    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE telemetry, trucks"
        )  # no RESTART IDENTITY: admin does not own sequences
        with cur.copy(f"COPY trucks ({', '.join(TRUCK_COLS)}) FROM STDIN") as cp:
            for row in trucks:
                cp.write_row(row)
        with cur.copy(f"COPY telemetry ({', '.join(TELEMETRY_COLS)}) FROM STDIN") as cp:
            for row in readings:
                cp.write_row(row)

    return IngestResult(len(trucks), len(readings), offset, latest + offset)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load synthetic fleet CSVs into PostgreSQL.")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--no-shift", action="store_true", help="keep generated timestamps")
    args = parser.parse_args()

    settings = get_settings()
    url = role_url(
        settings.require_database_url(), "coldtrace_admin", settings.coldtrace_admin_password
    )
    with scrubbed_errors(settings.secrets()):
        result = load(url, args.data_dir, shift_to=None if args.no_shift else current_slot())
    print(
        f"Loaded {result.trucks} trucks, {result.readings} readings as coldtrace_admin. "
        f"Timestamps shifted by {result.offset}; "
        f"latest reading {result.latest_reading:%Y-%m-%d %H:%M} UTC."
    )


if __name__ == "__main__":
    main()
