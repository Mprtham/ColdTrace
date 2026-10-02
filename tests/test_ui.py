"""UI: the HTTP client (mocked transport), the pure view helpers, the chart spec, and the
Streamlit page itself (AppTest, against a fake API client — no server, no model)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from ui import client as api
from ui.charts import WARNING, temperature_chart
from ui.client import ApiError, ColdTraceClient
from ui.view import cost_usd, flag_labels, format_cost, readings_table, summarise_call

# --- Fixtures: one realistic /query response -------------------------------------------


def _reading(minute: int, temp: float, flags: list[str] | None = None) -> dict[str, Any]:
    return {
        "truck_id": "TRK-026",
        "shipment_id": "SHP-026-04",
        "cargo_type": "frozen",
        "recorded_at": f"2026-09-30T11:{minute:02d}:00+00:00",
        "temperature_c": temp,
        "min_temp_c": -25.0,
        "max_temp_c": -18.0,
        "temp_in_range": -25.0 <= temp <= -18.0,
        "delay_probability": 0.21,
        "risk_level": "low",
        "data_quality_flag": (flags or ["CLEAN"])[0],
        "data_quality_flags": flags or [],
        "distance_km": None,
    }


READINGS = [_reading(m, -21.2, ["STALE_SENSOR"] if m >= 20 else None) for m in range(0, 60, 10)]
QUERY_RESPONSE: dict[str, Any] = {
    "session_id": "7d1f0c3e-1c2b-4c55-9a51-8a9f3a1d2e44",
    "log_id": 7,
    "row_hash": "ab" * 32,
    "intent": "temperature_alarm",
    "as_of": "2026-09-30T12:10:00+00:00",
    "evidence": [
        {
            "tool_name": "get_temperature_history",
            "input": {"shipment_id": "SHP-026-04", "hours": 1},
            "output": {"tool_name": "get_temperature_history", "readings": READINGS},
            "quality_flags": {"STALE_SENSOR": 4},
            "error": None,
        },
        {
            "tool_name": "search_sop",
            "input": {"query": "frozen breach"},
            "output": {
                "tool_name": "search_sop",
                "results": [
                    {
                        "citation": "SOP v3.0 §1.2",
                        "effective_date": "2024-06-01",
                        "content": "Escalate to Tier 2 if breach exceeds 30 minutes.",
                    }
                ],
            },
            "quality_flags": {},
            "error": None,
        },
    ],
    "verdict": "Sensor on TRK-026 looks stuck; ask the driver for a manual probe reading.",
    "sop_cited": "SOP v3.0 §1.2",
    "confidence": "medium",
    "caveats": ["Evidence includes data quality flags (STALE_SENSOR)."],
    "data_quality_flags": ["STALE_SENSOR"],
    "token_counts": {"input": 4200, "output": 180},
}
AUDIT_ITEM: dict[str, Any] = {
    "log_id": 7,
    "session_id": QUERY_RESPONSE["session_id"],
    "created_at": "2026-09-30T12:11:00+00:00",
    "dispatcher_question": "Is TRK-026 ok?",
    "tools": [
        {"tool_name": "get_temperature_history", "input": {}, "quality_flags": {}, "error": None}
    ],
    "data_quality_flags": ["STALE_SENSOR"],
    "final_recommendation": "Probe it.",
    "sop_clause_cited": "SOP v3.0 §1.2",
    "token_count_in": 4200,
    "token_count_out": 180,
    "current_decision": "pending",
    "decision_reason": None,
    "decided_at": None,
    "row_hash": "ab" * 32,
}


# --- ColdTraceClient -------------------------------------------------------------------


def _client(handler: Any) -> ColdTraceClient:
    base = "http://api.test"
    return ColdTraceClient(
        base, http=httpx.Client(base_url=base, transport=httpx.MockTransport(handler))
    )


def test_client_posts_query_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=QUERY_RESPONSE)

    assert _client(handler).query("Is TRK-026 ok?", "s-1")["log_id"] == 7
    assert seen[0].url.path == "/query"
    assert json.loads(seen[0].content) == {"question": "Is TRK-026 ok?", "session_id": "s-1"}


def test_client_drops_unset_audit_params() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    _client(handler).audit(decision="overridden", limit=10)
    assert dict(seen[0].url.params) == {"decision": "overridden", "limit": "10"}


def test_client_raises_api_error_with_detail() -> None:
    client = _client(lambda r: httpx.Response(503, json={"detail": "agent unavailable: boom"}))
    with pytest.raises(ApiError) as err:
        client.query("?")
    assert (err.value.status, err.value.detail) == (503, "agent unavailable: boom")


def test_client_unreachable_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiError, match="API unreachable") as err:
        _client(handler).health()
    assert err.value.status == 0


# --- View helpers ----------------------------------------------------------------------


def test_cost_needs_both_prices() -> None:
    assert cost_usd(1_000_000, 1_000_000, 0.27, 1.10) == pytest.approx(1.37)
    assert cost_usd(4200, 180, None, 1.10) is None


@pytest.mark.parametrize(
    ("cost", "provider", "text"),
    [(0.0012345, "deepseek", "$0.0012"), (None, "ollama", "$0 (local model)"),
     (None, "deepseek", "pricing not set")],
)  # fmt: skip
def test_format_cost(cost: float | None, provider: str, text: str) -> None:
    assert format_cost(cost, provider) == text


def test_summaries() -> None:
    history, sop = QUERY_RESPONSE["evidence"]
    assert summarise_call(history) == "6 readings · quality: STALE_SENSOR×4"
    assert summarise_call(sop) == "SOP v3.0 §1.2"
    assert summarise_call({"error": "bad radius"}) == "error — bad radius"


def test_readings_table_is_the_accessible_twin() -> None:
    rows = readings_table(QUERY_RESPONSE["evidence"][0])
    assert len(rows) == 6
    assert rows[-1]["Data quality"] == "STALE_SENSOR"
    assert rows[0]["Data quality"] == "CLEAN"
    assert rows[0]["Safe range °C"] == "-25 to -18"


def test_flags_carry_icon_and_meaning() -> None:
    (label,) = flag_labels(["GPS_FROZEN"])
    assert label.startswith("⚠ GPS_FROZEN — ")


# --- Chart -----------------------------------------------------------------------------


def _layers(spec: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for layer in spec.get("layer", []):
        out += _layers(layer) if "layer" in layer else [layer]
    return out


@pytest.mark.parametrize("dark", [False, True])
def test_chart_layers_and_encoding(dark: bool) -> None:
    layers = _layers(temperature_chart(READINGS, dark=dark).to_dict())
    by_mark: dict[str, list[dict[str, Any]]] = {}
    for layer in layers:
        by_mark.setdefault(layer["mark"]["type"], []).append(layer)

    (line,) = by_mark["line"]
    assert line["mark"]["strokeWidth"] == 2
    assert line["mark"]["color"] == ("#3987e5" if dark else "#2a78d6")
    assert "legend" not in json.dumps(line["encoding"])  # one series, no legend

    (flagged,) = [lay for lay in by_mark["point"] if lay["mark"].get("shape") == "triangle-up"]
    assert flagged["mark"]["color"] == WARNING

    (hit,) = [lay for lay in by_mark["point"] if "tooltip" in lay.get("encoding", {})]
    assert "Data quality" in [t["title"] for t in hit["encoding"]["tooltip"]]
    assert hit["mark"]["size"] >= 400  # hit target bigger than the mark

    labels = [lay["encoding"]["text"] for lay in by_mark["text"]]
    assert len(labels) == 2  # safe max / safe min, directly labelled


def test_chart_pads_axis_beyond_safe_band() -> None:
    from ui.charts import temperature_frame, y_domain

    lo, hi = y_domain(temperature_frame(READINGS))
    assert lo < -25 and hi > -18


# --- The Streamlit page ----------------------------------------------------------------


class FakeClient:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.queries: list[tuple[str, str | None]] = []
        self.decisions: list[tuple[int, str, str | None]] = []

    def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise ApiError(0, "API unreachable at http://localhost:8000 (ConnectError)")
        return {"status": "ok", "database": "ok", "llm_provider": "ollama", "clock_mode": "replay"}

    def query(self, question: str, session_id: str | None = None) -> dict[str, Any]:
        self.queries.append((question, session_id))
        return QUERY_RESPONSE

    def decide(self, log_id: int, decision: str, reason: str | None = None) -> dict[str, Any]:
        self.decisions.append((log_id, decision, reason))
        return {"log_id": 8, "amends_log_id": log_id, "decision": decision, "row_hash": "cd" * 32}

    def audit(self, **_: Any) -> dict[str, Any]:
        return {"items": [AUDIT_ITEM], "next_cursor": None}

    def audit_row(self, log_id: int) -> dict[str, Any]:
        return {**AUDIT_ITEM, "tool_calls": [], "previous_row_hash": None, "decisions": []}

    def verify(self) -> dict[str, Any]:
        return {"ok": True, "rows_checked": 8, "first_broken_log_id": None, "reason": None}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(api, "make_client", lambda: client)
    return client


def _app() -> AppTest:
    app = Path(__file__).parents[1] / "ui" / "app.py"
    return AppTest.from_file(str(app), default_timeout=30).run()


def _texts(at: AppTest) -> str:
    parts = [e.value for kind in ("markdown", "caption", "warning", "info", "success", "error")
             for e in getattr(at, kind)]  # fmt: skip
    parts += [e.label for e in at.expander]
    return "\n".join(str(p) for p in parts)


def test_page_reports_api_down_and_disables_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "make_client", lambda: FakeClient(healthy=False))
    at = _app()
    assert not at.exception
    assert any("API unavailable" in e.value for e in at.sidebar.error)
    assert at.chat_input[0].disabled


def test_question_renders_evidence_and_verdict_panels(fake: FakeClient) -> None:
    at = _app()
    at.chat_input[0].set_value("Is TRK-026 ok?").run()
    assert not at.exception
    assert fake.queries == [("Is TRK-026 ok?", at.session_state.session_id)]
    text = _texts(at)
    assert "#### Evidence" in text and "#### Verdict" in text
    assert "`get_temperature_history`: 6 readings · quality: STALE_SENSOR×4" in text
    assert "Sensor on TRK-026 looks stuck" in text
    (flag,) = at.warning
    assert flag.value.startswith("STALE_SENSOR — same temperature for 8+ readings")
    assert flag.icon == "⚠️"
    assert "cost/query" in text and "audit row #7" in text


def test_accept_records_decision(fake: FakeClient) -> None:
    at = _app()
    at.chat_input[0].set_value("Is TRK-026 ok?").run()
    at.button(key="accept-0").click().run()
    assert fake.decisions == [(7, "accepted", None)]
    assert any("Accepted (audit row #8)" in e.value for e in at.info)


def test_override_requires_and_records_reason(fake: FakeClient) -> None:
    at = _app()
    at.chat_input[0].set_value("Is TRK-026 ok?").run()
    assert at.button(key="override-0").disabled
    at.text_area(key="reason-0").input("Driver probed it: -21 °C, sensor fine.").run()
    at.button(key="override-0").click().run()
    assert fake.decisions == [(7, "overridden", "Driver probed it: -21 °C, sensor fine.")]


def test_audit_tab_lists_rows_and_verifies_chain(fake: FakeClient) -> None:
    at = _app()
    assert at.dataframe[0].value["Row"].tolist() == [7]
    verify = next(b for b in at.button if b.label == "Verify hash chain")
    verify.click().run()
    assert any("Chain intact: 8 rows verified." in e.value for e in at.success)


def test_packages_import_outside_repo_root(tmp_path: Path) -> None:
    # `streamlit run ui/app.py` puts ui/ on sys.path, not the repo root; the project must
    # be installed so `import src` works from any entry point and working directory.
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-c", "import src.config, ui.view, ui.charts, api.main, scripts.ask"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={k: v for k, v in __import__("os").environ.items() if k != "PYTHONPATH"},
    )
    assert done.returncode == 0, done.stderr


def test_client_sends_api_key_header() -> None:
    assert ColdTraceClient("http://api.test", api_key="k1")._http.headers["X-API-Key"] == "k1"
    assert "X-API-Key" not in ColdTraceClient("http://api.test")._http.headers


def test_ui_password_screen(fake: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.config import Settings

    guarded = Settings(_env_file=None, ui_password="letmein")  # type: ignore[call-arg]
    monkeypatch.setattr("src.config.get_settings", lambda: guarded)
    at = _app()
    assert not at.tabs  # nothing behind the screen yet
    at.text_input[0].input("wrong").run()
    assert any("Wrong password" in e.value for e in at.error)
    at.text_input[0].input("letmein").run()
    assert len(at.tabs) == 2
