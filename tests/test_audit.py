"""Hash-chained audit log against a real PostgreSQL, written as coldtrace_agent."""

import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from src.audit import (
    compute_row_hash,
    current_decision,
    verify_chain,
    write_agent_row,
    write_decision,
)
from src.db import role_url

Conn = psycopg.Connection[dict[str, Any]]
SESSION = uuid.uuid4()


@pytest.fixture(scope="module")
def db(fresh_db: Callable[[], str]) -> str:
    return fresh_db()


@pytest.fixture
def agent(db: str) -> Iterator[Conn]:
    with psycopg.connect(
        role_url(db, "coldtrace_agent", None), row_factory=dict_row, autocommit=True
    ) as c:
        yield c


@pytest.fixture
def owner(db: str) -> Iterator[Conn]:
    """The schema owner — the only identity that can tamper, which is what we simulate."""
    with psycopg.connect(db, row_factory=dict_row, autocommit=True) as c:
        yield c


def _agent_row(conn: Conn, question: str = "trucks near LA?", **overrides: Any) -> int:
    fields: dict[str, Any] = {
        "session_id": SESSION,
        "dispatcher_question": question,
        "tool_calls": [
            {
                "tool_name": "get_truck_telemetry",
                "input": {"lat": 34.05, "lon": -118.24, "radius_km": 50.0},
                "output": {"readings": [{"truck_id": "TRK-009", "temperature_c": 4.7}]},
                "quality_flags": {},
            }
        ],
        "data_quality_flags": [],
        "final_recommendation": "Contact driver of TRK-009 within 5 minutes.",
        "sop_clause_cited": "SOP v3.0 §1.1",
        "token_count_in": 1200,
        "token_count_out": 150,
        **overrides,
    }
    return write_agent_row(conn, **fields).log_id


def test_first_rows_chain_and_verify(agent: Conn) -> None:
    a = _agent_row(agent)
    b = _agent_row(agent, "high risk shipments?")
    rows = {
        r["log_id"]: r
        for r in agent.execute("SELECT * FROM audit_log WHERE log_id IN (%s, %s)", (a, b))
    }
    assert rows[b]["previous_row_hash"] == rows[a]["row_hash"]
    assert compute_row_hash(rows[a]) == rows[a]["row_hash"]
    assert verify_chain(agent).ok


def test_tool_calls_are_queryable_jsonb(agent: Conn) -> None:
    _agent_row(agent)
    hits = agent.execute(
        "SELECT count(*) AS n FROM audit_log WHERE tool_calls @> %s::jsonb",
        ('[{"tool_name": "get_truck_telemetry"}]',),
    ).fetchone()
    assert hits is not None and hits["n"] >= 1


def test_decision_appends_row_and_leaves_original_untouched(agent: Conn) -> None:
    log_id = _agent_row(agent)
    before = agent.execute("SELECT * FROM audit_log WHERE log_id = %s", (log_id,)).fetchone()
    assert current_decision(agent, log_id) == "pending"

    write_decision(
        agent,
        amends_log_id=log_id,
        session_id=SESSION,
        decision="overridden",
        override_reason="Driver already at depot; cargo transferred.",
    )
    after = agent.execute("SELECT * FROM audit_log WHERE log_id = %s", (log_id,)).fetchone()
    assert after == before
    assert current_decision(agent, log_id) == "overridden"
    assert verify_chain(agent).ok


def test_latest_decision_wins(agent: Conn) -> None:
    log_id = _agent_row(agent)
    write_decision(agent, amends_log_id=log_id, session_id=SESSION, decision="overridden",
                   override_reason="first look")  # fmt: skip
    write_decision(agent, amends_log_id=log_id, session_id=SESSION, decision="accepted")
    assert current_decision(agent, log_id) == "accepted"


def test_override_without_reason_rejected(agent: Conn) -> None:
    log_id = _agent_row(agent)
    with pytest.raises(ValueError, match="reason"):
        write_decision(
            agent, amends_log_id=log_id, session_id=SESSION, decision="overridden",
            override_reason="  ",
        )  # fmt: skip


def test_decision_on_missing_or_decision_row_rejected(agent: Conn) -> None:
    with pytest.raises(LookupError):
        write_decision(agent, amends_log_id=999_999, session_id=SESSION, decision="accepted")
    log_id = _agent_row(agent)
    decision = write_decision(agent, amends_log_id=log_id, session_id=SESSION, decision="accepted")
    with pytest.raises(ValueError, match="itself a decision"):
        write_decision(
            agent, amends_log_id=decision.log_id, session_id=SESSION, decision="accepted"
        )


def test_non_json_tool_output_fails_before_insert(agent: Conn) -> None:
    count = agent.execute("SELECT count(*) AS n FROM audit_log").fetchone()
    with pytest.raises(TypeError):
        _agent_row(agent, tool_calls=[{"output": object()}])
    assert agent.execute("SELECT count(*) AS n FROM audit_log").fetchone() == count


def test_concurrent_writers_never_fork_the_chain(db: str, agent: Conn) -> None:
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with psycopg.connect(
                role_url(db, "coldtrace_agent", None), row_factory=dict_row, autocommit=True
            ) as c:
                for _ in range(5):
                    _agent_row(c, "concurrent")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    forks = agent.execute(
        "SELECT previous_row_hash, count(*) AS n FROM audit_log "
        "GROUP BY previous_row_hash HAVING count(*) > 1"
    ).fetchall()
    assert forks == []
    assert verify_chain(agent).ok


# --- Tamper evidence (as the schema owner; the agent cannot UPDATE or DELETE) -----------


def test_edited_row_detected(agent: Conn, owner: Conn) -> None:
    target = _agent_row(agent)
    _agent_row(agent)
    original = owner.execute(
        "SELECT final_recommendation FROM audit_log WHERE log_id = %s", (target,)
    ).fetchone()
    assert original is not None
    owner.execute(
        "UPDATE audit_log SET final_recommendation = 'No action needed.' WHERE log_id = %s",
        (target,),
    )
    report = verify_chain(agent)
    assert (report.ok, report.first_broken_log_id) == (False, target)
    assert report.reason == "row content does not match row_hash"

    owner.execute(
        "UPDATE audit_log SET final_recommendation = %s WHERE log_id = %s",
        (original["final_recommendation"], target),
    )
    assert verify_chain(agent).ok


def test_deleted_row_detected(agent: Conn, owner: Conn) -> None:
    victim = _agent_row(agent)
    successor = _agent_row(agent)
    saved = owner.execute("SELECT * FROM audit_log WHERE log_id = %s", (victim,)).fetchone()
    assert saved is not None
    owner.execute("DELETE FROM audit_log WHERE log_id = %s", (victim,))

    report = verify_chain(agent)
    assert (report.ok, report.first_broken_log_id) == (False, successor)
    assert report.reason == "previous_row_hash does not match the row before"

    cols = ", ".join(saved)
    owner.execute(
        f"INSERT INTO audit_log ({cols}) VALUES ({', '.join(['%s'] * len(saved))})",
        [psycopg.types.json.Jsonb(v) if k in ("tool_calls", "data_quality_flags") else v
         for k, v in saved.items()],
    )  # fmt: skip
    assert verify_chain(agent).ok
