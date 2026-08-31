"""The ingestion pipeline: hash -> extract -> normalise -> chunk -> embed.

Invariant 1: no LLM anywhere in this path. Extract, normalise, chunk and embed
are plain code. Never model-clean source text - grounding must check against the
true source (TC-0117).
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass, field

from app.graph.state import Chunk, ContentObject, Media, SourceType
from app.ingest import chunk as chunker
from app.ingest.errors import NoReadableContent, UnsupportedType
from app.ingest.hash import content_hash, text_hash

log = logging.getLogger(__name__)

EXTENSION_TYPES: dict[str, SourceType] = {
    ".pdf": SourceType.PDF,
    ".docx": SourceType.DOCX,
    ".html": SourceType.HTML,
    ".htm": SourceType.HTML,
    ".txt": SourceType.TEXT,
    ".md": SourceType.TEXT,
    ".png": SourceType.IMAGE,
    ".jpg": SourceType.IMAGE,
    ".jpeg": SourceType.IMAGE,
    ".webp": SourceType.IMAGE,
    ".gif": SourceType.IMAGE,
    ".mp4": SourceType.VIDEO,
    ".mov": SourceType.VIDEO,
    ".mkv": SourceType.VIDEO,
    ".webm": SourceType.VIDEO,
    ".m4v": SourceType.VIDEO,
}

ACCEPTED = sorted(EXTENSION_TYPES)

# Minimum characters for a source to be considered usable at all (TC-0112).
MIN_USABLE_CHARS = 20


@dataclass
class Extracted:
    """Intermediate between extraction and the frozen ContentObject."""

    text: str
    title: str = ""
    source_type: SourceType = SourceType.TEXT
    media: list[Media] = field(default_factory=list)
    page_map: list[tuple[int, int]] | None = None


def detect_type(filename: str, declared: str | None = None) -> SourceType:
    """Route by extension, falling back to mimetype (TC-0113)."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in EXTENSION_TYPES:
        return EXTENSION_TYPES[ext]

    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        if guessed.startswith("image/"):
            return SourceType.IMAGE
        if guessed.startswith("video/"):
            return SourceType.VIDEO
        if guessed == "text/html":
            return SourceType.HTML
        if guessed.startswith("text/"):
            return SourceType.TEXT

    raise UnsupportedType(ext or (declared or "unknown"), ACCEPTED)


def extract(
    data: bytes | None,
    *,
    filename: str = "",
    source_type: SourceType | None = None,
    path: str | None = None,
    url: str | None = None,
    text: str | None = None,
) -> Extracted:
    """Dispatch to the right extractor. No model calls anywhere below here."""
    if text is not None:
        # Free-form prompt source; chunking still applies (TC-0115).
        return Extracted(text=text.strip(), source_type=SourceType.TEXT)

    stype = source_type or detect_type(filename)

    if stype is SourceType.PDF:
        from app.ingest.extract import pdf

        body, title = pdf.extract(data or b"")
        return Extracted(text=body, title=title, source_type=stype)

    if stype is SourceType.DOCX:
        from app.ingest.extract import docx

        body, title = docx.extract(data or b"")
        return Extracted(text=body, title=title, source_type=stype)

    if stype is SourceType.HTML:
        from app.ingest.extract import html

        body, title = html.extract(data or b"", url=url)
        return Extracted(text=body, title=title, source_type=stype)

    if stype is SourceType.IMAGE:
        from app.ingest.extract import image

        ocr_text, title, caption = image.extract(data or b"")
        # The original is retained as an attachment (TC-0105).
        media = [Media(kind="image", path=path or filename, caption=caption, ocr_text=ocr_text)]
        return Extracted(text=ocr_text, title=title, source_type=stype, media=media)

    if stype is SourceType.VIDEO:
        from app.ingest.extract import video

        if not path:
            raise NoReadableContent("Video ingestion requires a file on disk.")
        transcript, title = video.extract(path)
        media = [Media(kind="video", path=path)]
        return Extracted(text=transcript, title=title, source_type=stype, media=media)

    body = (data or b"").decode("utf-8", errors="replace")
    return Extracted(text=body.strip(), source_type=SourceType.TEXT)


def build_content_object(
    source_id: str,
    source_hash: str,
    extracted: Extracted,
    *,
    filename: str = "",
) -> ContentObject:
    """Assemble the frozen content object - the only thing downstream reads."""
    text = (extracted.text or "").strip()
    if len(text) < MIN_USABLE_CHARS and not extracted.media:
        raise NoReadableContent()

    chunks: list[Chunk] = chunker.split(text, source_id, extracted.page_map)
    if not chunks and not extracted.media:
        raise NoReadableContent()

    title = extracted.title or _derive_title(text, filename)
    return ContentObject(
        source_id=source_id,
        source_hash=source_hash,
        source_type=extracted.source_type,
        title=title,
        text=text,
        chunks=chunks,
        media=extracted.media,
    )


def _derive_title(text: str, filename: str) -> str:
    """First non-trivial line, else the filename."""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if len(line) > 8:
            return line[:200]
    return os.path.basename(filename) or "Untitled source"


def compute_hash(data: bytes | None, text: str | None) -> str:
    return text_hash(text) if data is None and text is not None else content_hash(data or b"")
