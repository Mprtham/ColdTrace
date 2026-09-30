"""Pure formatting helpers for the UI and CLI — no Streamlit imports, so they are unit-tested."""

from __future__ import annotations

import json
from typing import Any

FLAG_MEANING = {
    "OUT_OF_RANGE": "sensor reading outside −30..60 °C — a sensor fault, not cargo data",
    "STALE_SENSOR": "same temperature for 8+ readings — sensor may be stuck",
    "GPS_FROZEN": "position unchanged for 2 h while in transit",
    "STALE_FEED": "gap of more than 15 min in the feed",
}


def summarise_call(call: dict[str, Any]) -> str:
    """One line per tool call for evidence headers."""
    if call.get("error"):
        return f"error — {call['error']}"
    out = call.get("output") or {}
    if "readings" in out:
        flags = ", ".join(f"{k}×{v}" for k, v in (call.get("quality_flags") or {}).items())
        n = len(out["readings"])
        return f"{n} reading{'s' if n != 1 else ''} · quality: {flags or 'all CLEAN'}"
    if "waypoints" in out:
        hazards = len(out.get("hazards") or [])
        return f"{out['route_km']} km · max {out['max_ambient_c']} °C · {hazards} hazard(s)"
    if "results" in out:
        return " · ".join(r["citation"] for r in out["results"]) or "no rules in force"
    return json.dumps(out)[:120]


def readings_table(call: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows for the evidence table view (the accessible twin of any chart)."""
    rows = []
    for r in (call.get("output") or {}).get("readings", []):
        row = {
            "Truck": r["truck_id"],
            "Shipment": r["shipment_id"],
            "Cargo": r["cargo_type"],
            "Recorded (UTC)": r["recorded_at"][:16].replace("T", " "),
            "Temp °C": r["temperature_c"],
            "Safe range °C": f"{r['min_temp_c']:g} to {r['max_temp_c']:g}",
            "In range": "yes" if r["temp_in_range"] else "NO",
            "Delay p": r["delay_probability"],
            "Risk": r["risk_level"],
            "Data quality": " + ".join(r["data_quality_flags"]) or "CLEAN",
        }
        if r.get("distance_km") is not None:
            row["Distance km"] = r["distance_km"]
        rows.append(row)
    return rows


def cost_usd(
    tokens_in: int | None,
    tokens_out: int | None,
    price_in_per_mtok: float | None,
    price_out_per_mtok: float | None,
) -> float | None:
    """Cost of one query, or None when pricing is not configured."""
    if price_in_per_mtok is None or price_out_per_mtok is None:
        return None
    return ((tokens_in or 0) * price_in_per_mtok + (tokens_out or 0) * price_out_per_mtok) / 1e6


def format_cost(cost: float | None, provider: str) -> str:
    if cost is not None:
        return f"${cost:.4f}"
    if provider == "ollama":
        return "$0 (local model)"
    return "pricing not set"


def flag_labels(flags: list[str]) -> list[str]:
    """Icon + name + meaning: a quality flag is never conveyed by color alone."""
    return [f"⚠ {f} — {FLAG_MEANING.get(f, 'data quality flag')}" for f in flags]
