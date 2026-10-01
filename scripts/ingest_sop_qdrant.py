"""Chunk the SOP documents in data/policy/, embed them, and load them into Qdrant.

    uv run python -m scripts.ingest_sop_qdrant

One chunk per `## Section` heading. Versions are ordered by effective_date; each version
is superseded by the next one, and its chunks get `superseded_by` and `valid_until` set.
Idempotent: the collection is recreated on every run.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from qdrant_client import QdrantClient, models

from src.config import get_settings
from src.tools.sop import COLLECTION, date_key, embed, get_client

POLICY_DIR = Path(__file__).resolve().parents[1] / "data" / "policy"
SECTION_RE = re.compile(r"^## Section (\S+) — (.+)$", re.M)
CARGO_RE = re.compile(r"^Cargo type:\s*(\S+)\s*$", re.M)
HEADER_RE = re.compile(r"^(effective_date|version):\s*(\S+)\s*$", re.M)


@dataclass
class Chunk:
    content: str
    section: str
    title: str
    version: str
    effective_date: str
    superseded_by: str | None
    valid_until: str | None
    cargo_type_scope: str | None
    source_file: str

    def payload(self) -> dict[str, object]:
        return {
            **asdict(self),
            "effective_from": date_key(date.fromisoformat(self.effective_date)),
            "valid_until": date_key(date.fromisoformat(self.valid_until))
            if self.valid_until
            else None,
        }


def parse_sop(path: Path) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Header fields, and (section, title, body) per section."""
    text = path.read_text(encoding="utf-8")
    header = dict(HEADER_RE.findall(text))
    matches = list(SECTION_RE.finditer(text))
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1), m.group(2).strip(), text[m.end() : end].strip()))
    return header, sections


def build_chunks(policy_dir: Path = POLICY_DIR) -> list[Chunk]:
    docs = sorted(
        (parse_sop(p) + (p.name,) for p in policy_dir.glob("sop_v*.md")),
        key=lambda d: d[0]["effective_date"],
    )
    chunks = []
    for i, (header, sections, source) in enumerate(docs):
        successor = docs[i + 1][0] if i + 1 < len(docs) else None
        for section, title, body in sections:
            cargo = CARGO_RE.search(body)
            chunks.append(
                Chunk(
                    content=f"SOP v{header['version']} §{section} — {title}\n{body}",
                    section=section,
                    title=title,
                    version=header["version"],
                    effective_date=header["effective_date"],
                    superseded_by=successor["version"] if successor else None,
                    valid_until=successor["effective_date"] if successor else None,
                    cargo_type_scope=cargo.group(1) if cargo else None,
                    source_file=source,
                )
            )
    return chunks


def ingest(client: QdrantClient, policy_dir: Path = POLICY_DIR) -> int:
    chunks = build_chunks(policy_dir)
    vectors = embed([c.content for c in chunks])
    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        COLLECTION,
        vectors_config=models.VectorParams(
            size=get_settings().embedding_dim, distance=models.Distance.COSINE
        ),
    )
    client.upsert(
        COLLECTION,
        points=[
            models.PointStruct(
                # Deterministic IDs: same file + section always maps to the same point.
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{c.source_file}#{c.section}")),
                vector=v,
                payload=c.payload(),
            )
            for c, v in zip(chunks, vectors, strict=True)
        ],
    )
    return len(chunks)


def ensure_sop_loaded(client: QdrantClient, policy_dir: Path = POLICY_DIR) -> int:
    """Load the SOP only if the collection is missing or empty. Returns chunks loaded (0 if
    already there). Lets an in-memory Qdrant rebuild itself on every API start."""
    if client.collection_exists(COLLECTION) and client.count(COLLECTION).count > 0:
        return 0
    return ingest(client, policy_dir)


def main() -> None:
    client = get_client()
    n = ingest(client)
    client.close()
    print(f"Loaded {n} SOP chunks into Qdrant collection '{COLLECTION}'.")


if __name__ == "__main__":
    main()
