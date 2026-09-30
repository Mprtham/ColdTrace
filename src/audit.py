"""Append-only, hash-chained audit log — docs/DESIGN.md §4.4.

Every row's row_hash is SHA-256 over its content plus the previous row's hash, so
altering or deleting any past row breaks every hash after it (tamper-evident, not
tamper-proof: see §8). Rows are never updated: a dispatcher's accept/override is a new
row whose amends_log_id points at the agent row it decides on.

Writers serialise on a transaction-scoped advisory lock, so two concurrent inserts can
never read the same "last" hash and fork the chain.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

Conn = psycopg.Connection[dict[str, Any]]
Decision = Literal["accepted", "overridden"]

# Arbitrary constant naming the one lock every audit writer takes.
CHAIN_LOCK_KEY = 0x436F6C64  # "Cold"

# Everything except log_id (assigned by the sequence) and row_hash itself. Order does not
# matter — the canonical form sorts keys — but the set does: changing it breaks old chains.
HASHED_FIELDS = (
    "amends_log_id",
    "session_id",
    "created_at",
    "dispatcher_question",
    "tool_calls",
    "data_quality_flags",
    "final_recommendation",
    "sop_clause_cited",
    "human_decision",
    "override_reason",
    "token_count_in",
    "token_count_out",
    "previous_row_hash",
)
JSON_FIELDS = ("tool_calls", "data_quality_flags")


@dataclass(frozen=True)
class AuditEntry:
    log_id: int
    created_at: datetime
    previous_row_hash: str | None
    row_hash: str


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    rows_checked: int
    first_broken_log_id: int | None = None
    reason: str | None = None


# --- Hashing ---------------------------------------------------------------------------


def canonical(fields: Mapping[str, Any]) -> bytes:
    """Deterministic bytes for the hashed fields, identical before insert and after a
    round trip through PostgreSQL (UUIDs as strings, timestamps in UTC to microseconds)."""

    def norm(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat(timespec="microseconds")
        if isinstance(value, UUID):
            return str(value)
        return value

    body = {k: norm(fields[k]) for k in HASHED_FIELDS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_row_hash(fields: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(fields)).hexdigest()


# --- Writers ---------------------------------------------------------------------------


def write_agent_row(
    conn: Conn,
    *,
    session_id: UUID | str,
    dispatcher_question: str,
    tool_calls: list[dict[str, Any]],
    data_quality_flags: list[str],
    final_recommendation: str | None,
    sop_clause_cited: str | None,
    token_count_in: int | None,
    token_count_out: int | None,
    created_at: datetime | None = None,
) -> AuditEntry:
    """One row per dispatcher question, written as `pending`."""
    return _append(
        conn,
        {
            "amends_log_id": None,
            "session_id": str(session_id),
            "created_at": created_at or datetime.now(UTC),
            "dispatcher_question": dispatcher_question,
            "tool_calls": tool_calls,
            "data_quality_flags": data_quality_flags,
            "final_recommendation": final_recommendation,
            "sop_clause_cited": sop_clause_cited,
            "human_decision": "pending",
            "override_reason": None,
            "token_count_in": token_count_in,
            "token_count_out": token_count_out,
        },
    )


def write_decision(
    conn: Conn,
    *,
    amends_log_id: int,
    session_id: UUID | str,
    decision: Decision,
    override_reason: str | None = None,
    created_at: datetime | None = None,
) -> AuditEntry:
    """Record a dispatcher's accept/override as a new row amending an agent row."""
    if decision not in ("accepted", "overridden"):
        raise ValueError(f"decision must be 'accepted' or 'overridden', got {decision!r}")
    if decision == "overridden" and not (override_reason and override_reason.strip()):
        raise ValueError("an override needs a reason")
    target = conn.execute(
        "SELECT amends_log_id FROM audit_log WHERE log_id = %s", (amends_log_id,)
    ).fetchone()
    if target is None:
        raise LookupError(f"no audit row {amends_log_id}")
    if target["amends_log_id"] is not None:
        raise ValueError(f"row {amends_log_id} is itself a decision; amend the agent row")
    return _append(
        conn,
        {
            "amends_log_id": amends_log_id,
            "session_id": str(session_id),
            "created_at": created_at or datetime.now(UTC),
            "dispatcher_question": None,
            "tool_calls": [],
            "data_quality_flags": [],
            "final_recommendation": None,
            "sop_clause_cited": None,
            "human_decision": decision,
            "override_reason": override_reason if decision == "overridden" else None,
            "token_count_in": None,
            "token_count_out": None,
        },
    )


def _append(conn: Conn, row: dict[str, Any]) -> AuditEntry:
    # Normalise JSON fields to exactly what JSONB will hand back, so the hash computed now
    # matches the hash verify_chain() recomputes later. Non-JSON values fail loudly here.
    for key in JSON_FIELDS:
        row[key] = json.loads(json.dumps(row[key]))

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (CHAIN_LOCK_KEY,))
        last = conn.execute(
            "SELECT row_hash FROM audit_log ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
        row["previous_row_hash"] = last["row_hash"] if last else None
        row["row_hash"] = compute_row_hash(row)
        params = {k: Jsonb(v) if k in JSON_FIELDS else v for k, v in row.items()}
        cols = ", ".join(params)
        values = ", ".join(f"%({k})s" for k in params)
        inserted = conn.execute(
            f"INSERT INTO audit_log ({cols}) VALUES ({values}) RETURNING log_id", params
        ).fetchone()
    assert inserted is not None
    return AuditEntry(
        inserted["log_id"], row["created_at"], row["previous_row_hash"], row["row_hash"]
    )


# --- Readers ---------------------------------------------------------------------------


def verify_chain(conn: Conn) -> ChainReport:
    """Walk the whole chain in log_id order; report the first row that does not check out."""
    previous: str | None = None
    checked = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY log_id"):
        checked += 1
        if row["previous_row_hash"] != previous:
            return ChainReport(
                False, checked, row["log_id"], "previous_row_hash does not match the row before"
            )
        if compute_row_hash(row) != row["row_hash"]:
            return ChainReport(False, checked, row["log_id"], "row content does not match row_hash")
        previous = row["row_hash"]
    return ChainReport(True, checked)


def current_decision(conn: Conn, log_id: int) -> str:
    """The latest human decision on an agent row, or 'pending'."""
    row = conn.execute(
        "SELECT human_decision FROM audit_log WHERE amends_log_id = %s "
        "ORDER BY log_id DESC LIMIT 1",
        (log_id,),
    ).fetchone()
    return row["human_decision"] if row else "pending"
