"""Route weather against a mocked Open-Meteo — no network in tests."""

from typing import Any

import httpx
import pytest

from src.tools.weather import (
    WeatherUnavailable,
    fetch_route_conditions,
    interpolate,
)

LA = (34.0522, -118.2437)
PHOENIX = (33.4484, -112.0740)


def _current(temp: float = 20.0, gusts: float = 15.0, code: int = 0) -> dict[str, Any]:
    return {
        "current": {
            "temperature_2m": temp,
            "precipitation": 0.0,
            "wind_speed_10m": 10.0,
            "wind_gusts_10m": gusts,
            "weather_code": code,
        }
    }


def _client(body: Any, status: int = 200, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_interpolate_includes_both_ends() -> None:
    pts = interpolate(LA, PHOENIX, 5)
    assert len(pts) == 5
    assert pts[0] == (round(LA[0], 4), round(LA[1], 4))
    assert pts[-1] == (round(PHOENIX[0], 4), round(PHOENIX[1], 4))


def test_one_request_for_all_waypoints() -> None:
    seen: list[httpx.Request] = []
    fetch_route_conditions(*LA, *PHOENIX, waypoints=4, client=_client([_current()] * 4, seen=seen))
    assert len(seen) == 1
    assert len(seen[0].url.params["latitude"].split(",")) == 4


def test_waypoints_carry_distance_along_route() -> None:
    result = fetch_route_conditions(*LA, *PHOENIX, waypoints=5, client=_client([_current()] * 5))
    kms = [w.km_from_start for w in result.waypoints]
    assert kms[0] == 0
    assert kms[-1] == result.route_km
    assert 550 < result.route_km < 600  # LA to Phoenix, straight line
    assert kms == sorted(kms)


def test_calm_route_has_no_hazards() -> None:
    result = fetch_route_conditions(*LA, *PHOENIX, waypoints=3, client=_client([_current()] * 3))
    assert result.hazards == []


def test_hazards_named_with_location() -> None:
    body = [_current(), _current(temp=41.0), _current(gusts=75.0, code=95)]
    result = fetch_route_conditions(*LA, *PHOENIX, waypoints=3, client=_client(body))
    assert result.max_ambient_c == 41.0
    assert result.max_gust_kmh == 75.0
    assert any(h.startswith("HEAT: 41°C") for h in result.hazards)
    assert any(h.startswith("WIND: gusts 75") for h in result.hazards)
    assert any(h.startswith("WEATHER: thunderstorm") for h in result.hazards)
    assert all("at km" in h for h in result.hazards)


def test_http_error_raises_weather_unavailable() -> None:
    with pytest.raises(WeatherUnavailable):
        fetch_route_conditions(*LA, *PHOENIX, waypoints=2, client=_client({}, status=503))


def test_wrong_number_of_points_raises() -> None:
    with pytest.raises(WeatherUnavailable, match="asked for 3"):
        fetch_route_conditions(*LA, *PHOENIX, waypoints=3, client=_client([_current()] * 2))


@pytest.mark.parametrize(
    ("args", "waypoints"), [((91, 0, *PHOENIX), 3), ((*LA, *PHOENIX), 1), ((*LA, *PHOENIX), 11)]
)
def test_rejects_bad_parameters(args: tuple[float, float, float, float], waypoints: int) -> None:
    with pytest.raises(ValueError):
        fetch_route_conditions(*args, waypoints=waypoints, client=_client([]))


def test_result_is_json_serialisable() -> None:
    import json

    result = fetch_route_conditions(*LA, *PHOENIX, waypoints=2, client=_client([_current()] * 2))
    assert json.loads(json.dumps(result.to_json()))["tool_name"] == "fetch_route_conditions"
