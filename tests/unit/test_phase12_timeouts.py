"""Timeouts and the URL fetch guard.

A hung connection used to be indistinguishable from slow work: nothing in the
real completion path passed a timeout, so the first thing to notice was arq's
job timeout 900 seconds later. preflight() always passed one, which is what
made the omission visible.
"""

import pytest

from app.config import get_settings
from app.ingest.errors import NoReadableContent
from app.ingest.extract import html

# === the model call is bounded ==============================================


@pytest.mark.p0
async def test_every_completion_carries_a_timeout(monkeypatch):
    """The kwargs reaching the provider must include one."""
    from app.gateway import router

    seen: dict = {}

    class _Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]

    class _FakeRouter:
        async def acompletion(self, **kwargs):
            seen.update(kwargs)
            return _Resp()

    monkeypatch.setattr(router, "get_router", lambda: _FakeRouter())

    await router.complete(router.FAST, [{"role": "user", "content": "hi"}])

    assert "timeout" in seen, "a completion went out with no timeout"
    assert seen["timeout"] == get_settings().request_timeout


# === the URL fetch refuses to be a proxy into the container =================


@pytest.mark.p0
@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:6333/collections",
        "http://127.0.0.1:5432/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://metadata.google.internal/",
    ],
)
def test_fetch_refuses_private_and_loopback_addresses(url):
    """The URL is operator-supplied and reaches this from the dashboard form.

    Without the guard, "ingest this URL" reads the container's own services.
    """
    with pytest.raises(NoReadableContent, match="private or loopback"):
        html.fetch(url)


@pytest.mark.p1
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_fetch_refuses_non_http_schemes(url):
    with pytest.raises(NoReadableContent, match="http and https"):
        html.fetch(url)


@pytest.mark.p1
def test_fetch_refuses_an_oversized_page(monkeypatch):
    monkeypatch.setattr("trafilatura.fetch_url", lambda *_a, **_kw: "x" * 200, raising=False)
    with pytest.raises(NoReadableContent, match="larger than"):
        html.fetch("https://example.com/big", max_bytes=100)


@pytest.mark.p1
def test_a_public_url_is_still_allowed(monkeypatch):
    monkeypatch.setattr(
        "trafilatura.fetch_url", lambda *_a, **_kw: "<html><body>ok</body></html>", raising=False
    )
    assert "ok" in html.fetch("https://example.com/advisory")


# === embeddings are batched and bounded =====================================


@pytest.mark.p1
def test_embeddings_are_sent_in_batches(monkeypatch):
    """One request per 96 chunks: a long source used to send every chunk at once."""
    from app.ingest import embed

    batches: list[int] = []

    class _FakeClient:
        def __init__(self, **kwargs):
            assert "timeout" in kwargs, "the embeddings client had no timeout"
            self.embeddings = self

        def create(self, *, model, input):
            batches.append(len(input))
            return type(
                "R", (), {"data": [type("D", (), {"embedding": [0.0] * 4})() for _ in input]}
            )()

    monkeypatch.setattr(embed, "get_settings", lambda: _settings_with_key())
    monkeypatch.setattr("openai.OpenAI", _FakeClient, raising=False)

    embed.embed_texts([f"chunk {i}" for i in range(200)])

    assert batches == [96, 96, 8], f"expected three batches, got {batches}"


def _settings_with_key():
    s = get_settings().model_copy()
    s.embedding_api_key = "sk-test"
    s.embedding_dim = 4
    return s
