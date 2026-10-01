"""SOP retrieval (docs/DESIGN.md §3.2 E). The headline guarantee: a superseded rule is
never retrieved — v2.4 says escalate after 60 minutes, v3.0 says 30, and only 30 may
come back for today's date."""

from collections.abc import Iterator
from datetime import date

import pytest
from qdrant_client import QdrantClient

from scripts.generate_data import CARGO_RANGES
from scripts.ingest_sop_qdrant import build_chunks, ingest
from src.tools.sop import COLLECTION, search_sop


@pytest.fixture(scope="module")
def client() -> Iterator[QdrantClient]:
    c = QdrantClient(":memory:")
    ingest(c)
    yield c
    c.close()


# --- The five required guarantees --------------------------------------------------


def test_superseded_rule_never_retrieved(client: QdrantClient) -> None:
    results = search_sop("escalation threshold fresh perishables", client=client)
    assert "30 minutes" in results[0].content
    assert all("60 minutes" not in r.content for r in results)


def test_result_carries_current_version(client: QdrantClient) -> None:
    results = search_sop("escalation threshold fresh perishables", client=client)
    assert {r.version for r in results} == {"3.0"}


def test_result_carries_section_reference(client: QdrantClient) -> None:
    results = search_sop(
        "escalation threshold fresh perishables", cargo_type="fresh_perishables", client=client
    )
    assert results[0].section == "1.1"


def test_cargo_filter_excludes_other_scopes(client: QdrantClient) -> None:
    results = search_sop("temperature breach escalation", cargo_type="frozen", k=10, client=client)
    assert "1.2" in {r.section for r in results}
    assert all(r.cargo_type_scope in ("frozen", None) for r in results)


@pytest.mark.parametrize("cargo_type", sorted(CARGO_RANGES))
def test_general_sections_apply_to_any_cargo(client: QdrantClient, cargo_type: str) -> None:
    results = search_sop("port congestion diversion", cargo_type=cargo_type, client=client)
    assert "2.1" in {r.section for r in results}


# --- Point-in-time behaviour ------------------------------------------------------


def test_past_date_returns_rule_in_force_then(client: QdrantClient) -> None:
    results = search_sop(
        "escalation threshold fresh perishables",
        cargo_type="fresh_perishables",
        as_of_date=date(2023, 1, 1),
        client=client,
    )
    assert results[0].version == "2.4"
    assert "60 minutes" in results[0].content


def test_switchover_happens_on_effective_date(client: QdrantClient) -> None:
    def version_on(d: date) -> set[str]:
        return {r.version for r in search_sop("escalation", as_of_date=d, k=10, client=client)}

    assert version_on(date(2024, 5, 31)) == {"2.4"}
    assert version_on(date(2024, 6, 1)) == {"3.0"}


def test_nothing_in_force_before_first_sop(client: QdrantClient) -> None:
    assert search_sop("escalation", as_of_date=date(2020, 12, 31), client=client) == []


# --- Ingest -----------------------------------------------------------------------


def test_chunk_metadata() -> None:
    chunks = {(c.version, c.section): c for c in build_chunks()}
    assert len(chunks) == 8
    old, new = chunks[("2.4", "1.1")], chunks[("3.0", "1.1")]
    assert (old.superseded_by, old.valid_until) == ("3.0", "2024-06-01")
    assert (new.superseded_by, new.valid_until) == (None, None)
    assert old.cargo_type_scope == "fresh_perishables"
    assert chunks[("3.0", "2.1")].cargo_type_scope is None
    assert old.source_file == "sop_v2_4.md"
    assert old.effective_date == "2021-01-01"


def test_ingest_is_idempotent(client: QdrantClient) -> None:
    ingest(client)
    assert client.count(COLLECTION).count == 8


def test_ensure_sop_loaded_only_when_empty() -> None:
    from scripts.ingest_sop_qdrant import ensure_sop_loaded

    fresh = QdrantClient(":memory:")
    assert ensure_sop_loaded(fresh) == 8
    assert ensure_sop_loaded(fresh) == 0  # already there: no rebuild
    assert search_sop("escalation", client=fresh)[0].version == "3.0"
    fresh.close()
