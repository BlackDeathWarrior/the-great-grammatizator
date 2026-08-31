"""PDF extraction via PyMuPDF, with an OCR fallback for scans.

Zero LLM (Invariant 1). OCR is CPU-bound and deliberately kept out of the
model-call concurrency pool (ARCHITECTURE.md §2).
"""

from __future__ import annotations

import logging

from app.ingest.errors import CorruptSource, EncryptedSource

log = logging.getLogger(__name__)

# Below this many characters we assume a scan and fall back to OCR (TC-0102).
OCR_THRESHOLD_CHARS = 200


def extract(data: bytes) -> tuple[str, str]:
    """Return (text, title). Raises IngestError subclasses on bad input."""
    import pymupdf as fitz  # `fitz` is the deprecated alias

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - library raises assorted types
        raise CorruptSource(f"Could not open PDF: {exc}") from exc

    if doc.needs_pass:
        raise EncryptedSource("This PDF is password-protected. Remove the password and re-upload.")

    try:
        pages = [page.get_text() for page in doc]
    except Exception as exc:  # noqa: BLE001
        raise CorruptSource(f"PDF is unreadable: {exc}") from exc

    text = "\n\n".join(pages).strip()
    title = (doc.metadata or {}).get("title") or ""

    if len(text) < OCR_THRESHOLD_CHARS:
        log.info("pdf extraction yielded %d chars; falling back to OCR", len(text))
        text = _ocr(doc) or text

    doc.close()
    return text, title.strip()


def _ocr(doc) -> str:
    """Rasterise each page and OCR it.

    Best-effort: a missing tesseract binary degrades to whatever the text layer
    gave us rather than failing the whole ingest.
    """
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError:  # pragma: no cover - dependency guard
        log.warning("OCR dependencies unavailable; skipping fallback")
        return ""

    out: list[str] = []
    for page in doc:
        try:
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            out.append(pytesseract.image_to_string(img))
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR failed on a page: %s", exc)
    return "\n\n".join(out).strip()
