"""Generate the synthetic ColdTrace fleet: trucks, telemetry, and a ground-truth fault manifest.

Deterministic: the same --seed always produces byte-identical CSVs, so anyone who clones
the repo can verify that the data quality gate catches exactly the faults in faults.csv.

    uv run python -m scripts.generate_data

Outputs (data/synthetic/):
    trucks.csv     one row per vehicle
    telemetry.csv  one row per sensor reading, every 10 minutes
    faults.csv     every injected fault and real temperature excursion (ground truth)
"""

from __future__ import annotations

import argparse
import csv
import random
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.geo import haversine_km

SEED = 42
N_TRUCKS = 50
HOURS = 72
STEP = timedelta(minutes=10)
END_AT = datetime(2026, 10, 1, tzinfo=UTC)
SPEED_KMH = 75.0
DWELL_STEPS = 12  # 2 h parked at a depot between legs
OUT_DIR = Path("data/synthetic")

# Safe range (min_temp_c, max_temp_c) per cargo type — docs/DESIGN.md §4.1.
CARGO_RANGES: dict[str, tuple[float, float]] = {
    "fresh_perishables": (0.0, 4.0),
    "frozen": (-25.0, -18.0),
    "vaccines": (2.0, 8.0),
    "insulin": (2.0, 8.0),
}

DEPOTS: dict[str, tuple[float, float]] = {
    "Los Angeles": (34.0522, -118.2437),
    "San Diego": (32.7157, -117.1611),
    "Phoenix": (33.4484, -112.0740),
    "Las Vegas": (36.1699, -115.1398),
    "San Francisco": (37.7749, -122.4194),
    "Sacramento": (38.5816, -121.4944),
    "Fresno": (36.7378, -119.7871),
    "Salt Lake City": (40.7608, -111.8910),
}

# Fault sizes. Each is comfortably past the detection threshold in docs/DESIGN.md §3.2 B.
STUCK_READINGS = 10  # 100 min of one repeated value (threshold: 8+ consecutive)
GPS_FREEZE_READINGS = 14  # 140 min at one coordinate while in transit (threshold: 120 min)
FEED_GAP_READINGS = 10  # 100 min with no readings mid-route (spec: 90+ min)
DROPOUT_READINGS = 6  # feed stops 60 min before END_AT (threshold: 15 min)
EXCURSION_READINGS = 36  # 6 h real warm-up and recovery — NOT a sensor fault
OUT_OF_RANGE_VALUES = (85.0, -40.0, 71.5, -35.2)

N_FAULTS = {
    "STALE_SENSOR": 3,
    "OUT_OF_RANGE": 3,
    "GPS_FROZEN": 3,
    "STALE_FEED_GAP": 3,
    "STALE_FEED_DROPOUT": 2,
    "TEMP_EXCURSION": 4,
}
N_HIGH_RISK = 6


@dataclass
class Truck:
    truck_id: str
    plate_number: str
    cargo_type: str
    min_temp_c: float
    max_temp_c: float
    home_depot: str
    created_at: str


@dataclass
class Reading:
    truck_id: str
    shipment_id: str
    recorded_at: datetime
    ingested_at: datetime
    lat: float
    lon: float
    trip_status: str  # in_transit | at_depot
    temperature_c: float
    cargo_condition_code: str
    delay_probability: float
    route_risk_index: float


@dataclass
class Fault:
    truck_id: str
    fault_type: str
    start_at: datetime
    end_at: datetime
    n_readings: int
    detail: str


@dataclass
class Dataset:
    trucks: list[Truck]
    readings: list[Reading]
    faults: list[Fault]


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _simulate_truck(
    rng: random.Random, truck: Truck, dest: str, start: datetime, steps: int, base_risk: float
) -> list[Reading]:
    home, away = DEPOTS[truck.home_depot], DEPOTS[dest]
    leg_km = haversine_km(home, away)
    km_per_step = SPEED_KMH * STEP.total_seconds() / 3600
    progress = rng.uniform(0, leg_km)
    outbound = rng.random() < 0.5
    dwell = 0
    leg = 1
    setpoint = (truck.min_temp_c + truck.max_temp_c) / 2
    drift = 0.0
    num = truck.truck_id.split("-")[1]

    readings = []
    for i in range(steps):
        if dwell > 0:
            status = "at_depot"
            dwell -= 1
            if dwell == 0:
                outbound, progress, leg = not outbound, 0.0, leg + 1
        else:
            status = "in_transit"
            progress = min(leg_km, progress + km_per_step)
            if progress >= leg_km:
                dwell = DWELL_STEPS

        origin, target = (home, away) if outbound else (away, home)
        f = progress / leg_km
        lat = origin[0] + (target[0] - origin[0]) * f + rng.gauss(0, 0.0001)
        lon = origin[1] + (target[1] - origin[1]) * f + rng.gauss(0, 0.0001)

        drift = 0.8 * drift + rng.gauss(0, 0.3)
        recorded = start + i * STEP
        readings.append(
            Reading(
                truck_id=truck.truck_id,
                shipment_id=f"SHP-{num}-{leg:02d}",
                recorded_at=recorded,
                ingested_at=recorded + timedelta(seconds=rng.randint(2, 45)),
                lat=round(lat, 5),
                lon=round(lon, 5),
                trip_status=status,
                temperature_c=round(setpoint + drift, 1),
                cargo_condition_code="OK",  # recomputed after fault injection
                delay_probability=round(_clamp(base_risk + rng.gauss(0, 0.03), 0, 1), 3),
                route_risk_index=round(_clamp(base_risk * 8 + rng.gauss(0, 0.5), 0, 10), 2),
            )
        )
    return readings


def _find_window(rng: random.Random, readings: list[Reading], length: int, in_transit: bool) -> int:
    """Random start index of `length` readings away from the series edges."""
    for _ in range(10_000):
        s = rng.randrange(6, len(readings) - length - 6)
        window = readings[s : s + length]
        if not in_transit or all(r.trip_status == "in_transit" for r in window):
            return s
    raise RuntimeError(f"no {length}-reading window found for {readings[0].truck_id}")


def _condition_code(temp: float, lo: float, hi: float) -> str:
    # What the truck's onboard system reports. It trusts its sensor, so a faulty
    # 85 °C reading shows up as CRIT — catching that is the quality gate's job.
    if lo <= temp <= hi:
        return "OK"
    return "WARN" if min(abs(temp - lo), abs(temp - hi)) <= 1.0 else "CRIT"


def generate(seed: int = SEED, n_trucks: int = N_TRUCKS, hours: int = HOURS) -> Dataset:
    rng = random.Random(seed)
    steps = hours * 3600 // int(STEP.total_seconds())
    start = END_AT - steps * STEP + STEP  # last reading lands at END_AT
    cargo_types = list(CARGO_RANGES)
    depot_names = list(DEPOTS)

    trucks = []
    for i in range(1, n_trucks + 1):
        cargo = cargo_types[(i - 1) % len(cargo_types)]
        lo, hi = CARGO_RANGES[cargo]
        trucks.append(
            Truck(
                truck_id=f"TRK-{i:03d}",
                plate_number=f"CT-{rng.randint(10000, 99999)}",
                cargo_type=cargo,
                min_temp_c=lo,
                max_temp_c=hi,
                home_depot=rng.choice(depot_names),
                created_at=(END_AT - timedelta(days=90)).isoformat(),
            )
        )

    high_risk = set(rng.sample([t.truck_id for t in trucks], N_HIGH_RISK))
    series: dict[str, list[Reading]] = {}
    for t in trucks:
        dest = rng.choice([d for d in depot_names if d != t.home_depot])
        risk = rng.uniform(0.65, 0.85) if t.truck_id in high_risk else rng.uniform(0.05, 0.45)
        series[t.truck_id] = _simulate_truck(rng, t, dest, start, steps, risk)

    # Each fault goes on its own truck so every truck has at most one known cause.
    victims = rng.sample([t.truck_id for t in trucks], sum(N_FAULTS.values()))
    faults: list[Fault] = []
    for fault_type, count in N_FAULTS.items():
        for _ in range(count):
            tid = victims.pop()
            faults.append(_inject(rng, fault_type, series[tid]))

    by_id = {t.truck_id: t for t in trucks}
    readings = [r for tid in sorted(series) for r in series[tid]]
    for r in readings:
        t = by_id[r.truck_id]
        r.cargo_condition_code = _condition_code(r.temperature_c, t.min_temp_c, t.max_temp_c)

    faults.sort(key=lambda f: (f.truck_id, f.start_at))
    return Dataset(trucks=trucks, readings=readings, faults=faults)


def _inject(rng: random.Random, fault_type: str, rs: list[Reading]) -> Fault:
    tid = rs[0].truck_id

    if fault_type == "STALE_SENSOR":
        s = _find_window(rng, rs, STUCK_READINGS, in_transit=True)
        stuck = rs[s].temperature_c
        for r in rs[s : s + STUCK_READINGS]:
            r.temperature_c = stuck
        return Fault(
            tid,
            fault_type,
            rs[s].recorded_at,
            rs[s + STUCK_READINGS - 1].recorded_at,
            STUCK_READINGS,
            f"temperature stuck at {stuck} while GPS keeps moving",
        )

    if fault_type == "OUT_OF_RANGE":
        s = _find_window(rng, rs, 1, in_transit=False)
        rs[s].temperature_c = rng.choice(OUT_OF_RANGE_VALUES)
        return Fault(
            tid,
            fault_type,
            rs[s].recorded_at,
            rs[s].recorded_at,
            1,
            f"single reading of {rs[s].temperature_c} °C (valid sensor range -30..60)",
        )

    if fault_type == "GPS_FROZEN":
        s = _find_window(rng, rs, GPS_FREEZE_READINGS, in_transit=True)
        lat, lon = rs[s].lat, rs[s].lon
        for r in rs[s : s + GPS_FREEZE_READINGS]:
            r.lat, r.lon = lat, lon
        return Fault(
            tid,
            fault_type,
            rs[s].recorded_at,
            rs[s + GPS_FREEZE_READINGS - 1].recorded_at,
            GPS_FREEZE_READINGS,
            f"GPS frozen at ({lat}, {lon}) for 140 min while trip_status=in_transit",
        )

    if fault_type == "STALE_FEED_GAP":
        s = _find_window(rng, rs, FEED_GAP_READINGS, in_transit=True)
        gone = rs[s : s + FEED_GAP_READINGS]
        del rs[s : s + FEED_GAP_READINGS]
        return Fault(
            tid,
            "STALE_FEED",
            gone[0].recorded_at,
            gone[-1].recorded_at,
            FEED_GAP_READINGS,
            "no readings for 100 min mid-route, feed then resumes",
        )

    if fault_type == "STALE_FEED_DROPOUT":
        gone = rs[-DROPOUT_READINGS:]
        del rs[-DROPOUT_READINGS:]
        return Fault(
            tid,
            "STALE_FEED",
            gone[0].recorded_at,
            gone[-1].recorded_at,
            DROPOUT_READINGS,
            "feed stops; latest reading is 60 min old at END_AT",
        )

    if fault_type == "TEMP_EXCURSION":
        s = _find_window(rng, rs, EXCURSION_READINGS, in_transit=False)
        half = EXCURSION_READINGS // 2
        for k, r in enumerate(rs[s : s + EXCURSION_READINGS]):
            rise = k + 1 if k < half else EXCURSION_READINGS - k
            r.temperature_c = round(r.temperature_c + 0.25 * rise, 1)
        return Fault(
            tid,
            fault_type,
            rs[s].recorded_at,
            rs[s + EXCURSION_READINGS - 1].recorded_at,
            EXCURSION_READINGS,
            "real cargo warm-up peaking +4.5 °C; data is valid, expected flag CLEAN",
        )

    raise ValueError(fault_type)


def _write_csv(path: Path, rows: list[Truck] | list[Reading] | list[Fault]) -> None:
    cols = [f.name for f in fields(rows[0])]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: v.isoformat() if isinstance(v, datetime) else v for k, v in asdict(row).items()}
            )


def write(dataset: Dataset, out_dir: Path = OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "trucks.csv", dataset.trucks)
    _write_csv(out_dir / "telemetry.csv", dataset.readings)
    _write_csv(out_dir / "faults.csv", dataset.faults)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--trucks", type=int, default=N_TRUCKS)
    parser.add_argument("--hours", type=int, default=HOURS)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    args = parser.parse_args()

    ds = generate(seed=args.seed, n_trucks=args.trucks, hours=args.hours)
    write(ds, args.out)
    print(
        f"{len(ds.trucks)} trucks, {len(ds.readings)} readings, {len(ds.faults)} faults "
        f"-> {args.out}/"
    )


if __name__ == "__main__":
    main()
