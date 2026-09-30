# ColdTrace

An AI copilot for cold-chain dispatchers that shows its evidence before it recommends anything.

Design, scope and build plan: [`docs/DESIGN.md`](docs/DESIGN.md).

## Setup

```bash
uv sync
cp .env.example .env              # fill in DATABASE_URL (Neon)
git config core.hooksPath .githooks
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```
