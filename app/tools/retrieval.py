"""Retrieval tools. Purpose-built and scoped - never a raw Qdrant client.

`search_chunks(job_id, query, k)` takes a job_id in its signature, so an agent
physically cannot reach another job's chunks: there is no argument that would
express it (ARCHITECTURE.md sec.5, TC-1003, TC-1004).

Read-only by construction. No write or delete tool is registered at all, so
output generators cannot mutate chunks (Invariant 2, TC-1002).
"""

from __future__ import annotations

import logging

from app.tools.registry import ToolGroup, tool

log = logging.getLogger(__name__)

# job_id -> the source_ids that job is permitted to read. Populated when a job
# starts; the scope of a job never widens during its run.
_JOB_SOURCES: dict[str, list[str]] = {}


def bind_job(job_id: str, source_ids: list[str]) -> None:
    """Declare which sources a job may retrieve from."""
    _JOB_SOURCES[job_id] = list(source_ids)


def unbind_job(job_id: str) -> None:
    _JOB_SOURCES.pop(job_id, None)


def bound_sources(job_id: str) -> list[str]:
    return list(_JOB_SOURCES.get(job_id, []))


@tool(
    "search_chunks",
    ToolGroup.RETRIEVAL,
    "Semantic search over the chunks of THIS job's sources only.",
)
def search_chunks(job_id: str, query: str, k: int = 5) -> list[dict]:
    """Retrieve the k most relevant chunks for a query, scoped to the job.

    An unknown or unbound job_id returns nothing rather than raising: the caller
    learns only that there is no data, never whether the job exists (TC-1003).
    """
    from app.ingest import embed

    source_ids = _JOB_SOURCES.get(job_id)
    if not source_ids:
        log.warning("search_chunks: job %s has no bound sources", job_id)
        return []
    return embed.search(source_ids, query, k=k)


@tool(
    "get_chunk",
    ToolGroup.RETRIEVAL,
    "Fetch one chunk by id, scoped to THIS job. Used by grounding to verify a "
    "cited claim against the chunk it cites.",
)
def get_chunk(job_id: str, chunk_id: str) -> dict | None:
    """Fetch a single chunk by id.

    This is what makes grounding cheap: the checker verifies a claim against the
    chunk it cites rather than searching the whole document (ARCHITECTURE.md
    sec.7). A nonexistent id returns None so the checker reports a grounding
    failure instead of crashing (TC-0404).
    """
    from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

    from app.config import get_settings
    from app.ingest.embed import _client, ensure_collection

    source_ids = _JOB_SOURCES.get(job_id)
    if not source_ids:
        return None

    ensure_collection()
    points, _ = _client().scroll(
        collection_name=get_settings().qdrant_collection,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="source_id", match=MatchAny(any=source_ids)),
                FieldCondition(key="chunk_id", match=MatchValue(value=chunk_id)),
            ]
        ),
        limit=1,
        with_payload=True,
    )
    if not points:
        return None

    payload = points[0].payload
    return {
        "chunk_id": payload["chunk_id"],
        "text": payload["text"],
        "page": payload.get("page"),
        "source_id": payload["source_id"],
    }
