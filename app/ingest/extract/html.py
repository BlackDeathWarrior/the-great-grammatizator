"""HTML / URL extraction via trafilatura. Boilerplate stripped (TC-0104)."""

from __future__ import annotations

from app.ingest.errors import NoReadableContent


def extract(data: bytes | str, url: str | None = None) -> tuple[str, str]:
    """Return (text, title)."""
    import trafilatura

    raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

    text = trafilatura.extract(raw, include_comments=False, include_tables=True, url=url)
    if not text or not text.strip():
        raise NoReadableContent("No article body found; the page may be a redirect or paywall.")

    title = ""
    try:
        meta = trafilatura.extract_metadata(raw)
        if meta and meta.title:
            title = meta.title
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass

    return text.strip(), title.strip()


def fetch(url: str) -> str:
    """Download a URL for extraction.

    Kept separate from extract() so tests can supply HTML without network access.
    """
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise NoReadableContent(f"Could not fetch {url}")
    return downloaded
