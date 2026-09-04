"""Embeddings and vector storage.

Embeddings call the provider SDK DIRECTLY, never through the LiteLLM router
(ARCHITECTURE.md §2, TC-0905). Different API surface, no guardrails, no
completion, incompatible cache keys.

One Qdrant collection for everything, filtered by a source_id payload field -
not one collection per job (TC-0101).
"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.graph.state import Chunk

log = logging.getLogger(__name__)


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=get_settings().qdrant_url)


def ensure_collection() -> None:
    """Create the single shared collection if absent. Idempotent."""
    from qdrant_client.models import Distance, VectorParams

    s = get_settings()
    client = _client()
    existing = {c.name for c in client.get_collections().collections}
    if s.qdrant_collection not in existing:
        client.create_collection(
            collection_name=s.qdrant_collection,
            vectors_config=VectorParams(size=s.embedding_dim, distance=Distance.COSINE),
        )
        log.info("created qdrant collection %s", s.qdrant_collection)


# The provider caps inputs per request; a 500-page PDF exceeds it comfortably.
_EMBED_BATCH = 96


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed via the provider SDK directly.

    Deliberately NOT routed through app.gateway. If no key is configured we fall
    back to a deterministic local vector so ingestion stays testable offline -
    ingestion must never depend on a provider being reachable.
    """
    s = get_settings()
    if not texts:
        return []

    if not s.embedding_api_key:
        # Loud, not just logged. "If a component is allowed to fail silently,
        # something must periodically assert it is actually working" - and a
        # log line nobody reads is how retrieval came to look healthy while
        # ranking nothing (docs/v2/ARCHITECTURE.md §13.2, POC.md §5).
        _mark_degraded()
        log.warning(
            "EMBEDDING_API_KEY is not set: using deterministic hash vectors. "
            "Retrieval will return chunks but CANNOT rank them by meaning."
        )
        return [_offline_vector(t, s.embedding_dim) for t in texts]

    from openai import OpenAI

    # Batched: a long source sends every chunk in one request otherwise, and
    # the provider rejects the whole batch over its input limit - losing an
    # extraction that had already succeeded.
    client = OpenAI(api_key=s.embedding_api_key, timeout=s.embedding_timeout)
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        batch = texts[start : start + _EMBED_BATCH]
        resp = client.embeddings.create(model=s.embedding_model, input=batch)
        vectors.extend(d.embedding for d in resp.data)
    return vectors


# Set the first time the offline fallback is used, so the API and the
# dashboard can tell the operator that retrieval is not semantic - rather than
# leaving them to infer it from a log line in a container.
_DEGRADED = False


def is_degraded() -> bool:
    """True when embeddings are hash-derived rather than semantic."""
    return _DEGRADED


def degraded_reason() -> str:
    return (
        "Embeddings are running offline: retrieval returns chunks in document "
        "order and cannot rank them by meaning. Set EMBEDDING_API_KEY for "
        "semantic retrieval."
    )


def _mark_degraded() -> None:
    global _DEGRADED
    _DEGRADED = True


def _offline_vector(text: str, dim: int) -> list[float]:
    """Deterministic pseudo-embedding. Same text always yields the same vector.

    Good enough for pipeline tests and offline demos; useless for real semantic
    similarity, which is why it logs a warning.
    """
    import hashlib
    import struct

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    raw = (digest * ((dim * 4 // len(digest)) + 1))[: dim * 4]
    vals = list(struct.unpack(f"{dim}f", raw))
    norm = sum(v * v for v in vals) ** 0.5 or 1.0
    return [v / norm for v in vals]


def upsert_chunks(chunks: list[Chunk]) -> int:
    """Write chunk vectors, each carrying its source_id in the payload."""
    from qdrant_client.models import PointStruct

    if not chunks:
        return 0

    ensure_collection()
    vectors = embed_texts([c.text for c in chunks])
    points = [
        PointStruct(
            id=_point_id(c.source_id, c.id),
            vector=vec,
            payload={
                "source_id": c.source_id,
                "chunk_id": c.id,
                "text": c.text,
                "page": c.page,
            },
        )
        for c, vec in zip(chunks, vectors, strict=True)
    ]
    _client().upsert(collection_name=get_settings().qdrant_collection, points=points)
    return len(points)


def _point_id(source_id: str, chunk_id: str) -> int:
    """Qdrant needs int or UUID ids; derive a stable one from the pair.

    Stable means re-ingesting the same source overwrites rather than duplicates.
    """
    import hashlib

    h = hashlib.sha256(f"{source_id}:{chunk_id}".encode()).digest()
    return int.from_bytes(h[:8], "big") >> 1  # positive int64


def search(source_ids: list[str], query: str, k: int = 5) -> list[dict]:
    """Retrieve chunks scoped to the given sources.

    Scoping by source_id in the filter - not by collection - is what makes
    cross-job access structurally impossible (TC-1003).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny

    if not source_ids:
        return []

    ensure_collection()
    vector = embed_texts([query])[0]
    hits = (
        _client()
        .query_points(
            collection_name=get_settings().qdrant_collection,
            query=vector,
            limit=k,
            query_filter=Filter(
                must=[FieldCondition(key="source_id", match=MatchAny(any=source_ids))]
            ),
        )
        .points
    )
    return [
        {
            "chunk_id": h.payload["chunk_id"],
            "text": h.payload["text"],
            "page": h.payload.get("page"),
            "source_id": h.payload["source_id"],
            "score": h.score,
        }
        for h in hits
    ]


def delete_source(source_id: str) -> None:
    from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

    _client().delete(
        collection_name=get_settings().qdrant_collection,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))])
        ),
    )
