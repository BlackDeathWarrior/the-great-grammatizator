"""Content hashing. Enables source reuse: the same file is never ingested twice."""

from __future__ import annotations

import hashlib


def content_hash(data: bytes) -> str:
    """Stable sha256 over raw bytes.

    Hashing the bytes rather than the extracted text is deliberate: extraction
    is version-dependent, so text-hashing would miss dedup after a library
    upgrade (TC-0109).
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def text_hash(text: str) -> str:
    """For free-form prompt sources, which have no file bytes (TC-0115)."""
    return "sha256:" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
