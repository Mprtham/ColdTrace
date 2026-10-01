"""SOP retrieval with version awareness — docs/DESIGN.md §3.2 Improvement E.

Only rules in force on `as_of_date` (default: today) can be retrieved. A chunk is in
force when its version's effective_date has passed and it has not yet been replaced:

    effective_from <= as_of  AND  (valid_until IS NULL OR valid_until > as_of)

With as_of = today this is the `superseded_by IS NULL` rule, with two differences that
matter in a regulated setting: a newer version loaded before its effective date does not
hide the rule still in force, and an auditor can ask which rule applied on a past date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache

from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from src.config import get_settings

COLLECTION = "sop_chunks"


@dataclass(frozen=True)
class SopResult:
    content: str
    section: str
    version: str
    effective_date: str
    cargo_type_scope: str | None
    score: float


@lru_cache
def get_embedder() -> TextEmbedding:
    return TextEmbedding(get_settings().embedding_model)


def embed(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in get_embedder().embed(texts)]


@lru_cache
def get_client() -> QdrantClient:
    settings = get_settings()
    if settings.qdrant_url:
        return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    if settings.qdrant_path == ":memory:":
        return QdrantClient(":memory:")  # rebuilt at API startup; see ensure_sop_loaded()
    return QdrantClient(path=settings.qdrant_path)


def date_key(d: date) -> int:
    """2024-06-01 -> 20240601. Qdrant range filters compare numbers, not date strings."""
    return d.year * 10_000 + d.month * 100 + d.day


def in_force_filter(cargo_type: str | None, as_of_date: date | None) -> models.Filter:
    day = date_key(as_of_date or date.today())
    must: list[models.Condition] = [
        models.FieldCondition(key="effective_from", range=models.Range(lte=day)),
        models.Filter(
            should=[
                models.IsNullCondition(is_null=models.PayloadField(key="valid_until")),
                models.FieldCondition(key="valid_until", range=models.Range(gt=day)),
            ]
        ),
    ]
    if cargo_type:
        # Rules scoped to this cargo type, plus general rules that apply to all cargo.
        must.append(
            models.Filter(
                should=[
                    models.FieldCondition(
                        key="cargo_type_scope", match=models.MatchValue(value=cargo_type)
                    ),
                    models.IsNullCondition(is_null=models.PayloadField(key="cargo_type_scope")),
                ]
            )
        )
    return models.Filter(must=must)


def search_sop(
    query: str,
    cargo_type: str | None = None,
    as_of_date: date | None = None,
    k: int = 3,
    *,
    client: QdrantClient | None = None,
) -> list[SopResult]:
    """Top-k SOP chunks in force on `as_of_date`, most relevant first."""
    hits = (client or get_client()).query_points(
        COLLECTION,
        query=embed([query])[0],
        query_filter=in_force_filter(cargo_type, as_of_date),
        limit=k,
        with_payload=True,
    )
    results = []
    for point in hits.points:
        p = point.payload or {}
        results.append(
            SopResult(
                content=p["content"],
                section=p["section"],
                version=p["version"],
                effective_date=p["effective_date"],
                cargo_type_scope=p["cargo_type_scope"],
                score=point.score,
            )
        )
    return results
