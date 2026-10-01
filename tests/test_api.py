"""HTTP API against a real PostgreSQL, with the scripted LLM and fake tools from the
orchestrator tests — the full request → graph → audit row → response path, minus the
model and network."""

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from api.main import app, get_conn, get_llm, get_tools
from src.db import role_url
from tests.test_orchestrator import ScriptedLLM, call, done, fake_tools, route, verdict

Conn = psycopg.Connection[dict[str, Any]]


@pytest.fixture(scope="module")
def db(fresh_db: Callable[[], str]) -> str:
    return fresh_db()


@pytest.fixture
def conn(db: str) -> Iterator[Conn]:
    with psycopg.connect(
        role_url(db, "coldtrace_agent", None), row_factory=dict_row, autocommit=True
    ) as c:
        yield c


@pytest.fixture
def client(conn: Conn) -> Iterator[TestClient]:
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_tools] = lambda: fake_tools(flags={"STALE_FEED": 1})
    yield TestClient(app)
    app.dependency_overrides.clear()


def _script(*replies: Any) -> None:
    app.dependency_overrides[get_llm] = lambda: ScriptedLLM(*replies)


def _ask(client: TestClient, question: str = "Trucks near LA?", **body: Any) -> dict[str, Any]:
    _script(
        route("temperature_alarm"),
        call("get_truck_telemetry", {"lat": 34.05, "lon": -118.24, "radius_km": 50}),
        call("search_sop", {"query": "breach", "cargo_type": "fresh_perishables"}, "c2"),
        done(),
        verdict(),
    )
    response = client.post("/query", json={"question": question, **body})
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()
    return data


# --- POST /query -----------------------------------------------------------------------


def test_query_returns_evidence_verdict_and_audit_ids(client: TestClient) -> None:
    data = _ask(client)
    assert data["intent"] == "temperature_alarm"
    assert [e["tool_name"] for e in data["evidence"]] == ["get_truck_telemetry", "search_sop"]
    assert data["verdict"].startswith("Contact the driver of TRK-009")
    assert data["sop_cited"] == "SOP v3.0 §1.1"
    assert data["confidence"] == "medium"  # flagged evidence caps it
    assert data["data_quality_flags"] == ["STALE_FEED"]
    assert data["token_counts"] == {"input": 1200, "output": 115}
    assert data["log_id"] >= 1
    assert len(data["row_hash"]) == 64


def test_query_keeps_given_session(client: TestClient) -> None:
    session = "7d1f0c3e-1c2b-4c55-9a51-8a9f3a1d2e44"
    assert _ask(client, session_id=session)["session_id"] == session


@pytest.mark.parametrize("body", [{}, {"question": ""}, {"question": "x", "session_id": "nope"}])
def test_query_rejects_bad_body(client: TestClient, body: dict[str, Any]) -> None:
    assert client.post("/query", json=body).status_code == 422


def test_query_returns_503_when_agent_fails(client: TestClient) -> None:
    _script()  # no replies: the first LLM call raises
    response = client.post("/query", json={"question": "anything"})
    assert response.status_code == 503
    assert response.json()["detail"].startswith("agent unavailable")


# --- POST /decision --------------------------------------------------------------------


def test_accept_then_override_appends_rows(client: TestClient) -> None:
    log_id = _ask(client)["log_id"]
    accepted = client.post("/decision", json={"log_id": log_id, "decision": "accepted"})
    assert accepted.status_code == 200
    assert accepted.json()["amends_log_id"] == log_id

    overridden = client.post(
        "/decision",
        json={"log_id": log_id, "decision": "overridden", "reason": "Driver reports door ajar."},
    )
    assert overridden.status_code == 200
    row = client.get(f"/audit/{log_id}").json()
    assert row["current_decision"] == "overridden"
    assert [d["human_decision"] for d in row["decisions"]] == ["accepted", "overridden"]
    assert row["human_decision"] == "pending"  # the agent row itself never changes


def test_decision_takes_session_from_amended_row(client: TestClient, conn: Conn) -> None:
    asked = _ask(client)
    decided = client.post("/decision", json={"log_id": asked["log_id"], "decision": "accepted"})
    row = conn.execute(
        "SELECT session_id FROM audit_log WHERE log_id = %s", (decided.json()["log_id"],)
    ).fetchone()
    assert row is not None and str(row["session_id"]) == asked["session_id"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"log_id": 999_999, "decision": "accepted"}, 404),
        ({"log_id": 1, "decision": "overridden"}, 422),  # no reason
        ({"log_id": 1, "decision": "maybe"}, 422),
        ({"log_id": 0, "decision": "accepted"}, 422),
    ],
)
def test_decision_errors(client: TestClient, body: dict[str, Any], status: int) -> None:
    _ask(client)  # make sure log_id 1 exists
    assert client.post("/decision", json=body).status_code == status


def test_decision_on_a_decision_row_rejected(client: TestClient) -> None:
    log_id = _ask(client)["log_id"]
    decided = client.post("/decision", json={"log_id": log_id, "decision": "accepted"}).json()
    again = client.post("/decision", json={"log_id": decided["log_id"], "decision": "accepted"})
    assert again.status_code == 422
    assert "itself a decision" in again.json()["detail"]


# --- GET /audit ------------------------------------------------------------------------


def test_audit_lists_agent_rows_newest_first_without_outputs(client: TestClient) -> None:
    first, second = _ask(client, "first?")["log_id"], _ask(client, "second?")["log_id"]
    items = client.get("/audit", params={"limit": 2}).json()["items"]
    assert [i["log_id"] for i in items] == [second, first]
    assert items[0]["tools"][0] == {
        "tool_name": "get_truck_telemetry",
        "input": {"lat": 34.05, "lon": -118.24, "radius_km": 50},
        "quality_flags": {"STALE_FEED": 1},
        "error": None,
    }
    assert "output" not in items[0]["tools"][0]


def test_audit_filters(client: TestClient) -> None:
    session = "0b8a6f7e-3d2c-4a1b-9e8f-7a6b5c4d3e2f"
    mine = _ask(client, "mine", session_id=session)["log_id"]
    client.post("/decision", json={"log_id": mine, "decision": "overridden", "reason": "no"})

    by_session = client.get("/audit", params={"session_id": session}).json()["items"]
    assert [i["log_id"] for i in by_session] == [mine]
    assert by_session[0]["current_decision"] == "overridden"
    assert by_session[0]["decision_reason"] == "no"

    overridden = client.get("/audit", params={"decision": "overridden"}).json()["items"]
    assert mine in {i["log_id"] for i in overridden}
    assert all(i["current_decision"] == "overridden" for i in overridden)

    weather = client.get("/audit", params={"tool_name": "fetch_route_conditions"}).json()
    assert weather["items"] == []
    telemetry = client.get("/audit", params={"tool_name": "get_truck_telemetry"}).json()
    assert mine in {i["log_id"] for i in telemetry["items"]}


def test_audit_keyset_pagination_covers_every_row_once(client: TestClient) -> None:
    for i in range(5):
        _ask(client, f"page {i}")
    seen: list[int] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/audit", params=params).json()
        seen += [i["log_id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen, reverse=True)
    assert len(seen) == len(set(seen))
    total = client.get("/audit", params={"limit": 100}).json()["items"]
    assert len(seen) == len(total)


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"tool_name": "drop_table"}, {"decision": "x"}]
)
def test_audit_rejects_bad_params(client: TestClient, params: dict[str, Any]) -> None:
    assert client.get("/audit", params=params).status_code == 422


def test_audit_row_404(client: TestClient) -> None:
    assert client.get("/audit/999999").status_code == 404


def test_verify_chain_endpoint(client: TestClient) -> None:
    _ask(client)
    report = client.get("/audit/verify").json()
    assert report["ok"] is True
    assert report["rows_checked"] >= 1


def test_health(client: TestClient) -> None:
    assert client.get("/health").json()["database"] == "ok"


# --- API key (deploy) ------------------------------------------------------------------


@pytest.fixture
def locked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from api import main
    from src.config import Settings

    keyed = Settings(_env_file=None, api_key="s3cret-key")  # type: ignore[call-arg]
    monkeypatch.setattr(main, "get_settings", lambda: keyed)
    return client


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", "/query"), ("post", "/decision"), ("get", "/audit"), ("get", "/audit/verify"),
     ("get", "/audit/1")],
)  # fmt: skip
def test_routes_need_api_key_when_set(locked: TestClient, method: str, path: str) -> None:
    assert getattr(locked, method)(path).status_code == 401
    wrong = getattr(locked, method)(path, headers={"X-API-Key": "nope"})
    assert wrong.status_code == 401


def test_right_key_passes_and_health_stays_open(locked: TestClient) -> None:
    assert locked.get("/audit", headers={"X-API-Key": "s3cret-key"}).status_code == 200
    assert locked.get("/health").status_code == 200
