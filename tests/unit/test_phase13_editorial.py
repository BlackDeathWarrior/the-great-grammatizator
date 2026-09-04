"""The editorial checker: is the writing any GOOD?

Nothing else in the QA layer asked. The first live run produced a LinkedIn
post whose body was 36 consecutive words copied from the source, kept the
jargon the operator's "plain, no jargon" style forbade, and opened with a hook
that restated the title. Format, grounding, safety and tone all passed it.

The deterministic half of this checker - the verbatim run - is tested exactly.
The model half is tested through its policy consequences, not its scores: a
test that pins a model's judgement to two decimal places tests the model, not
the code.
"""

import pytest

from app.agents import verdict as verdict_policy
from app.agents.qa import editorial
from app.config import get_settings
from app.graph.state import (
    Artefact,
    ArtefactStatus,
    CheckerName,
    CheckerResult,
    Chunk,
    Claim,
    ContentObject,
    QAResult,
    Verdict,
)

SOURCE = (
    "Critical authentication bypass in NetGuard Connect Secure. A vulnerability "
    "rated 9.1 out of 10 on the CVSS scale allows an unauthenticated attacker to "
    "bypass authentication entirely and gain administrative access. Active "
    "exploitation has been observed in the wild since 12 August."
)


def _content() -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Advisory",
        text=SOURCE,
        chunks=[Chunk(id="c1", source_id="s_1", text=SOURCE)],
    )


def _artefact(content: dict) -> Artefact:
    return Artefact(
        output_type="linkedin_post",
        content=content,
        claims=[Claim(**c) for c in content.get("claims", [])],
    )


# === the deterministic half: copying is measured, not judged ================


@pytest.mark.p0
def test_a_body_lifted_from_the_source_is_measured():
    """The exact artefact the platform shipped on its first live run."""
    lifted = _artefact(
        {
            "hook": "Critical authentication bypass vulnerability in NetGuard Connect Secure",
            "body": (
                "A vulnerability rated 9.1 out of 10 on the CVSS scale allows an "
                "unauthenticated attacker to bypass authentication entirely and "
                "gain administrative access."
            ),
            "call_to_action": "Patch immediately",
            "hashtags": [],
            "claims": [],
        }
    )

    run = editorial.verbatim_run(lifted, _content())

    assert run >= editorial._VERBATIM_FAIL_WORDS, f"only measured {run} copied words"


@pytest.mark.p0
def test_original_writing_measures_short():
    original = _artefact(
        {
            "hook": "Attackers have been walking in since 12 August. No password required.",
            "body": (
                "The flaw scores 9.1 of 10. Anyone who can reach the login page "
                "lands with admin rights. Version 22.7R2.6 closes it."
            ),
            "call_to_action": "Patch, then read your auth logs back to 12 August.",
            "hashtags": [],
            "claims": [],
        }
    )

    assert editorial.verbatim_run(original, _content()) < editorial._VERBATIM_FAIL_WORDS


@pytest.mark.p0
def test_quoting_inside_a_claim_is_not_counted_as_copying():
    """Claims quote the source by design - that is what makes them checkable.

    Counting them would punish correct citation.
    """
    cited = _artefact(
        {
            "hook": "Short hook",
            "body": "Original body text written fresh.",
            "call_to_action": "Act",
            "hashtags": [],
            "claims": [
                {
                    "text": (
                        "a vulnerability rated 9.1 out of 10 on the CVSS scale allows "
                        "an unauthenticated attacker to bypass authentication entirely "
                        "and gain administrative access"
                    ),
                    "chunk_id": "c1",
                }
            ],
        }
    )

    assert editorial.verbatim_run(cited, _content()) < editorial._VERBATIM_FAIL_WORDS


# === the checker has no tools ===============================================


@pytest.mark.p1
def test_the_editorial_checker_gets_no_tools():
    """TC-1007. It is handed the overlap figure it needs; it reaches for nothing."""
    from app.tools.registry import ALLOWLIST, Caller, available

    assert ALLOWLIST[Caller.EDITORIAL_CHECKER] == frozenset()
    assert available(Caller.EDITORIAL_CHECKER) == []


# === the verdict treats quality as binding ==================================


def _qa(editorial_passed: bool, tone_score: float = 0.9) -> QAResult:
    return QAResult(
        results=[
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.GROUNDING, passed=True),
            CheckerResult(checker=CheckerName.SAFETY, passed=True),
            CheckerResult(checker=CheckerName.TONE, passed=True, score=tone_score),
            CheckerResult(
                checker=CheckerName.EDITORIAL,
                passed=editorial_passed,
                score=0.85 if editorial_passed else 0.55,
                fix_notes=[] if editorial_passed else ["36 words copied verbatim."],
            ),
        ]
    )


@pytest.mark.p0
def test_an_editorial_failure_retries_with_the_reason():
    artefact = _artefact({"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []})
    updated, message = verdict_policy.apply(_qa(editorial_passed=False), artefact)

    assert updated.status is ArtefactStatus.QA, "a poor artefact was delivered"
    assert updated.retry_count == 1
    assert message == ""
    assert any("verbatim" in n for n in updated.qa_result.all_fix_notes())


@pytest.mark.p0
def test_an_editorial_failure_blocks_once_the_budget_is_spent():
    """Block and report honestly: it must not ship after three poor attempts."""
    artefact = _artefact({"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []})
    artefact.retry_count = get_settings().qa_max_retries

    decision, message = verdict_policy.decide(_qa(editorial_passed=False), artefact)

    assert decision is Verdict.BLOCK
    assert "linkedin_post" in message
    assert "start a new job" in message


@pytest.mark.p1
def test_good_writing_still_passes():
    artefact = _artefact({"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []})
    updated, _ = verdict_policy.apply(_qa(editorial_passed=True), artefact)

    assert updated.status is ArtefactStatus.PASSED


# === the tone floor ==========================================================


@pytest.mark.p0
def test_an_artefact_far_below_the_tone_floor_is_blocked_not_flagged():
    """Tone is advisory, so on its own it could never stop anything.

    Without a floor a 0.30 artefact shipped with a warning badge.
    """
    artefact = _artefact({"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []})
    artefact.tone_retried = True  # would otherwise pass flagged

    qa = QAResult(
        results=[
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.GROUNDING, passed=True),
            CheckerResult(checker=CheckerName.SAFETY, passed=True),
            CheckerResult(checker=CheckerName.EDITORIAL, passed=True, score=0.8),
            CheckerResult(checker=CheckerName.TONE, passed=False, score=0.30),
        ]
    )
    decision, message = verdict_policy.decide(qa, artefact)

    assert decision is Verdict.BLOCK
    assert "0.30" in message


@pytest.mark.p1
def test_a_tone_score_just_under_threshold_still_only_retries():
    """The floor must not swallow the ordinary retry-once-then-flag path."""
    settings = get_settings()
    artefact = _artefact({"hook": "h", "body": "b", "call_to_action": "c", "hashtags": []})

    qa = QAResult(
        results=[
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.GROUNDING, passed=True),
            CheckerResult(checker=CheckerName.SAFETY, passed=True),
            CheckerResult(checker=CheckerName.EDITORIAL, passed=True, score=0.8),
            CheckerResult(
                checker=CheckerName.TONE, passed=False, score=settings.tone_threshold - 0.02
            ),
        ]
    )
    decision, _ = verdict_policy.decide(qa, artefact)

    assert decision is Verdict.RETRY


# === thresholds are configurable, not hardcoded =============================


@pytest.mark.p1
def test_the_quality_thresholds_are_settings():
    """A demo must be able to loosen these without a code change."""
    s = get_settings()
    assert 0.0 < s.editorial_threshold <= 1.0
    assert 0.0 < s.tone_floor < s.tone_threshold <= 1.0


# === the generator is told how to avoid failing ==============================


@pytest.mark.p1
def test_the_shared_prompt_carries_an_editorial_contract():
    """Quality failures are cheaper to prevent than to retry."""
    import pathlib

    shared = (
        pathlib.Path(editorial.__file__).parents[2] / "prompts" / "templates" / "_shared.jinja"
    ).read_text(encoding="utf-8")

    assert "EDITORIAL CONTRACT" in shared
    assert "Transform, do not transcribe" in shared
    # The banned openers must be named. "Avoid cliches" is not actionable.
    assert "rapidly evolving landscape" in shared
    # And a worked weak-vs-strong pair, the trick that fixed the claims bug.
    assert "Weak, because" in shared and "Strong, because" in shared
