"""HTML / URL extraction via trafilatura. Boilerplate stripped (TC-0104)."""

from __future__ import annotations

from app.config import get_settings
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


# Hosts that resolve to the machine itself or to cloud metadata. The URL is
# operator-supplied and reaches this from both the API and the dashboard, so
# "fetch whatever you are given" would let the form read the container's own
# services (qdrant, postgres, redis) or a cloud instance's credentials.
_BLOCKED_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal", "169.254.169.254"}
)
_PRIVATE_PREFIXES = ("10.", "192.168.", "127.", "169.254.", "172.16.", "172.17.", "172.18.")


def _check_url(url: str) -> None:
    """Refuse anything that is not a public http(s) URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NoReadableContent(f"Only http and https URLs are supported, not {parsed.scheme!r}.")

    host = (parsed.hostname or "").lower()
    if not host:
        raise NoReadableContent("That URL has no host.")
    if host in _BLOCKED_HOSTS or host.startswith(_PRIVATE_PREFIXES):
        raise NoReadableContent(
            "That URL points at a private or loopback address, which this "
            "service will not fetch. Paste the page contents instead."
        )


def fetch(url: str, *, max_bytes: int = 10_000_000) -> str:
    """Download a URL for extraction.

    Kept separate from extract() so tests can supply HTML without network access.
    """
    import trafilatura

    _check_url(url)

    # trafilatura reads its timeout from this; without it a slow host holds the
    # request open indefinitely, and ingestion runs inside the HTTP request.
    from trafilatura.settings import DEFAULT_CONFIG

    config = DEFAULT_CONFIG
    try:
        config["DEFAULT"]["DOWNLOAD_TIMEOUT"] = str(get_settings().request_timeout)
    except Exception:  # noqa: BLE001 - config shape is trafilatura's, not ours
        config = None

    downloaded = trafilatura.fetch_url(url, config=config) if config else trafilatura.fetch_url(url)
    if not downloaded:
        raise NoReadableContent(f"Could not fetch {url}")
    if len(downloaded) > max_bytes:
        raise NoReadableContent(
            f"That page is larger than {max_bytes // 1_000_000} MB; refusing to ingest it."
        )
    return downloaded
