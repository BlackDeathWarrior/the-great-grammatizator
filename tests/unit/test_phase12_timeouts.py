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
    # Force the OpenAI branch: embed_texts dispatches on the model name, and
    # the deployed default is now a Gemini model.
    s.embedding_model = "text-embedding-3-small"
    return s


# === semantic retrieval, and the traps around switching it on ===============


@pytest.mark.p1
def test_the_embedder_dispatches_on_the_model_name():
    """Two providers, one entry point. Gemini is chosen by name, not a flag."""
    from app.ingest import embed

    assert embed._is_gemini("gemini-embedding-001")
    assert embed._is_gemini("models/text-embedding-004")
    assert not embed._is_gemini("text-embedding-3-small")


@pytest.mark.p0
def test_a_provider_failure_falls_back_rather_than_losing_the_ingest(monkeypatch):
    """Ingestion must never depend on a provider being reachable.

    Extraction and chunking have already succeeded by this point; raising here
    would roll the whole source back and make the operator re-upload.
    """
    from app.ingest import embed

    settings = get_settings().model_copy()
    settings.embedding_api_key = "sk-test"
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dim = 8
    monkeypatch.setattr(embed, "get_settings", lambda: settings)
    monkeypatch.setattr(embed, "_DEGRADED", False)

    def boom(*_a, **_kw):
        raise RuntimeError("embeddings provider down")

    monkeypatch.setattr(embed, "_embed_openai", boom)

    vectors = embed.embed_texts(["one", "two"])

    assert len(vectors) == 2, "the ingest lost its vectors"
    assert len(vectors[0]) == 8
    # And it says so: a fallback nobody notices is how retrieval came to look
    # healthy while ranking nothing.
    assert embed.is_degraded()


@pytest.mark.p1
def test_a_vector_dimension_mismatch_is_reported(monkeypatch, caplog):
    """Changing embedding model breaks every upsert with a 400 deep in the client.

    The collection is created with a fixed vector size, so this is a real trap:
    it cost a debugging cycle when the model changed from 1536 to 768 dims.
    """
    import logging

    from app.ingest import embed

    class _Vectors:
        size = 1536

    class _Params:
        vectors = _Vectors()

    class _Config:
        params = _Params()

    class _Info:
        config = _Config()

    class _Collection:
        name = "chunks"

    class _Client:
        def get_collections(self):
            return type("R", (), {"collections": [_Collection()]})()

        def get_collection(self, _name):
            return _Info()

    settings = get_settings().model_copy()
    settings.qdrant_collection = "chunks"
    settings.embedding_dim = 768
    monkeypatch.setattr(embed, "get_settings", lambda: settings)
    monkeypatch.setattr(embed, "_client", lambda: _Client())

    with caplog.at_level(logging.ERROR):
        embed.ensure_collection()

    assert "1536" in caplog.text and "768" in caplog.text
    assert "re-ingest" in caplog.text, "the message must say what to do about it"


# === the long alias has somewhere to fall back to ===========================


@pytest.mark.p0
def test_both_aliases_have_more_than_one_deployment(monkeypatch):
    """`long` had exactly one, so a Gemini 429 crossed over to `fast`.

    Four of seven formats ask for `long`, so losing its context window to a
    routine free-tier rate limit was the likeliest way to degrade a demo.
    """
    from app.gateway import router

    settings = get_settings().model_copy()
    settings.groq_api_key = "g"
    settings.gemini_api_key = "gm"
    settings.openrouter_api_key = "or"
    monkeypatch.setattr(router, "get_settings", lambda: settings)

    built = router.build_router()
    names = [m["model_name"] for m in built.model_list]

    assert names.count(router.FAST) >= 2
    assert names.count(router.LONG) >= 2, "a 429 on long has nowhere to go"


# === top-k selection ========================================================


@pytest.mark.p0
def test_a_long_source_is_narrowed_but_chunk_ids_are_preserved(monkeypatch):
    """Invariant 2: selection changes which chunks are shown, never their ids.

    A claim citing c37 must still verify against c37.
    """
    from app.agents import generator
    from app.graph.state import AnalysisResult, Chunk, ContentObject
    from app.prompts import loader

    n = loader.MAX_PROMPT_CHUNKS + 40
    content = ContentObject(
        source_id="s_long",
        source_hash="sha256:a",
        source_type="pdf",
        title="Long report",
        text="...",
        chunks=[Chunk(id=f"c{i}", source_id="s_long", text=f"chunk {i}") for i in range(1, n + 1)],
    )
    analysis = AnalysisResult(objective="inform", audience="engineers")

    # No job bound, so it degrades to document order - the pre-existing
    # behaviour, which must never be worse than before.
    focused = generator._focus(content, analysis)

    assert len(focused.chunks) == loader.MAX_PROMPT_CHUNKS
    assert focused.chunks[0].id == "c1", "ids were renumbered"
    assert all(c.id.startswith("c") for c in focused.chunks)


@pytest.mark.p1
def test_a_short_source_is_left_alone():
    from app.agents import generator
    from app.graph.state import AnalysisResult, Chunk, ContentObject

    content = ContentObject(
        source_id="s_short",
        source_hash="sha256:a",
        source_type="text",
        title="Advisory",
        text="...",
        chunks=[Chunk(id="c1", source_id="s_short", text="only chunk")],
    )

    focused = generator._focus(content, AnalysisResult(objective="inform", audience="all"))

    assert focused is content, "a short source should not be copied at all"
