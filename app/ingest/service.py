"""Ingestion service: the full deterministic path, transactional.

Order matters. The duration gate and extraction run BEFORE any row is written,
so a rejected upload leaves no partial source (TC-0108, TC-0110).
"""

from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Source
from app.graph.state import ContentObject, SourceType
from app.ingest import embed, normalise
from app.ingest.errors import VideoTooLong

log = logging.getLogger(__name__)


def find_by_hash(session: Session, source_hash: str) -> Source | None:
    return session.query(Source).filter_by(source_hash=source_hash).one_or_none()


def to_content_object(row: Source) -> ContentObject:
    """Rebuild the frozen content object from a persisted source.

    Chunk ids come back exactly as stored - never renumbered (TC-0114).
    """
    from app.graph.state import Chunk, Media

    return ContentObject(
        source_id=row.id,
        source_hash=row.source_hash,
        source_type=SourceType(row.source_type),
        title=row.title,
        text=row.text,
        chunks=[Chunk(**c) for c in (row.chunks or [])],
        media=[Media(**m) for m in (row.media or [])],
    )


def ingest(
    session: Session,
    *,
    data: bytes | None = None,
    filename: str = "",
    text: str | None = None,
    url: str | None = None,
) -> tuple[ContentObject, bool]:
    """Run the pipeline. Returns (content, reused).

    reused=True means the hash matched an existing source and nothing was
    re-extracted or re-embedded (TC-0109).
    """
    source_hash = normalise.compute_hash(data, text)

    existing = find_by_hash(session, source_hash)
    if existing is not None:
        log.info("duplicate upload; reusing source %s", existing.id)
        return to_content_object(existing), True

    source_id = "s_" + uuid.uuid4().hex[:8]
    stype = None if text is not None else normalise.detect_type(filename)

    # Video needs a real file for ffprobe/whisper. Stage it, gate it, and only
    # keep it if the gate passes.
    path: str | None = None
    if stype is SourceType.VIDEO and data is not None:
        path = _stage(data, filename, source_id)
        try:
            from app.ingest.extract import video

            video.check_duration(path)  # raises before any transcription work
        except VideoTooLong:
            os.unlink(path)  # no partial artefacts of a rejected upload
            raise

    extracted = normalise.extract(
        data, filename=filename, source_type=stype, path=path, url=url, text=text
    )
    content = normalise.build_content_object(source_id, source_hash, extracted, filename=filename)

    if path is None and data is not None:
        path = _stage(data, filename, source_id)

    row = Source(
        id=content.source_id,
        source_hash=content.source_hash,
        source_type=content.source_type,
        title=content.title,
        text=content.text,
        chunks=[c.model_dump() for c in content.chunks],
        media=[m.model_dump() for m in content.media],
        storage_path=path,
    )
    session.add(row)
    session.flush()

    written = embed.upsert_chunks(content.chunks)
    log.info("ingested %s: %d chunks embedded", content.source_id, written)
    return content, False


def _stage(data: bytes, filename: str, source_id: str) -> str:
    """Persist the original bytes so re-render and attachments keep working."""
    root = get_settings().storage_dir
    os.makedirs(root, exist_ok=True)
    ext = os.path.splitext(filename or "")[1].lower()
    path = os.path.join(root, f"{source_id}{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    return path
