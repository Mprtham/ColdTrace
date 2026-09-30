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

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
