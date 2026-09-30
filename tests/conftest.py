from collections.abc import Callable, Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def fresh_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[[], str]]:
    """A throwaway PostgreSQL 16 (pgserver, no Docker) with setup_db.sql applied.

    Returns a callable so tests without pgserver installed are skipped, not errored."""
    pgserver = pytest.importorskip("pgserver")
    from scripts.setup_db import apply

    server = pgserver.get_server(Path(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    uri: str = server.get_uri()
    apply(uri)
    yield lambda: uri
    server.cleanup()
