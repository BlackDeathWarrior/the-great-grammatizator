"""POST /sources - deterministic ingestion, no LLM (ARCHITECTURE.md §2).

Bad uploads fail here in ~2s rather than mid-generation after spending tokens.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.db.models import Source
from app.db.session import session_scope
from app.ingest import service
from app.ingest.errors import IngestError

log = logging.getLogger(__name__)
router = APIRouter(tags=["sources"])


class SourceOut(BaseModel):
    source_id: str
    source_hash: str
    source_type: str
    title: str
    chunk_count: int
    reused: bool
    status: str = "ingested"


class SourceSummary(BaseModel):
    source_id: str
    title: str
    source_type: str
    chunk_count: int
    created_at: str


@router.post("/sources", response_model=SourceOut, status_code=201)
async def create_source(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    url: str | None = Form(default=None),
) -> SourceOut:
    """Three input modes: file upload, pasted URL, or free-form text (UC-01)."""
    if file is None and not text and not url:
        raise HTTPException(400, "Provide a file, a url, or text.")

    data: bytes | None = None
    filename = ""
    if file is not None:
        data = await file.read()
        filename = file.filename or ""
        if not data:
            raise HTTPException(400, "Uploaded file is empty.")
    elif url:
        from app.ingest.extract import html

        try:
            data = html.fetch(url).encode("utf-8")
        except IngestError as exc:
            raise HTTPException(exc.http_status, exc.message) from exc
        filename = "fetched.html"

    try:
        with session_scope() as session:
            content, reused = service.ingest(
                session, data=data, filename=filename, text=text, url=url
            )
            return SourceOut(
                source_id=content.source_id,
                source_hash=content.source_hash,
                source_type=content.source_type.value,
                title=content.title,
                chunk_count=len(content.chunks),
                reused=reused,
            )
    except IngestError as exc:
        # Specific message, never a generic 500 (TC-0110, TC-0111, TC-0113).
        raise HTTPException(exc.http_status, exc.message) from exc


@router.get("/sources", response_model=list[SourceSummary])
async def list_sources(limit: int = 20) -> list[SourceSummary]:
    """Recent-sources list for the dashboard (UC-12, TC-0807)."""
    with session_scope() as session:
        rows = session.query(Source).order_by(Source.created_at.desc()).limit(limit).all()
        return [
            SourceSummary(
                source_id=r.id,
                title=r.title,
                source_type=str(r.source_type),
                chunk_count=len(r.chunks or []),
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
