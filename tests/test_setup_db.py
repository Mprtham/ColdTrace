"""setup_db.sql against a real PostgreSQL (pgserver, no Docker).

The headline check is the privilege set of coldtrace_agent: the reference project's audit
log silently failed because its setup script never granted INSERT (docs/DESIGN.md §2.2
gap #1). Here the grants are asserted, not assumed."""

from collections.abc import Callable, Iterator

import psycopg
import pytest

from scripts.setup_db import apply

AUDIT_INSERT = (
    "INSERT INTO audit_log (session_id, created_at, dispatcher_question, row_hash) "
    "VALUES (gen_random_uuid(), now(), 'q', md5(random()::text) || md5(random()::text))"
)


@pytest.fixture(scope="module")
def url(fresh_db: Callable[[], str]) -> str:
    uri = fresh_db()
    apply(uri)  # idempotent: a second run on an existing schema must not fail
    with psycopg.connect(uri, autocommit=True) as c:
        c.execute("INSERT INTO trucks VALUES ('TRK-001','CT-1','insulin',2,8,'Los Angeles',now())")
    return uri


@pytest.fixture
def agent(url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("SET ROLE coldtrace_agent")
        yield c


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM vw_fleet_with_quality",
        "SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1",
        AUDIT_INSERT,
    ],
)
def test_agent_allowed(agent: psycopg.Connection, statement: str) -> None:
    agent.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_log SET final_recommendation = 'x'",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "SELECT * FROM telemetry",
        "SELECT * FROM trucks",
        "INSERT INTO trucks (truck_id) VALUES ('x')",
        "SELECT setval('audit_log_log_id_seq', 1)",
    ],
)
def test_agent_denied(agent: psycopg.Connection, statement: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        agent.execute(statement)


def test_trip_status_rejects_freeform(url: str) -> None:
    with psycopg.connect(url) as c, pytest.raises(psycopg.errors.InvalidTextRepresentation):
        c.execute(
            "INSERT INTO telemetry (truck_id, shipment_id, recorded_at, ingested_at, lat, lon, "
            "trip_status, temperature_c, cargo_condition_code, delay_probability, "
            "route_risk_index) VALUES ('TRK-001','S',now(),now(),34,-118,'parked',3,'OK',0.1,1)"
        )


def test_faulty_temperature_still_lands(url: str) -> None:
    # The table must accept an 85 °C reading: rejecting it would hide the fault from the gate.
    with psycopg.connect(url) as c:
        c.execute(
            "INSERT INTO telemetry (truck_id, shipment_id, recorded_at, ingested_at, lat, lon, "
            "trip_status, temperature_c, cargo_condition_code, delay_probability, "
            "route_risk_index) VALUES ('TRK-001','S',now(),now(),34,-118,'in_transit',85,'CRIT',"
            "0.1,1)"
        )
        c.rollback()


def test_override_requires_reason(url: str) -> None:
    with psycopg.connect(url) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute(
            "INSERT INTO audit_log (amends_log_id, session_id, created_at, human_decision, "
            "row_hash) VALUES (NULL, gen_random_uuid(), now(), 'overridden', repeat('b', 64))"
        )


def test_agent_row_must_carry_question(url: str) -> None:
    with psycopg.connect(url) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute(
            "INSERT INTO audit_log (session_id, created_at, row_hash) "
            "VALUES (gen_random_uuid(), now(), repeat('c', 64))"
        )
