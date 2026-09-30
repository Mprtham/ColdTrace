"""Apply scripts/setup_db.sql, then set role passwords from the environment.

    uv run python -m scripts.setup_db

Connects with DATABASE_URL (the Neon owner role, which owns the schema), creates the
tables, view and roles, then sets role passwords from COLDTRACE_ADMIN_PASSWORD and
COLDTRACE_AGENT_PASSWORD (.env or environment). A role without a password exists but
cannot log in yet.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg import sql

from src.config import get_settings
from src.db import scrubbed_errors

SQL_FILE = Path(__file__).with_name("setup_db.sql")


def apply(conninfo: str, passwords: dict[str, str | None] | None = None) -> list[str]:
    """Run the schema script and set any provided role passwords. Returns roles updated."""
    updated = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(SQL_FILE.read_text(encoding="utf-8"))  # script has its own BEGIN/COMMIT
        for role, password in (passwords or {}).items():
            if password:
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                updated.append(role)
    return updated


def main() -> None:
    settings = get_settings()
    with scrubbed_errors(settings.secrets()):
        updated = apply(
            settings.psycopg_url(),
            {
                "coldtrace_admin": settings.coldtrace_admin_password,
                "coldtrace_agent": settings.coldtrace_agent_password,
            },
        )
    print(f"Applied {SQL_FILE.name}. Passwords set for: {', '.join(updated) or 'none'}")


if __name__ == "__main__":
    main()
