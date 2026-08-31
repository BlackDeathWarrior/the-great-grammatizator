"""Phase 2: ingestion is deterministic and LLM-free."""

import pytest

from app.graph.state import SourceType
from app.ingest import chunk as chunker
from app.ingest import normalise
from app.ingest.errors import NoReadableContent, UnsupportedType, VideoTooLong
from app.ingest.hash import content_hash, text_hash

ADVISORY = (
    "Critical authentication bypass in NetGuard Connect Secure\n\n"
    "A vulnerability rated 9.1 out of 10 on the CVSS scale allows an "
    "unauthenticated attacker to bypass authentication entirely.\n\n"
    "The vendor has confirmed exploitation in the wild since 21 August 2026.\n\n"
    "A fix is available in version 22.7R2.6, released 28 August 2026.\n\n"
    "Approximately 14,000 internet-facing instances remain affected."
)


# --- Invariant 1: no LLM in the ingestion path ------------------------------


@pytest.mark.p0
def test_ingest_path_contains_no_completion_calls(monkeypatch):
    """TC-0117: assert zero LiteLLM completion calls during ingestion.

    The load-bearing test of Invariant 1. Any model call in extraction,
    normalisation or chunking would let the model rewrite source text, and
    grounding would then verify claims against model output rather than the
    true source.
    """
    calls = []

    import litellm

    for name in ("completion", "acompletion", "text_completion"):
        if hasattr(litellm, name):
            monkeypatch.setattr(
                litellm,
                name,
                # `n=name` binds per iteration; a bare closure would record
                # only the last patched name.
                lambda *a, n=name, **k: calls.append(n),
                raising=False,
            )

    extracted = normalise.extract(None, text=ADVISORY)
    content = normalise.build_content_object("s_test", text_hash(ADVISORY), extracted)

    assert content.chunks, "ingestion produced no chunks"
    assert calls == [], f"ingestion made model calls: {calls}"


@pytest.mark.p0
def test_source_text_is_never_model_cleaned():
    """Invariant 1: the stored text is byte-identical to what was extracted.

    Grounding checks claims against this text, so any normalisation that
    rewrites content would silently invalidate every citation.
    """
    extracted = normalise.extract(None, text=ADVISORY)
    content = normalise.build_content_object("s_test", "sha256:x", extracted)
    assert content.text == ADVISORY.strip()


# --- Invariant 2: chunk ids ------------------------------------------------


@pytest.mark.p0
def test_chunk_ids_are_stable_across_reads():
    """TC-0114: ids identical across reads; never renumbered."""
    first = chunker.split(ADVISORY, "s_test")
    second = chunker.split(ADVISORY, "s_test")
    assert [c.id for c in first] == [c.id for c in second]
    assert [c.text for c in first] == [c.text for c in second]
    assert [c.id for c in first] == [f"c{i}" for i in range(1, len(first) + 1)]


@pytest.mark.p0
def test_chunks_carry_their_source_id():
    """Retrieval filters on this payload field (TC-0101, TC-1003)."""
    chunks = chunker.split(ADVISORY, "s_abc")
    assert all(c.source_id == "s_abc" for c in chunks)


@pytest.mark.p1
def test_page_attribution():
    """Citations display a page, so chunks must map back to one."""
    # Each "page" must exceed CHUNK_SIZE_CHARS or the text never splits and
    # page 2 is unreachable - which is what the first version of this test
    # got wrong.
    pages = ["Page one content. " * 220, "Page two content. " * 220]
    text = "\n\n".join(pages)
    chunks = chunker.split(text, "s_p", chunker.page_map_from_pages(pages))
    assert chunks[0].page == 1
    assert any(c.page == 2 for c in chunks)


# --- dedup ------------------------------------------------------------------


@pytest.mark.p0
def test_identical_bytes_hash_identically():
    """TC-0109: the same file is never ingested twice."""
    assert content_hash(b"same bytes") == content_hash(b"same bytes")
    assert content_hash(b"a") != content_hash(b"b")


@pytest.mark.p1
def test_text_hash_ignores_surrounding_whitespace():
    assert text_hash("  hello  ") == text_hash("hello")


# --- rejections -------------------------------------------------------------


@pytest.mark.p1
def test_unsupported_type_lists_accepted_types():
    """TC-0113: rejection names what IS accepted."""
    with pytest.raises(UnsupportedType) as exc:
        normalise.detect_type("payload.exe")
    assert ".pdf" in exc.value.message
    assert ".docx" in exc.value.message


@pytest.mark.p0
def test_empty_source_is_rejected():
    """TC-0112: OCR returned nothing -> fail with a clear message."""
    extracted = normalise.extract(None, text="   ")
    with pytest.raises(NoReadableContent):
        normalise.build_content_object("s_empty", "sha256:e", extracted)


@pytest.mark.p0
def test_video_over_limit_raises_before_transcription(monkeypatch):
    """TC-0108: 10:01 is rejected and nothing is transcribed.

    The gate reads container metadata only; if transcription were attempted
    first, an over-limit upload would cost real compute before being refused.
    """
    from app.ingest.extract import video

    transcribed = []
    monkeypatch.setattr(video, "probe_duration", lambda p: 601.0)

    # Fail loudly if anything downstream of the gate is reached. Patching the
    # import machinery keeps this independent of whether faster-whisper is
    # installed in this environment.
    import builtins

    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        if name.startswith("faster_whisper"):
            transcribed.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)

    with pytest.raises(VideoTooLong) as exc:
        video.extract("/tmp/fake.mp4")

    assert transcribed == [], "transcription was attempted on an over-limit video"
    assert "10 min" in exc.value.message


@pytest.mark.p0
def test_video_at_boundary_is_accepted(monkeypatch):
    """TC-0107: exactly 10:00 passes the gate."""
    from app.ingest.extract import video

    monkeypatch.setattr(video, "probe_duration", lambda p: 600.0)
    assert video.check_duration("/tmp/fake.mp4") == 600.0


# --- type routing -----------------------------------------------------------


@pytest.mark.p1
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.pdf", SourceType.PDF),
        ("a.docx", SourceType.DOCX),
        ("a.html", SourceType.HTML),
        ("a.png", SourceType.IMAGE),
        ("a.mp4", SourceType.VIDEO),
        ("a.txt", SourceType.TEXT),
    ],
)
def test_type_routing(filename, expected):
    assert normalise.detect_type(filename) is expected


@pytest.mark.p1
def test_free_form_text_is_chunked():
    """TC-0115: source created from text; chunking still applies."""
    extracted = normalise.extract(None, text=ADVISORY)
    content = normalise.build_content_object("s_t", "sha256:t", extracted)
    assert content.source_type is SourceType.TEXT
    assert len(content.chunks) >= 1


@pytest.mark.p1
def test_title_is_derived_when_absent():
    extracted = normalise.extract(None, text=ADVISORY)
    content = normalise.build_content_object("s_t", "sha256:t", extracted)
    assert content.title.startswith("Critical authentication bypass")
