"""Route weather — docs/DESIGN.md §6, fixing gap #5 of the reference project.

The reference tool reduced "corridor conditions" to `8.5 if wind > 10 else 2.5` at one
point. This samples current conditions at evenly spaced waypoints along the straight
line between two points (one Open-Meteo request, no API key) and reports named hazards
with where they occur. It is still point-in-time current weather, not a forecast along
the truck's ETA — a stated limitation (docs/DESIGN.md §8).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from src.geo import haversine_km

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m,weather_code"
MIN_WAYPOINTS, MAX_WAYPOINTS = 2, 10

# Hazard thresholds. Heat matters because reefer units lose margin in high ambient heat.
HEAT_C = 32.0
FREEZE_C = -10.0
GUST_KMH = 60.0

# WMO weather codes used by Open-Meteo.
WEATHER_CODES: dict[int, str] = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}  # fmt: skip
HAZARD_CODES = {45, 48, 56, 57, 65, 66, 67, 73, 75, 82, 85, 86, 95, 96, 99}


class WeatherUnavailable(RuntimeError):
    """The weather service could not be reached or returned something unusable."""


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    km_from_start: float
    temperature_c: float
    precipitation_mm: float
    wind_speed_kmh: float
    wind_gusts_kmh: float
    weather_code: int
    conditions: str


@dataclass(frozen=True)
class RouteConditions:
    tool_name: str
    route_km: float
    waypoints: list[Waypoint]
    max_ambient_c: float
    max_gust_kmh: float
    hazards: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def fetch_route_conditions(
    from_lat: float,
    from_lon: float,
    to_lat: float,
    to_lon: float,
    waypoints: int = 5,
    *,
    client: httpx.Client | None = None,
) -> RouteConditions:
    """Current weather at `waypoints` evenly spaced points from start to destination."""
    for lat, lon in ((from_lat, from_lon), (to_lat, to_lon)):
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError(f"({lat}, {lon}) is not a valid coordinate")
    if not MIN_WAYPOINTS <= waypoints <= MAX_WAYPOINTS:
        raise ValueError(f"waypoints must be in [{MIN_WAYPOINTS}, {MAX_WAYPOINTS}]")

    points = interpolate((from_lat, from_lon), (to_lat, to_lon), waypoints)
    current = _fetch_current(points, client)
    route_km = haversine_km((from_lat, from_lon), (to_lat, to_lon))

    wps = []
    for i, ((lat, lon), c) in enumerate(zip(points, current, strict=True)):
        code = int(c["weather_code"])
        wps.append(
            Waypoint(
                lat=lat,
                lon=lon,
                km_from_start=round(route_km * i / (waypoints - 1), 1),
                temperature_c=float(c["temperature_2m"]),
                precipitation_mm=float(c["precipitation"]),
                wind_speed_kmh=float(c["wind_speed_10m"]),
                wind_gusts_kmh=float(c["wind_gusts_10m"]),
                weather_code=code,
                conditions=WEATHER_CODES.get(code, f"code {code}"),
            )
        )
    return RouteConditions(
        tool_name="fetch_route_conditions",
        route_km=round(route_km, 1),
        waypoints=wps,
        max_ambient_c=max(w.temperature_c for w in wps),
        max_gust_kmh=max(w.wind_gusts_kmh for w in wps),
        hazards=hazards(wps),
    )


def interpolate(
    start: tuple[float, float], end: tuple[float, float], n: int
) -> list[tuple[float, float]]:
    """n points from start to end inclusive, linear in lat/lon (fine at trucking scale)."""
    return [
        (
            round(start[0] + (end[0] - start[0]) * i / (n - 1), 4),
            round(start[1] + (end[1] - start[1]) * i / (n - 1), 4),
        )
        for i in range(n)
    ]


def hazards(wps: list[Waypoint]) -> list[str]:
    found = []
    for w in wps:
        at = f"at km {w.km_from_start:g}"
        if w.temperature_c >= HEAT_C:
            found.append(f"HEAT: {w.temperature_c:g}°C ambient {at} — reefer under extra load")
        if w.temperature_c <= FREEZE_C:
            found.append(f"FREEZE: {w.temperature_c:g}°C ambient {at}")
        if w.wind_gusts_kmh >= GUST_KMH:
            found.append(f"WIND: gusts {w.wind_gusts_kmh:g} km/h {at}")
        if w.weather_code in HAZARD_CODES:
            found.append(f"WEATHER: {w.conditions} {at}")
    return found


def _fetch_current(
    points: list[tuple[float, float]], client: httpx.Client | None
) -> list[dict[str, Any]]:
    params = {
        "latitude": ",".join(str(p[0]) for p in points),
        "longitude": ",".join(str(p[1]) for p in points),
        "current": CURRENT_FIELDS,
        "wind_speed_unit": "kmh",
        "timezone": "UTC",
    }
    try:
        if client is not None:
            response = client.get(OPEN_METEO_URL, params=params)
        else:
            with httpx.Client(timeout=10.0) as own:
                response = own.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherUnavailable(f"weather service unavailable: {exc}") from exc

    locations = body if isinstance(body, list) else [body]  # one point -> object, many -> list
    try:
        current = [loc["current"] for loc in locations]
    except (KeyError, TypeError) as exc:
        raise WeatherUnavailable("weather service returned an unexpected shape") from exc
    if len(current) != len(points):
        raise WeatherUnavailable(f"asked for {len(points)} points, got {len(current)}")
    return current
