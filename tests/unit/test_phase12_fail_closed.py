"""A checker that could not run must never report a pass.

Before this, `runner.guarded()` caught every non-provider exception and
returned `passed=True`. Because `verdict.decide()` blocks only when safety
reports `passed=False`, any bug inside the safety checker - a bad response
shape, a template error, a ValueError parsing a score - silently deleted the
hard safety gate. Nothing failed, no test caught it, and the only trace was a
WARNING log.

The rule now: hard checkers (grounding, safety) fail CLOSED and are reported
as unverified; tone stays advisory and fails open, because it cannot block on
its own and a false retry is the only thing failing it closed would buy.
"""

import pytest

from app.agents import verdict as verdict_policy
from app.agents.qa import grounding, runner, safety, tone
from app.formats import registry
from app.gateway import router
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
    Verdict,
)

ANALYSIS = AnalysisResult(objective="inform", audience="general public")


def _content() -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Advisory",
        text="A vulnerability rated 9.1 out of 10 allows authentication bypass.",
        chunks=[Chunk(id="c1", source_id="s_1", text="A vulnerability rated 9.1 out of 10.")],
    )


def _artefact(**kw) -> Artefact:
    return Artefact(
        output_type="linkedin_post",
        content={"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []},
        claims=[Claim(text="rated 9.1", chunk_id="c1")],
        **kw,
    )


# === the runner: hard checkers fail closed ==================================


@pytest.mark.p0
async def test_a_crashing_safety_checker_does_not_report_a_pass(monkeypatch):
    """The original defect: any bug in safety.check made every artefact safe."""

    async def boom(*_a, **_kw):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(safety, "check", boom)

    qa = await runner.run(
        registry.get("linkedin_post"), _artefact(), _content(), Parameters(), ANALYSIS
    )

    result = qa.by_checker(CheckerName.SAFETY)
    assert result is not None
    assert result.passed is False, "a crashed safety checker reported a pass"
    assert result.checker_error is True
    assert "template exploded" in result.reason


@pytest.mark.p0
async def test_a_crashing_grounding_checker_does_not_report_a_pass(monkeypatch):
    async def boom(*_a, **_kw):
        raise ValueError("bad shape")

    monkeypatch.setattr(grounding, "check", boom)

    qa = await runner.run(
        registry.get("linkedin_post"), _artefact(), _content(), Parameters(), ANALYSIS
    )

    result = qa.by_checker(CheckerName.GROUNDING)
    assert result.passed is False
    assert result.checker_error is True


@pytest.mark.p1
async def test_tone_still_fails_open_because_it_is_advisory(monkeypatch):
    """Tone can never block on its own, so failing it closed buys only false retries."""

    async def boom(*_a, **_kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(tone, "check", boom)

    qa = await runner.run(
        registry.get("linkedin_post"), _artefact(), _content(), Parameters(), ANALYSIS
    )

    result = qa.by_checker(CheckerName.TONE)
    assert result.passed is True
    assert result.checker_error is True, "still recorded, just not blocking"


@pytest.mark.p0
async def test_a_provider_error_is_still_infrastructure_not_a_verdict(monkeypatch):
    """TC-0609: ProviderError must propagate, not become a failed checker."""

    async def rate_limited(*_a, **_kw):
        raise router.ProviderError("429")

    monkeypatch.setattr(safety, "check", rate_limited)

    with pytest.raises(router.ProviderError):
        await runner.run(
            registry.get("linkedin_post"), _artefact(), _content(), Parameters(), ANALYSIS
        )


# === the verdict: unverified is blocked, and says so ========================


@pytest.mark.p0
def test_an_unverified_artefact_is_blocked_not_delivered():
    qa = QAResult(
        results=[
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.GROUNDING, passed=True),
            CheckerResult(
                checker=CheckerName.SAFETY,
                passed=False,
                checker_error=True,
                reason="checker unavailable: boom",
            ),
        ]
    )
    decision, message = verdict_policy.decide(qa, _artefact())

    assert decision is Verdict.BLOCK
    assert "safety" in message
    assert "never verified" in message
    assert "Traceback" not in message


@pytest.mark.p0
def test_an_unverified_artefact_does_not_spend_the_qa_budget():
    """A crashed checker crashes again. Retrying it burns the budget for nothing."""
    qa = QAResult(
        results=[
            CheckerResult(
                checker=CheckerName.GROUNDING,
                passed=False,
                checker_error=True,
                reason="checker unavailable: bad shape",
            ),
        ]
    )
    artefact = _artefact()
    updated, _ = verdict_policy.apply(qa, artefact)

    assert updated.status is ArtefactStatus.BLOCKED
    assert updated.retry_count == 0, "an infrastructure fault spent a quality retry"


@pytest.mark.p1
def test_a_genuine_quality_failure_still_retries():
    """The new rule must not swallow ordinary grounding failures."""
    qa = QAResult(
        results=[
            CheckerResult(
                checker=CheckerName.GROUNDING,
                passed=False,
                reason="claim unsupported",
                fix_notes=["Remove the claim about 14,000 instances."],
            ),
        ]
    )
    updated, message = verdict_policy.apply(qa, _artefact())

    assert updated.status is ArtefactStatus.QA
    assert updated.retry_count == 1
    assert message == ""


# === the parsers no longer default to a pass ================================


@pytest.mark.p0
def test_safety_refuses_to_read_junk_as_safe():
    with pytest.raises(ValueError, match="non-JSON"):
        safety._parse("the model wrote prose instead")


@pytest.mark.p0
async def test_safety_refuses_a_response_with_no_verdict(monkeypatch):
    """Valid JSON, but missing the one key the checker exists to produce."""

    async def no_verdict(*_a, **_kw):
        return '{"reason": "looks fine"}'

    monkeypatch.setattr(safety.router, "complete", no_verdict)

    with pytest.raises(ValueError, match="'safe' key"):
        await safety.check(_artefact())


@pytest.mark.p0
def test_grounding_refuses_to_read_junk_as_fully_supported():
    with pytest.raises(ValueError, match="non-JSON"):
        grounding._parse("I could not evaluate these claims.")


@pytest.mark.p1
def test_tone_treats_a_non_numeric_score_as_no_opinion():
    """Advisory: it degrades to a pass, but deliberately and locally."""
    assert tone._parse('{"score": "high"}') == {"score": "high"}


# === the QA semaphore must survive more than one event loop =================


@pytest.mark.p0
def test_the_qa_semaphore_is_not_bound_to_one_event_loop():
    """A module-global Semaphore binds to the first loop that awaits it.

    Found by running this file's own tests together: the second loop raised
    "bound to a different event loop" from inside guarded(), which the new
    fail-closed path then reported as every checker erroring at once. In
    production the same shape appears when the API process and the worker
    share the module, or when a worker's loop is replaced after a restart.
    """
    import asyncio

    async def acquire_and_release():
        sem = router.qa_semaphore()
        async with sem:
            return True

    for _ in range(2):
        loop = asyncio.new_event_loop()
        try:
            assert loop.run_until_complete(acquire_and_release()) is True
        finally:
            loop.close()


@pytest.mark.p1
def test_a_provider_error_does_not_leak_a_semaphore_slot():
    """The failing run left the semaphore [locked] with nothing holding it."""
    import asyncio

    async def scenario():
        sem = router.qa_semaphore()
        try:
            async with sem:
                raise router.ProviderError("429")
        except router.ProviderError:
            pass
        return sem

    loop = asyncio.new_event_loop()
    try:
        sem = loop.run_until_complete(scenario())
        assert not sem.locked(), "an error inside the cap leaked a slot"
    finally:
        loop.close()
