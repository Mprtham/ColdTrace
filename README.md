# ColdTrace

An AI copilot for cold-chain dispatchers that shows its evidence before it recommends anything.

Design, scope and build plan: [`docs/DESIGN.md`](docs/DESIGN.md).

## Setup

```bash
uv sync
cp .env.example .env              # fill in DATABASE_URL (Neon)
git config core.hooksPath .githooks
uv run python -m scripts.generate_data   # deterministic synthetic fleet -> data/synthetic/
uv run python -m scripts.setup_db        # schema, view, roles, grants
uv run python -m scripts.ingest_telemetry  # load CSVs as coldtrace_admin
```

The generated fleet has fixed timestamps (72 hours ending 2026-10-01 00:00 UTC) so it is
reproducible. The ingest script shifts all timestamps so the most recent reading lands at
ingest time; faults keep their relative timing. Re-run `scripts/ingest_telemetry.py` to
refresh the demo data window.

The database is Neon's free tier, which suspends compute after 5 minutes idle. The first
query after a pause takes 1–2 seconds longer while it wakes; that is expected, not a fault.

## Ask a question

```bash
uv run python -m scripts.ingest_sop_qdrant   # SOP -> local Qdrant (once)
uv run python -m scripts.ask --replay "Find any trucks near Los Angeles with temperature problems and tell me what to do."
```

Prints the evidence (gather node) and the verdict (recommend node) separately, then the
audit row and whether the hash chain verifies. `--replay` sets the clock to the newest
reading in the database (or set `CLOCK_MODE=replay` in `.env`); without it, a fleet
loaded more than 15 minutes ago is correctly flagged STALE_FEED everywhere.

The LLM is local Ollama (`qwen2.5:7b`) by default; set `LLM_PROVIDER=deepseek` with
`DEEPSEEK_API_KEY` and `DEEPSEEK_MODEL` to use DeepSeek.

## HTTP API

```bash
uv run uvicorn api.main:app --reload        # interactive docs at http://localhost:8000/docs
```

```bash
curl -X POST localhost:8000/query -H "Content-Type: application/json"      -d '{"question": "Any trucks near Los Angeles with temperature problems?"}'
# -> session_id, log_id, evidence, verdict, sop_cited, confidence, data_quality_flags, token_counts

curl -X POST localhost:8000/decision -H "Content-Type: application/json"      -d '{"log_id": 3, "decision": "overridden", "reason": "Driver already at depot"}'

curl "localhost:8000/audit?tool_name=get_truck_telemetry&decision=overridden&limit=20"
curl localhost:8000/audit/verify
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
