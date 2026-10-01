"""ColdTrace HTTP API — docs/DESIGN.md §5.2, §6.

    uv run uvicorn api.main:app --reload

Thin by design: each route validates its input, calls one function from src/, and maps
errors to status codes. No agent logic lives here.

    POST /query           run_query()       question -> evidence + verdict + audit row
    POST /decision        write_decision()  accept / override an agent row
    GET  /audit           read_audit()      filtered, keyset-paginated agent rows
    GET  /audit/verify    verify_chain()    walk the hash chain
    GET  /audit/{log_id}  get_audit_row()   one row with full tool outputs and decisions
    GET  /health
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from scripts.ingest_sop_qdrant import ensure_sop_loaded
from src.audit import (
    MAX_PAGE,
    current_decision,
    get_audit_row,
    read_audit,
    verify_chain,
    write_decision,
)
from src.config import get_settings
from src.db import agent_connection, redact
from src.orchestrator import run_query
from src.tools.registry import TOOLS, ToolContext, ToolSpec
from src.tools.sop import get_client
from src.tools.telemetry import Conn

ToolName = Literal[
    "get_fleet_status",
    "get_truck_telemetry",
    "get_temperature_history",
    "get_high_risk_shipments",
    "fetch_route_conditions",
    "search_sop",
]


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # With an in-memory Qdrant (the free-tier deploy), the SOP is rebuilt on each start.
    if not get_settings().qdrant_url:
        ensure_sop_loaded(get_client())
    yield
    if get_client.cache_info().currsize:
        get_client().close()


app = FastAPI(
    title="ColdTrace",
    description="Cold-chain dispatch copilot: evidence first, then a verdict, every "
    "decision in a hash-chained audit log.",
    version="0.4.0",
    lifespan=lifespan,
)


# --- Dependencies (overridden in tests) ------------------------------------------------


def get_conn() -> Iterator[Conn]:
    with agent_connection() as conn:
        yield conn


def get_llm() -> Any:
    return None  # run_query() builds the configured model


def get_tools() -> dict[str, ToolSpec]:
    return TOOLS


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = get_settings().api_key
    if expected and not (x_api_key and secrets.compare_digest(x_api_key, expected)):
        raise HTTPException(401, detail="missing or wrong X-API-Key")


Protected = [Depends(require_api_key)]
ConnDep = Annotated[Conn, Depends(get_conn)]
LlmDep = Annotated[Any, Depends(get_llm)]
ToolsDep = Annotated[dict[str, ToolSpec], Depends(get_tools)]


def _unavailable(exc: Exception) -> HTTPException:
    detail = redact(f"{type(exc).__name__}: {exc}", get_settings().secrets())
    return HTTPException(503, detail=f"agent unavailable: {detail}")


# --- Models ----------------------------------------------------------------------------


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None


class TokenCounts(BaseModel):
    input: int
    output: int


class QueryResponse(BaseModel):
    session_id: UUID
    log_id: int
    row_hash: str
    intent: str
    as_of: datetime | None
    evidence: list[dict[str, Any]]
    verdict: str
    sop_cited: str | None
    confidence: str
    caveats: list[str]
    data_quality_flags: list[str]
    token_counts: TokenCounts


class DecisionRequest(BaseModel):
    log_id: int = Field(ge=1)
    decision: Literal["accepted", "overridden"]
    reason: str | None = Field(None, max_length=2000)


class DecisionResponse(BaseModel):
    log_id: int
    amends_log_id: int
    decision: str
    row_hash: str


class ToolCallSummary(BaseModel):
    tool_name: str
    input: dict[str, Any]
    quality_flags: dict[str, int]
    error: str | None


class AuditItem(BaseModel):
    log_id: int
    session_id: UUID
    created_at: datetime
    dispatcher_question: str
    tools: list[ToolCallSummary]
    data_quality_flags: list[str]
    final_recommendation: str | None
    sop_clause_cited: str | None
    token_count_in: int | None
    token_count_out: int | None
    current_decision: str
    decision_reason: str | None
    decided_at: datetime | None
    row_hash: str


class AuditPageResponse(BaseModel):
    items: list[AuditItem]
    next_cursor: int | None


class ChainResponse(BaseModel):
    ok: bool
    rows_checked: int
    first_broken_log_id: int | None
    reason: str | None


# --- Routes ----------------------------------------------------------------------------


@app.post("/query", response_model=QueryResponse, dependencies=Protected)
def query(
    body: QueryRequest,
    conn: ConnDep,
    llm: LlmDep,
    tools: ToolsDep,
) -> QueryResponse:
    try:
        out = run_query(
            body.question, body.session_id, llm=llm, tools=tools, conn=conn, ctx=ToolContext()
        )
    except Exception as exc:  # noqa: BLE001 — LLM, DB or vector store down
        raise _unavailable(exc) from exc
    r = out.result
    return QueryResponse(
        session_id=UUID(out.session_id),
        log_id=out.audit.log_id,
        row_hash=out.audit.row_hash,
        intent=r.intent,
        as_of=r.as_of,
        evidence=r.tool_calls,
        verdict=r.recommendation,
        sop_cited=r.sop_clause_cited,
        confidence=r.confidence,
        caveats=r.caveats,
        data_quality_flags=r.quality_flags,
        token_counts=TokenCounts(input=r.tokens_in, output=r.tokens_out),
    )


@app.post("/decision", response_model=DecisionResponse, dependencies=Protected)
def decision(body: DecisionRequest, conn: ConnDep) -> DecisionResponse:
    try:
        entry = write_decision(
            conn,
            amends_log_id=body.log_id,
            decision=body.decision,
            override_reason=body.reason,
        )
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return DecisionResponse(
        log_id=entry.log_id,
        amends_log_id=body.log_id,
        decision=body.decision,
        row_hash=entry.row_hash,
    )


@app.get("/audit", response_model=AuditPageResponse, dependencies=Protected)
def audit(
    conn: ConnDep,
    session_id: UUID | None = None,
    tool_name: ToolName | None = None,
    decision: Literal["pending", "accepted", "overridden"] | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 20,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> AuditPageResponse:
    page = read_audit(
        conn,
        session_id=session_id,
        tool_name=tool_name,
        decision=decision,
        limit=limit,
        cursor=cursor,
    )
    return AuditPageResponse(items=[_item(row) for row in page.items], next_cursor=page.next_cursor)


@app.get("/audit/verify", response_model=ChainResponse, dependencies=Protected)
def audit_verify(conn: ConnDep) -> ChainResponse:
    report = verify_chain(conn)
    return ChainResponse(
        ok=report.ok,
        rows_checked=report.rows_checked,
        first_broken_log_id=report.first_broken_log_id,
        reason=report.reason,
    )


@app.get("/audit/{log_id}", dependencies=Protected)
def audit_row(log_id: int, conn: ConnDep) -> dict[str, Any]:
    row = get_audit_row(conn, log_id)
    if row is None:
        raise HTTPException(404, detail=f"no audit row {log_id}")
    decisions = conn.execute(
        "SELECT log_id, created_at, human_decision, override_reason, row_hash "
        "FROM audit_log WHERE amends_log_id = %s ORDER BY log_id",
        (log_id,),
    ).fetchall()
    return {**row, "current_decision": current_decision(conn, log_id), "decisions": decisions}


@app.get("/health")
def health(conn: ConnDep) -> dict[str, Any]:
    conn.execute("SELECT 1")
    settings = get_settings()
    return {
        "status": "ok",
        "database": "ok",
        "llm_provider": settings.llm_provider,
        "clock_mode": settings.clock_mode,
    }


def _item(row: dict[str, Any]) -> AuditItem:
    return AuditItem(
        **{k: row[k] for k in AuditItem.model_fields if k != "tools"},
        tools=[
            ToolCallSummary(
                tool_name=c["tool_name"],
                input=c.get("input") or {},
                quality_flags=c.get("quality_flags") or {},
                error=c.get("error"),
            )
            for c in row["tool_calls"]
        ],
    )
