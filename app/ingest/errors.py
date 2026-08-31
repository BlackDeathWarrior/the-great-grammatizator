"""Ingestion failures. Typed so the API returns a specific message, never a 500.

Bad uploads must fail in ~2s rather than mid-generation after spending tokens
(ARCHITECTURE.md §2).
"""

from __future__ import annotations


class IngestError(Exception):
    """Base. Carries an operator-facing message, not a stack trace."""

    http_status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UnsupportedType(IngestError):
    """TC-0113: reject listing the accepted types."""

    def __init__(self, given: str, accepted: list[str]):
        super().__init__(
            f"Unsupported file type {given!r}. Accepted: {', '.join(sorted(accepted))}."
        )
        self.given = given
        self.accepted = accepted


class CorruptSource(IngestError):
    """TC-0110: truncated/unreadable file."""


class EncryptedSource(IngestError):
    """TC-0111: password-protected PDF gets a specific message, not a generic 500."""


class NoReadableContent(IngestError):
    """TC-0112: extraction and OCR both yielded nothing."""

    def __init__(self, message: str = "No readable content found in the source."):
        super().__init__(message)


class VideoTooLong(IngestError):
    """TC-0108: over the 10-minute limit.

    Blocked with a paywall stub. No transcription is attempted and no partial
    source row is written.
    """

    http_status = 402  # Payment Required - the paywall stub

    def __init__(self, seconds: float, limit: int):
        super().__init__(
            f"Video is {seconds / 60:.1f} min; the limit is {limit // 60} min. "
            "Upgrade to process longer video."
        )
        self.seconds = seconds
        self.limit = limit
