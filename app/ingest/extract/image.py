"""Image extraction: OCR text plus a caption. Original retained as an attachment.

The caption is deliberately deterministic here. ARCHITECTURE.md §2 lists
"vision caption + OCR", but Invariant 1 forbids any LLM in the ingestion path,
and a vision caption is a model call. Resolution: OCR runs at ingest (cheap,
deterministic, CPU-bound), and any richer visual description is left to the
analysis layer, which is allowed to call models and reads media[] from the
frozen content object.
"""

from __future__ import annotations

import io
import logging

from app.ingest.errors import CorruptSource

log = logging.getLogger(__name__)


def extract(data: bytes) -> tuple[str, str, str]:
    """Return (text, title, caption) where text is OCR output (TC-0105)."""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise CorruptSource("Image support unavailable") from exc

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise CorruptSource(f"Could not open image: {exc}") from exc

    caption = f"{img.format or 'image'} {img.width}x{img.height}"

    ocr_text = ""
    try:
        import pytesseract

        ocr_text = pytesseract.image_to_string(img).strip()
    except ImportError:  # pragma: no cover
        log.warning("pytesseract unavailable; image contributes no text")
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR failed on image: %s", exc)

    return ocr_text, "", caption
