"""Ask ColdTrace a question from the terminal — the Phase 3 checkpoint.

    uv run python -m scripts.ask "Any trucks near Los Angeles with temperature problems?"
    uv run python -m scripts.ask --replay "..."   # clock = newest reading, not now

Prints the evidence (gather node) and the verdict (recommend node) separately, then the
audit row that was written and whether the hash chain still verifies.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from src.audit import verify_chain
from src.config import get_settings
from src.db import agent_connection, scrubbed_errors
from src.orchestrator import run_query
from src.tools.registry import ToolContext
from src.tools.sop import get_client


def _summarise(call: dict[str, Any]) -> str:
    if call["error"]:
        return f"ERROR — {call['error']}"
    out = call["output"]
    if "readings" in out:
        flags = ", ".join(f"{k}×{v}" for k, v in call["quality_flags"].items()) or "all CLEAN"
        return f"{len(out['readings'])} readings; quality: {flags}"
    if "waypoints" in out:
        hazards = out["hazards"] or "none"
        return f"{out['route_km']} km, max {out['max_ambient_c']}°C; hazards: {hazards}"
    if "results" in out:
        return "; ".join(r["citation"] for r in out["results"]) or "no rules in force"
    return json.dumps(out)[:120]


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask ColdTrace a dispatcher question.")
    parser.add_argument("question")
    parser.add_argument("--replay", action="store_true", help="clock = newest reading in the DB")
    args = parser.parse_args()
    settings = get_settings()

    with scrubbed_errors(settings.secrets()), agent_connection() as conn:
        as_of = None
        if args.replay:
            row = conn.execute("SELECT max(recorded_at) AS m FROM vw_fleet_with_quality").fetchone()
            as_of = row["m"] if row else None

        started = time.perf_counter()
        out = run_query(args.question, conn=conn, ctx=ToolContext(), as_of=as_of)
        elapsed = time.perf_counter() - started
        r = out.result
        chain = verify_chain(conn)

    if get_client.cache_info().currsize:  # close Qdrant before interpreter shutdown
        get_client().close()

    model = (
        settings.deepseek_model if settings.llm_provider == "deepseek" else settings.ollama_model
    )
    print(f"\nQUESTION  {r.question}")
    print(f"INTENT    {r.intent}   (clock {r.as_of:%Y-%m-%d %H:%M} UTC)" if r.as_of else "")
    print("\n── EVIDENCE (gather) " + "─" * 50)
    for i, call in enumerate(r.tool_calls, 1):
        print(f"{i}. {call['tool_name']}({json.dumps(call['input'])})")
        print(f"   → {_summarise(call)}")
    if not r.tool_calls:
        print("(no tools called)")
    print("\n── VERDICT (recommend) " + "─" * 48)
    print(r.recommendation)
    print(f"\nSOP cited   {r.sop_clause_cited or '—'}")
    print(f"Confidence  {r.confidence}")
    for c in r.caveats:
        print(f"Caveat      {c}")
    print("\n── AUDIT " + "─" * 62)
    print(f"log_id {out.audit.log_id}   session {out.session_id}")
    print(f"row_hash {out.audit.row_hash[:16]}…  prev {str(out.audit.previous_row_hash)[:16]}…")
    print(f"chain verified: {chain.ok} ({chain.rows_checked} rows)")
    print(f"tokens in/out {r.tokens_in}/{r.tokens_out}   model {settings.llm_provider}:{model}   "
          f"{elapsed:.1f}s")  # fmt: skip


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    main()
