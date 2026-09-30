"""Data quality gate — docs/DESIGN.md §3.2 Improvement B.

Every telemetry result passes through `apply_quality_checks()` before the agent sees it.
Flags are computed at query time, never stored (docs/DESIGN.md §4.2).

Each detector takes ONE truck's readings sorted by `recorded_at` and returns one bool per
reading. Detectors that look at patterns over time (stuck sensor, frozen GPS, feed gaps)
need history, so callers must pass enough lookback — at least `Thresholds.lookback` —
not just the latest reading per truck.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from src.geo import haversine_km

Row = Mapping[str, Any]  # needs truck_id, recorded_at, lat, lon, trip_status, temperature_c


class QualityFlag(StrEnum):
    CLEAN = "CLEAN"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    STALE_SENSOR = "STALE_SENSOR"
    GPS_FROZEN = "GPS_FROZEN"
    STALE_FEED = "STALE_FEED"


# Most severe first. Decides the single `data_quality_flag` when a reading has several.
SEVERITY: tuple[QualityFlag, ...] = (
    QualityFlag.OUT_OF_RANGE,
    QualityFlag.STALE_SENSOR,
    QualityFlag.GPS_FROZEN,
    QualityFlag.STALE_FEED,
)


@dataclass(frozen=True)
class Thresholds:
    sensor_min_c: float = -30.0
    sensor_max_c: float = 60.0
    stale_sensor_min_run: int = 8
    gps_freeze_min_duration: timedelta = timedelta(minutes=120)
    gps_tolerance_m: float = 25.0  # ordinary GPS jitter is not movement
    feed_max_gap: timedelta = timedelta(minutes=15)

    @property
    def lookback(self) -> timedelta:
        """Minimum history per truck a caller must fetch for every check to work."""
        return self.gps_freeze_min_duration + self.feed_max_gap


DEFAULT_THRESHOLDS = Thresholds()


def detect_out_of_range(series: Sequence[Row], t: Thresholds = DEFAULT_THRESHOLDS) -> list[bool]:
    """Physically implausible temperature: a sensor fault, not cargo data."""
    return [not t.sensor_min_c <= r["temperature_c"] <= t.sensor_max_c for r in series]


def detect_stale_sensor(series: Sequence[Row], t: Thresholds = DEFAULT_THRESHOLDS) -> list[bool]:
    """Every reading in a run of `stale_sensor_min_run`+ identical temperatures."""
    flags = [False] * len(series)
    start = 0
    for i in range(1, len(series) + 1):
        if i == len(series) or series[i]["temperature_c"] != series[start]["temperature_c"]:
            if i - start >= t.stale_sensor_min_run:
                flags[start:i] = [True] * (i - start)
            start = i
    return flags


def detect_gps_frozen(series: Sequence[Row], t: Thresholds = DEFAULT_THRESHOLDS) -> list[bool]:
    """Every reading in a run where the truck is `in_transit` but has not moved beyond
    `gps_tolerance_m` for `gps_freeze_min_duration` or longer. Parked trucks are exempt."""
    flags = [False] * len(series)
    start = 0
    for i in range(1, len(series) + 1):
        if i == len(series) or not _same_spot_in_transit(series[start], series[i], t):
            span = series[i - 1]["recorded_at"] - series[start]["recorded_at"]
            if series[start]["trip_status"] == "in_transit" and span >= t.gps_freeze_min_duration:
                flags[start:i] = [True] * (i - start)
            start = i
    return flags


def detect_stale_feed(
    series: Sequence[Row], as_of: datetime, t: Thresholds = DEFAULT_THRESHOLDS
) -> list[bool]:
    """A reading that follows a gap longer than `feed_max_gap`, and the latest reading
    if it is older than `feed_max_gap` at `as_of`."""
    flags = [
        i > 0 and r["recorded_at"] - series[i - 1]["recorded_at"] > t.feed_max_gap
        for i, r in enumerate(series)
    ]
    if series and as_of - series[-1]["recorded_at"] > t.feed_max_gap:
        flags[-1] = True
    return flags


def apply_quality_checks(
    rows: Sequence[Row], as_of: datetime, t: Thresholds = DEFAULT_THRESHOLDS
) -> list[dict[str, Any]]:
    """Return copies of `rows`, in their original order, each with two added keys:

    data_quality_flags  every flag raised, most severe first (empty list when clean)
    data_quality_flag   the single most severe flag, or CLEAN
    """
    by_truck: dict[str, list[int]] = defaultdict(list)
    for idx, r in enumerate(rows):
        by_truck[r["truck_id"]].append(idx)

    raised: list[set[QualityFlag]] = [set() for _ in rows]
    for indices in by_truck.values():
        indices.sort(key=lambda i: rows[i]["recorded_at"])
        series = [rows[i] for i in indices]
        checks = {
            QualityFlag.OUT_OF_RANGE: detect_out_of_range(series, t),
            QualityFlag.STALE_SENSOR: detect_stale_sensor(series, t),
            QualityFlag.GPS_FROZEN: detect_gps_frozen(series, t),
            QualityFlag.STALE_FEED: detect_stale_feed(series, as_of, t),
        }
        for flag, hits in checks.items():
            for pos, hit in enumerate(hits):
                if hit:
                    raised[indices[pos]].add(flag)

    out = []
    for r, flags in zip(rows, raised, strict=True):
        ordered = [f for f in SEVERITY if f in flags]
        out.append(
            {
                **r,
                "data_quality_flags": [str(f) for f in ordered],
                "data_quality_flag": str(ordered[0] if ordered else QualityFlag.CLEAN),
            }
        )
    return out


def _same_spot_in_transit(anchor: Row, r: Row, t: Thresholds) -> bool:
    if anchor["trip_status"] != "in_transit" or r["trip_status"] != "in_transit":
        return False
    moved_km = haversine_km((anchor["lat"], anchor["lon"]), (r["lat"], r["lon"]))
    return moved_km * 1000 <= t.gps_tolerance_m
