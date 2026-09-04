"""A transient provider failure must not kill one format for the whole job.

run_artefact() used to return immediately on the first ProviderError. LiteLLM
retries twice internally, so anything outliving that - a rate limit on a free
tier, a brief outage - permanently failed that artefact while the other six
succeeded. The job then reported "6 of 7 done" for a reason that had nothing
to do with quality.

Provider errors are still infrastructure: they retry with backoff on their own
counter and never touch the QA budget (Invariant 8).
"""

import pytest

from app.agents import generator
from app.agents.qa import runner
from app.gateway import router
from app.graph import build
from app.graph.state import (
    AnalysisResult,
    Artefact,
    ArtefactStatus,
    CheckerName,
    CheckerResult,
    Chunk,
    Claim,
    ContentObject,
    Parameters,
    QAResult,
)

ANALYSIS = AnalysisResult(objective="inform", audience="general public")


def _content() -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Advisory",
        text="A vulnerability rated 9.1 out of 10 allows authentication bypass.",
        chunks=[Chunk(id="c1", source_id="s_1", text="A vulnerability rated 9.1.")],
    )


def _good_artefact() -> Artefact:
    return Artefact(
        output_type="linkedin_post",
        content={"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []},
        claims=[Claim(text="rated 9.1", chunk_id="c1")],
    )


def _all_pass() -> QAResult:
    return QAResult(
        results=[
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.GROUNDING, passed=True),
            CheckerResult(checker=CheckerName.TONE, passed=True, score=0.9),
            CheckerResult(checker=CheckerName.SAFETY, passed=True),
        ]
    )


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    """Backoff is real in production; tests should not wait for it."""

    async def instant(_seconds):
        return None

    monkeypatch.setattr(build.asyncio, "sleep", instant)


@pytest.mark.p0
async def test_a_transient_provider_error_no_longer_kills_the_artefact(monkeypatch):
    """TC-0902: the fallback is exercised, not merely configured."""
    calls = {"n": 0}

    async def flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise router.ProviderError("429 rate limited")
        return _good_artefact()

    monkeypatch.setattr(generator, "generate", flaky)
    monkeypatch.setattr(runner, "run", lambda *_a, **_kw: _settled())
    monkeypatch.setattr(build, "_export", lambda *_a, **_kw: [])

    artefact, message = await build.run_artefact(
        "linkedin_post", _content(), ANALYSIS, Parameters()
    )

    assert artefact.status is ArtefactStatus.PASSED, "a transient 429 failed the artefact"
    assert calls["n"] == 2, "it did not retry"
    assert message == ""


async def _settled():
    return _all_pass()


@pytest.mark.p0
async def test_a_provider_error_never_spends_the_qa_budget(monkeypatch):
    """Invariant 8: the two counters stay separate under retry."""

    async def always_down(*_a, **_kw):
        raise router.ProviderError("provider down")

    monkeypatch.setattr(generator, "generate", always_down)

    artefact, _ = await build.run_artefact("linkedin_post", _content(), ANALYSIS, Parameters())

    assert artefact.status is ArtefactStatus.FAILED
    assert artefact.retry_count == 0, "an outage consumed the quality budget"
    assert artefact.provider_error_count == build._PROVIDER_ATTEMPTS
    assert "provider error" in artefact.error


@pytest.mark.p1
async def test_provider_retries_are_bounded(monkeypatch):
    """It must give up, not hammer a dead provider forever."""
    calls = {"n": 0}

    async def always_down(*_a, **_kw):
        calls["n"] += 1
        raise router.ProviderError("down")

    monkeypatch.setattr(generator, "generate", always_down)

    await build.run_artefact("linkedin_post", _content(), ANALYSIS, Parameters())

    assert calls["n"] == build._PROVIDER_ATTEMPTS


@pytest.mark.p1
async def test_a_provider_error_during_qa_also_retries(monkeypatch):
    """The QA leg had the same immediate-return shape as generation."""
    calls = {"n": 0}

    async def flaky_qa(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise router.ProviderError("429")
        return _all_pass()

    monkeypatch.setattr(generator, "generate", lambda *_a, **_kw: _generated())
    monkeypatch.setattr(runner, "run", flaky_qa)
    monkeypatch.setattr(build, "_export", lambda *_a, **_kw: [])

    artefact, _ = await build.run_artefact("linkedin_post", _content(), ANALYSIS, Parameters())

    assert artefact.status is ArtefactStatus.PASSED
    assert calls["n"] == 2
    assert artefact.provider_error_count == 1, "the counter must still record it"
    assert artefact.retry_count == 0


async def _generated():
    return _good_artefact()


@pytest.mark.p1
async def test_a_parse_failure_is_not_retried_as_a_provider_error(monkeypatch):
    """A malformed response is the model's fault, not the network's (TC-0405)."""
    calls = {"n": 0}

    async def malformed(*_a, **_kw):
        calls["n"] += 1
        raise generator.ParseFailure("not JSON")

    monkeypatch.setattr(generator, "generate", malformed)

    artefact, _ = await build.run_artefact("linkedin_post", _content(), ANALYSIS, Parameters())

    assert calls["n"] == 1, "a parse failure was retried on the provider counter"
    assert artefact.parse_retry_count == 1
    assert artefact.provider_error_count == 0
    assert artefact.retry_count == 0
