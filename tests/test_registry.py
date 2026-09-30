"""The compact tool views the model reads: trimming must never hide the readings that matter."""

from datetime import UTC, datetime, timedelta

from src.tools.registry import HISTORY_ROW_LIMIT, _telemetry
from src.tools.telemetry import TelemetryResult, TruckReading

T0 = datetime(2026, 9, 28, 18, 0, tzinfo=UTC)


def _reading(i: int, flags: list[str] | None = None, temp: float = -21.0) -> TruckReading:
    return TruckReading(
        truck_id="TRK-026", shipment_id="SHP-026-04", cargo_type="frozen",
        recorded_at=T0 + timedelta(minutes=10 * i), lat=36.0, lon=-120.0,
        trip_status="in_transit", temperature_c=temp, min_temp_c=-25.0, max_temp_c=-18.0,
        temp_in_range=-25.0 <= temp <= -18.0, cargo_condition_code="OK",
        delay_probability=0.2, route_risk_index=1.0, risk_level="low",
        data_quality_flag=(flags or ["CLEAN"])[0], data_quality_flags=flags or [],
    )  # fmt: skip


def _history(readings: list[TruckReading]) -> dict:  # type: ignore[type-arg]
    result = TelemetryResult("get_temperature_history", T0, readings)
    return _telemetry(result, limit=HISTORY_ROW_LIMIT, problems_first=False).for_llm


def test_trimmed_history_keeps_early_flagged_readings() -> None:
    # 73 readings, the stuck run early on — exactly the case a 7B model missed when
    # only the most recent 48 were shown.
    readings = [_reading(i, ["STALE_SENSOR"] if 10 <= i < 20 else None) for i in range(73)]
    view = _history(readings)
    shown = view["readings"]
    assert len(shown) == HISTORY_ROW_LIMIT
    assert sum(r["data_quality_flag"] == "STALE_SENSOR" for r in shown) == 10
    assert shown == sorted(shown, key=lambda r: r["recorded_at"])
    assert shown[-1]["recorded_at"] == readings[-1].recorded_at.strftime("%Y-%m-%d %H:%M UTC")
    assert view["count"] == 73
    assert "every flagged or out-of-range reading" in view["note"]


def test_trimmed_history_keeps_out_of_range_readings() -> None:
    readings = [_reading(i, temp=-15.0 if i == 3 else -21.0) for i in range(60)]
    shown = _history(readings)["readings"]
    assert any(r["temperature_c"] == -15.0 for r in shown)


def test_short_history_untouched() -> None:
    view = _history([_reading(i) for i in range(5)])
    assert len(view["readings"]) == 5
    assert "note" not in view
