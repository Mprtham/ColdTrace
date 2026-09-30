"""Connection URLs for the three database identities: the Neon owner (DATABASE_URL, runs
migrations), coldtrace_admin (loads data), coldtrace_agent (the app)."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url


def libpq_url(url: str) -> str:
    """psycopg wants a plain libpq URL, without SQLAlchemy's +driver suffix."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def role_url(url: str, role: str, password: str | None) -> str:
    """Same host and database as `url`, logged in as `role`."""
    swapped = make_url(url).set(username=role, password=password)
    return libpq_url(swapped.render_as_string(hide_password=False))


@contextmanager
def agent_connection() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    """A connection as coldtrace_agent — the only identity the app and tools use."""
    from src.config import get_settings

    settings = get_settings()
    url = role_url(settings.psycopg_url(), "coldtrace_agent", settings.coldtrace_agent_password)
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        yield conn


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


@contextmanager
def scrubbed_errors(secrets: list[str]) -> Iterator[None]:
    """Exit with the error message minus any password. libpq and SQLAlchemy both echo
    connection strings in some errors; a pasted traceback must never leak credentials."""
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — every error type can carry a DSN
        print(f"error: {redact(f'{type(exc).__name__}: {exc}', secrets)}", file=sys.stderr)
        raise SystemExit(1) from None
