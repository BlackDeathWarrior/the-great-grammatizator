"""Phase 7 done-criterion: reproduce worked Example A from USE-CASES.md.

    "One source, two artefacts, no re-analysis, and one honest QA catch with a
     targeted fix. A demo where everything passes first time only proves the
     pipeline runs."

The model is stubbed so the assertion is about the PIPELINE - that a tone
failure produces a specific fix note, that the note reaches the retry prompt,
and that the retry lands above threshold. Whether a real provider writes good
prose is a different question, answered by the eval harness in Phase 11.
"""

import json

import pytest

from app.agents import generator
from app.gateway import router
from app.graph import build
from app.graph.state import (
    AnalysisResult,
    ArtefactStatus,
    CheckerName,
    Chunk,
    ContentObject,
    Parameters,
    Verdict,
)

pytestmark = pytest.mark.integration


CONTENT = ContentObject(
    source_id="s_8f2a",
    source_hash="sha256:example_a",
    source_type="pdf",
    title="Critical auth bypass in NetGuard Connect Secure",
    text=(
        "A vulnerability rated 9.1 out of 10 on the CVSS scale allows an "
        "unauthenticated attacker to bypass authentication. Exploitation has been "
        "confirmed in the wild. A fix is available in version 22.7R2.6, released "
        "28 August 2026. Approximately 14,000 internet-facing instances remain "
        "affected."
    ),
    chunks=[
        Chunk(id="c1", text="rated 9.1 out of 10 on the CVSS scale", page=1, source_id="s_8f2a"),
        Chunk(
            id="c2", text="exploitation has been confirmed in the wild", page=1, source_id="s_8f2a"
        ),
        Chunk(id="c3", text="a fix is available in version 22.7R2.6", page=2, source_id="s_8f2a"),
        Chunk(
            id="c4",
            text="approximately 14,000 internet-facing instances",
            page=3,
            source_id="s_8f2a",
        ),
    ],
)

PARAMETERS = Parameters(
    audience="general public",
    tone="informative, urgent",
    language="en",
    detail="brief",
    objective="drive awareness and patching",
    style="plain, no jargon",
)

ANALYSIS = AnalysisResult(
    objective="drive awareness and patching",
    audience="general public",
    key_facts=["CVSS 9.1", "exploited in the wild", "fixed in 22.7R2.6", "14,000 affected"],
)

CLAIMS = [
    {"text": "rated 9.1 out of 10", "chunk_id": "c1"},
    {"text": "already being exploited", "chunk_id": "c2"},
    {"text": "fix is 22.7R2.6, released 28 August", "chunk_id": "c3"},
    {"text": "around 14,000 systems affected", "chunk_id": "c4"},
]

# The alarmist closing line that Example A has the tone checker catch.
ALARMIST = "Patch now or face catastrophic breach - your organisation could be next."
SOFTENED = "Exploitation is already occurring; applying the fix today is the safest course."

# Deliberately substantial. These fixtures used to be ~200-character
# illustrations, which is exactly the "too short" complaint the length floor
# now catches - a worked example should model the standard, not fall under it.
EXEC_SUMMARY = {
    "title": "Critical authentication bypass requires immediate patching",
    "bottom_line": (
        "A vulnerability in NetGuard Connect Secure is being exploited now, and "
        "any internet-facing instance running a release before 22.7R2.6 should be "
        "treated as reachable by an unauthenticated attacker until it is patched."
    ),
    "key_points": [
        "Rated 9.1 out of 10 on CVSS, the highest severity issued for this product "
        "line this year, because it requires no credentials and no user interaction.",
        "Exploitation has been confirmed in the wild, so this is a live incident "
        "rather than a theoretical weakness disclosed ahead of attacker interest.",
        "Fixed in 22.7R2.6. Earlier releases have no mitigating configuration, so "
        "upgrading is the only remedy rather than one option among several.",
        "Anyone who can reach the login page can reach administrative functions, "
        "which makes network exposure the practical measure of risk here.",
    ],
    "recommended_action": (
        "Upgrade every instance to 22.7R2.6 today. Where an immediate upgrade is "
        "impossible, restrict the portal to known addresses until it completes, and "
        "review authentication logs back to the first confirmed exploitation."
    ),
    "residual_risk": (
        "Patching closes the door but does not undo an entry made before it. "
        "Instances exposed while unpatched should be treated as potentially "
        "compromised until their logs have been reviewed for unfamiliar sessions."
    ),
    "claims": CLAIMS,
}


def _linkedin(call_to_action: str) -> dict:
    return {
        "hook": "If your organisation uses NetGuard Connect Secure, check your version today.",
        "body": (
            "A vulnerability rated 9.1 out of 10 allows attackers to bypass "
            "authentication entirely. It is already being exploited.\n\n"
            "What that means in practice: anyone who can reach your login page "
            "can reach administrative functions, without a password, without a "
            "stolen session, and without anything your users would notice. There "
            "is no configuration that mitigates it on affected releases.\n\n"
            "The fix is version 22.7R2.6. Two things are worth doing today rather "
            "than this week. Install it. Then read your authentication logs back "
            "to the first confirmed exploitation, because a patch closes the door "
            "but tells you nothing about who came through it beforehand.\n\n"
            "If an upgrade genuinely cannot happen today, restrict the portal to "
            "known addresses in the meantime. Exposure is the practical measure of "
            "risk here, and reducing it buys time that the patch will spend well."
        ),
        "call_to_action": call_to_action,
        "hashtags": ["#CyberSecurity", "#VPN", "#PatchNow"],
        "claims": CLAIMS,
    }


@pytest.fixture
def example_a_provider(monkeypatch):
    """Stub the provider to follow Example A's scripted verdicts.

    LinkedIn: tone fails at 0.61 on the first attempt with a specific note, then
    passes at 0.84 once the note has been applied. Exec summary passes clean.
    """
    state = {"linkedin_attempt": 0}

    async def fake_complete(alias, messages, **kwargs):
        prompt = messages[-1]["content"]
        system = messages[0]["content"]

        # --- QA checkers, identified by their system prompts ---
        if "verify whether a claim is supported" in system:
            return json.dumps({"claims": [{"index": i, "supported": True} for i in range(1, 5)]})

        if "rate how well" in system.lower():
            # Tone judges the artefact it is shown. The alarmist close scores
            # below threshold; the softened one scores above.
            if ALARMIST in prompt:
                return json.dumps(
                    {
                        "score": 0.61,
                        "reason": "closing line is alarmist for the stated audience",
                        "suggestion": (
                            "Soften the closing line. Keep urgency factual - reference "
                            "active exploitation rather than predicting consequences."
                        ),
                    }
                )
            return json.dumps({"score": 0.84, "reason": "urgent but factual"})

        if "policy concerns" in system:
            return json.dumps({"safe": True, "reason": "no exploit detail"})

        if "demanding editor" in system:
            # Example A is about the TONE arc, so editorial passes throughout
            # and leaves the scripted 0.61 -> 0.84 the only moving part.
            return json.dumps(
                {
                    "score": 0.85,
                    "dimensions": {
                        "specificity": 0.9,
                        "substance": 0.85,
                        "originality": 0.8,
                        "structure": 0.85,
                    },
                    "worst": "originality",
                    "suggestion": "",
                }
            )

        # --- generation ---
        if "Executive briefing" in prompt or "executive briefing" in prompt:
            return json.dumps(EXEC_SUMMARY)

        # LinkedIn: first attempt alarmist, retry softened once the note lands.
        state["linkedin_attempt"] += 1
        if "Soften the closing line" in prompt:
            return json.dumps(_linkedin(SOFTENED))
        return json.dumps(_linkedin(ALARMIST))

    monkeypatch.setattr(router, "complete", fake_complete)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)
    return state


@pytest.mark.p0
async def test_example_a_linkedin_tone_fails_then_passes_on_retry(example_a_provider):
    """TC-1102, TC-0611: tone 0.61 -> fix note -> 0.84.

    This is the demo moment. A run where everything passes first time only
    proves the pipeline executes.
    """
    artefact, message = await build.run_artefact(
        "linkedin_post", CONTENT, ANALYSIS, PARAMETERS, job_id="j_example_a"
    )

    assert artefact.status is ArtefactStatus.PASSED
    assert artefact.retry_count == 1, "should have taken exactly one retry"
    assert example_a_provider["linkedin_attempt"] == 2

    # The retry actually changed the artefact.
    assert artefact.content["call_to_action"] == SOFTENED

    tone = artefact.qa_result.by_checker(CheckerName.TONE)
    assert tone.score == 0.84

    grounding = artefact.qa_result.by_checker(CheckerName.GROUNDING)
    assert grounding.passed is True
    assert grounding.score == 1.0, "4/4 claims supported"


@pytest.mark.p0
async def test_example_a_fix_note_is_specific(example_a_provider):
    """TC-0604. "Quality insufficient" returns the same output."""
    seen: list[tuple[str, str]] = []
    original = router.complete

    async def capture(alias, messages, **kwargs):
        # Keep the system prompt so generator prompts can be told apart from
        # checker prompts - both contain the artefact text.
        seen.append((messages[0]["content"], messages[-1]["content"]))
        return await original(alias, messages, **kwargs)

    import app.gateway.router as r

    r.complete = capture
    try:
        await build.run_artefact(
            "linkedin_post", CONTENT, ANALYSIS, PARAMETERS, job_id="j_fix_note"
        )
    finally:
        r.complete = original

    generator_prompts = [user for system, user in seen if "communications writer" in system]
    retry_prompts = [p for p in generator_prompts if "RETRY" in p]
    assert retry_prompts, "the generator never received a retry prompt"

    note = retry_prompts[0]
    assert "Soften the closing line" in note
    assert "predicting consequences" in note
    # The note names what to change, not merely that something is wrong.
    assert "insufficient" not in note.lower()


@pytest.mark.p0
async def test_example_a_two_formats_share_one_analysis(example_a_provider):
    """TC-0402, TC-0408: analysis runs once and is shared.

    The whole architectural claim of the fan-out.
    """
    analyses = []

    async def counting_analysis(content, parameters, **kwargs):
        analyses.append(1)
        return ANALYSIS

    import app.agents.analysis as analysis_module

    original = analysis_module.analyse
    analysis_module.analyse = counting_analysis
    try:
        result = await build.run_job(
            "j_example_a2",
            CONTENT,
            PARAMETERS,
            ["linkedin_post", "exec_summary"],
        )
    finally:
        analysis_module.analyse = original

    assert len(analyses) == 1, "analysis must run exactly once for two formats"
    assert set(result["artefacts"]) == {"linkedin_post", "exec_summary"}
    assert result["artefacts"]["linkedin_post"].status is ArtefactStatus.PASSED
    assert result["artefacts"]["exec_summary"].status is ArtefactStatus.PASSED


@pytest.mark.p0
async def test_example_a_retry_is_isolated_to_one_artefact(example_a_provider):
    """TC-0606: the failing LinkedIn post retries alone; the summary does not."""
    result = await build.run_job(
        "j_example_a3",
        CONTENT,
        PARAMETERS,
        ["linkedin_post", "exec_summary"],
        analysis=ANALYSIS,
    )

    assert result["artefacts"]["linkedin_post"].retry_count == 1
    assert result["artefacts"]["exec_summary"].retry_count == 0


@pytest.mark.p1
async def test_example_a_exec_summary_passes_clean(example_a_provider):
    """TC-0401."""
    artefact, _ = await build.run_artefact(
        "exec_summary", CONTENT, ANALYSIS, PARAMETERS, job_id="j_exec"
    )
    assert artefact.status is ArtefactStatus.PASSED
    assert artefact.retry_count == 0
    assert artefact.qa_result.verdict is Verdict.PASS
    assert artefact.content["residual_risk"]
