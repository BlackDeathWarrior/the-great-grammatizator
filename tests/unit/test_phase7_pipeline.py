"""Phase 7: generation, the five checkers, and the verdict policy."""

import json

import pytest

from app.agents import generator
from app.agents import verdict as verdict_policy
from app.agents.qa import format_check, grounding, reuse
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

ANALYSIS = AnalysisResult(objective="drive awareness", audience="general public")


def _content() -> ContentObject:
    """Worked Example A: 4 chunks (USE-CASES.md)."""
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="pdf",
        title="Critical auth bypass",
        text=(
            "A vulnerability rated 9.1 out of 10 allows authentication bypass. "
            "Exploitation confirmed in the wild. A fix is available in 22.7R2.6. "
            "Approximately 14,000 internet-facing instances remain affected."
        ),
        chunks=[
            Chunk(id="c1", text="rated 9.1 out of 10", source_id="s_1"),
            Chunk(id="c2", text="exploitation confirmed in the wild", source_id="s_1"),
            Chunk(id="c3", text="a fix is available in 22.7R2.6", source_id="s_1"),
            Chunk(id="c4", text="approximately 14,000 instances affected", source_id="s_1"),
        ],
    )


def _linkedin(**overrides) -> dict:
    """Worked Example A's LinkedIn output shape, verbatim from USE-CASES.md."""
    body = {
        "hook": "If your organisation uses NetGuard Connect Secure, check your version today.",
        "body": "A critical authentication bypass is being exploited in the wild.",
        "call_to_action": "Upgrade to 22.7R2.6 or apply the vendor mitigation today.",
        "hashtags": ["#CyberSecurity", "#VPN", "#PatchNow"],
        "claims": [
            {"text": "rated 9.1 out of 10", "chunk_id": "c1"},
            {"text": "already being exploited", "chunk_id": "c2"},
            {"text": "fix is 22.7R2.6", "chunk_id": "c3"},
            {"text": "around 14,000 systems affected", "chunk_id": "c4"},
        ],
    }
    body.update(overrides)
    return body


def _artefact(content: dict, output_type: str = "linkedin_post", **kw) -> Artefact:
    return Artefact(
        output_type=output_type,
        content=content,
        claims=[Claim(**c) for c in content.get("claims", [])],
        **kw,
    )


# === format checker: DETERMINISTIC ==========================================


@pytest.mark.p0
def test_format_checker_makes_no_model_calls(monkeypatch):
    """TC-0503: assert zero completion calls in the format checker.

    A tweet over 280 characters is not a judgement call. Spending a model call
    here would be slower, less reliable and non-reproducible.
    """
    calls = []

    async def spy(*a, **k):
        calls.append(a)
        return ""

    monkeypatch.setattr(router, "complete", spy)
    format_check.check(registry.get("linkedin_post"), _artefact(_linkedin()))
    assert calls == []


@pytest.mark.p0
def test_tweet_over_limit_fails_deterministically():
    """TC-0504: 281 characters fails, every time, with the count named."""
    over = _artefact({"tweets": ["x" * 281], "claims": []}, "twitter_x")
    result = format_check.check(registry.get("twitter_x"), over)
    assert result.passed is False
    assert "281" in result.fix_notes[0]
    assert "280" in result.fix_notes[0]


@pytest.mark.p1
def test_tweet_at_the_limit_passes():
    at_limit = _artefact({"tweets": ["x" * 280], "claims": []}, "twitter_x")
    assert format_check.check(registry.get("twitter_x"), at_limit).passed is True


@pytest.mark.p1
def test_too_many_hashtags_fails():
    """TC-0505: 7 hashtags where the max is 5."""
    too_many = _artefact(_linkedin(hashtags=[f"#t{i}" for i in range(7)]))
    result = format_check.check(registry.get("linkedin_post"), too_many)
    assert result.passed is False
    assert "7 hashtags" in result.fix_notes[0]


@pytest.mark.p0
def test_format_fix_note_names_the_violated_constraint():
    """TC-0604: specific, not "quality insufficient"."""
    over = _artefact(_linkedin(body="x" * 4000))
    result = format_check.check(registry.get("linkedin_post"), over)
    assert result.passed is False
    note = result.fix_notes[0]
    assert "3000" in note and "Cut" in note


# === grounding ==============================================================


@pytest.mark.p0
async def test_invented_chunk_id_is_a_grounding_failure_not_a_crash(monkeypatch):
    """TC-0404: the model cites c99, which does not exist.

    Caught deterministically - no model call is needed to know c99 is absent.
    """
    calls = []

    async def spy(*a, **k):
        calls.append(a)
        return "{}"

    monkeypatch.setattr(router, "complete", spy)

    bad = _artefact(_linkedin(claims=[{"text": "invented", "chunk_id": "c99"}]))
    result = await grounding.check(bad, _content())

    assert result.passed is False
    assert "c99" in result.fix_notes[0]
    assert calls == [], "a nonexistent chunk id needs no model call"


@pytest.mark.p0
async def test_no_claims_fails_grounding(monkeypatch):
    result = await grounding.check(_artefact(_linkedin(claims=[])), _content())
    assert result.passed is False
    assert "claims" in result.fix_notes[0]


@pytest.mark.p0
async def test_unsupported_claim_is_named_in_the_fix_note(monkeypatch):
    """TC-0502: the offending claim is named, not just counted."""

    async def verdicts(*a, **k):
        return json.dumps(
            {
                "claims": [
                    {"index": 1, "supported": True},
                    {"index": 2, "supported": False, "reason": "chunk says nothing about this"},
                    {"index": 3, "supported": True},
                    {"index": 4, "supported": True},
                ]
            }
        )

    monkeypatch.setattr(router, "complete", verdicts)
    result = await grounding.check(_artefact(_linkedin()), _content())

    assert result.passed is False
    assert "already being exploited" in result.fix_notes[0]
    assert result.score == 0.75


@pytest.mark.p0
async def test_all_claims_supported_passes(monkeypatch):
    """TC-0501, worked Example A: grounding 4/4."""

    async def all_good(*a, **k):
        return json.dumps({"claims": [{"index": i, "supported": True} for i in range(1, 5)]})

    monkeypatch.setattr(router, "complete", all_good)
    result = await grounding.check(_artefact(_linkedin()), _content())
    assert result.passed is True
    assert result.score == 1.0


# === source reuse: DETERMINISTIC ============================================


@pytest.mark.p1
def test_long_verbatim_span_blocks():
    """TC-0511: a 60-word verbatim passage is reproduction, not transformation."""
    source_text = " ".join(f"word{i}" for i in range(100))
    content = ContentObject(
        source_id="s_1",
        source_hash="h",
        source_type="pdf",
        title="t",
        text=source_text,
        chunks=[Chunk(id="c1", text=source_text, source_id="s_1")],
    )
    copied = _artefact({"body": " ".join(f"word{i}" for i in range(70)), "claims": []})
    result = reuse.check(copied, content)
    assert result.passed is False
    assert "verbatim" in result.reason


@pytest.mark.p1
def test_short_quote_is_flagged_not_blocked():
    """TC-0510, worked Example B: a five-word title card is fine."""
    source_text = " ".join(f"word{i}" for i in range(100))
    content = ContentObject(
        source_id="s_1",
        source_hash="h",
        source_type="pdf",
        title="t",
        text=source_text,
        chunks=[Chunk(id="c1", text=source_text, source_id="s_1")],
    )
    quoted = _artefact(
        {"body": "Discussing " + " ".join(f"word{i}" for i in range(20)), "claims": []}
    )
    result = reuse.check(quoted, content)
    assert result.passed is True  # flagged, under the block threshold
    assert result.score >= 15


# === verdict policy =========================================================


def _qa(**checkers) -> QAResult:
    """Build a QAResult from checker -> passed/score."""
    results = []
    for name, spec in checkers.items():
        passed, score, notes = spec if isinstance(spec, tuple) else (spec, None, [])
        results.append(
            CheckerResult(checker=CheckerName(name), passed=passed, score=score, fix_notes=notes)
        )
    return QAResult(results=results)


@pytest.mark.p0
def test_safety_fail_blocks_even_when_everything_else_passes():
    """TC-0602. "Three of four passed" is meaningless when the failure is safety."""
    qa = _qa(grounding=True, format=True, tone=True, safety=False)
    updated, message = verdict_policy.apply(qa, _artefact(_linkedin()))
    assert updated.qa_result.verdict is Verdict.BLOCK
    assert updated.status is ArtefactStatus.BLOCKED
    assert message == ""  # blocking one artefact does not stop the job


@pytest.mark.p0
def test_grounding_fail_retries_with_fix_notes():
    """TC-0603."""
    qa = _qa(
        grounding=(False, None, ["Claim 2 is not supported by c2"]),
        format=True,
        tone=True,
        safety=True,
    )
    updated, _ = verdict_policy.apply(qa, _artefact(_linkedin()))
    assert updated.qa_result.verdict is Verdict.RETRY
    assert updated.retry_count == 1
    assert "not supported" in updated.qa_result.all_fix_notes()[0]


@pytest.mark.p1
def test_tone_below_threshold_retries_once_then_passes_flagged():
    """TC-0605. Tone is advisory; it must not consume the whole budget alone."""
    qa = _qa(grounding=True, format=True, tone=(False, 0.61, ["Soften the close"]), safety=True)

    first, _ = verdict_policy.apply(qa, _artefact(_linkedin()))
    assert first.qa_result.verdict is Verdict.RETRY
    assert first.tone_retried is True

    second, _ = verdict_policy.apply(qa, first)
    assert second.qa_result.verdict is Verdict.PASS_FLAGGED
    assert second.status is ArtefactStatus.PASSED_FLAGGED


@pytest.mark.p0
def test_third_qa_failure_stops_the_job_with_an_operator_message():
    """TC-0607, TC-0805: a restart instruction, not a stack trace."""
    qa = _qa(grounding=(False, None, ["still wrong"]), format=True, tone=True, safety=True)
    artefact = _artefact(_linkedin(), retry_count=3)

    updated, message = verdict_policy.apply(qa, artefact)
    assert updated.qa_result.verdict is Verdict.BLOCK
    assert "stopped" in message.lower()
    assert "linkedin_post" in message
    assert "Traceback" not in message


@pytest.mark.p0
def test_all_pass_proceeds():
    """TC-0601."""
    qa = _qa(grounding=True, format=True, tone=True, safety=True)
    updated, message = verdict_policy.apply(qa, _artefact(_linkedin()))
    assert updated.qa_result.verdict is Verdict.PASS
    assert updated.status is ArtefactStatus.PASSED
    assert message == ""


@pytest.mark.p0
def test_retry_counter_is_per_artefact():
    """TC-0606: a failing tweet retries alone; the deck is untouched."""
    qa = _qa(grounding=(False, None, ["bad"]), format=True, tone=True, safety=True)
    tweet = _artefact({"tweets": ["x"], "claims": []}, "twitter_x")
    deck = _artefact({"title": "t", "slides": [], "claims": []}, "presentation")

    updated_tweet, _ = verdict_policy.apply(qa, tweet)
    assert updated_tweet.retry_count == 1
    assert deck.retry_count == 0


@pytest.mark.p1
def test_long_reuse_span_blocks_via_the_policy():
    qa = _qa(grounding=True, format=True, tone=True, safety=True, source_reuse=False)
    updated, _ = verdict_policy.apply(qa, _artefact(_linkedin()))
    assert updated.qa_result.verdict is Verdict.BLOCK


# === generator ==============================================================


@pytest.mark.p0
async def test_malformed_json_is_a_parse_retry_not_a_qa_retry(monkeypatch):
    """TC-0405. The model failed to speak the protocol; content was never assessed."""
    attempts = []

    async def junk(alias, messages, **kwargs):
        attempts.append(alias)
        return "this is not json"

    monkeypatch.setattr(router, "complete", junk)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    with pytest.raises(generator.ParseFailure):
        await generator.generate(registry.get("linkedin_post"), _content(), ANALYSIS, Parameters())

    # It retried the PARSE, on its own budget.
    assert len(attempts) == 3  # 1 + parse_max_retries


@pytest.mark.p0
async def test_schema_violation_is_a_parse_failure(monkeypatch):
    """A missing required key is the wrong SHAPE - content was never assessable. (TC-0406)"""

    async def incomplete(alias, messages, **kwargs):
        return json.dumps({"hook": "only a hook"})

    monkeypatch.setattr(router, "complete", incomplete)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    with pytest.raises(generator.ParseFailure, match="schema violation"):
        await generator.generate(registry.get("linkedin_post"), _content(), ANALYSIS, Parameters())


@pytest.mark.p0
async def test_generation_uses_the_registry_alias(monkeypatch):
    """TC-0303, Invariant 5: the registry decides the alias, not the agent. (TC-0903)"""
    seen = []

    advisory_body = {
        "title": "Critical authentication bypass",
        "severity": "critical",
        "summary": "Authentication can be bypassed entirely.",
        "affected": ["NetGuard Connect Secure < 22.7R2.6"],
        "recommendations": ["Upgrade to 22.7R2.6."],
        "references": [],
        "claims": [{"text": "rated 9.1", "chunk_id": "c1"}],
    }

    async def capture(alias, messages, **kwargs):
        seen.append(alias)
        # Each format has its own schema; returning the wrong shape would be a
        # parse failure, not an alias test.
        return json.dumps(advisory_body if alias == "long" else _linkedin())

    monkeypatch.setattr(router, "complete", capture)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    await generator.generate(registry.get("linkedin_post"), _content(), ANALYSIS, Parameters())
    await generator.generate(registry.get("advisory"), _content(), ANALYSIS, Parameters())

    assert seen[0] == "fast"  # linkedin_post
    assert seen[1] == "long"  # advisory


@pytest.mark.p1
async def test_claims_are_extracted_onto_the_artefact(monkeypatch):
    async def ok(alias, messages, **kwargs):
        return json.dumps(_linkedin())

    monkeypatch.setattr(router, "complete", ok)
    monkeypatch.setattr(generator.cache, "get", lambda k: None)
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    artefact = await generator.generate(
        registry.get("linkedin_post"), _content(), ANALYSIS, Parameters()
    )
    assert len(artefact.claims) == 4
    assert {c.chunk_id for c in artefact.claims} == {"c1", "c2", "c3", "c4"}


@pytest.mark.p1
async def test_retry_does_not_serve_the_cached_failure(monkeypatch):
    """Fix notes would be pointless if the cache returned the failing artefact."""
    served = []

    async def ok(alias, messages, **kwargs):
        served.append(messages)
        return json.dumps(_linkedin())

    monkeypatch.setattr(router, "complete", ok)
    monkeypatch.setattr(generator.cache, "get", lambda k: json.dumps(_linkedin()))
    monkeypatch.setattr(generator.cache, "put", lambda k, v: None)

    await generator.generate(
        registry.get("linkedin_post"),
        _content(),
        ANALYSIS,
        Parameters(),
        fix_notes=["Soften the closing line"],
        attempt=1,
    )
    assert served, "a retry must call the model, not reuse cached output"
    assert "Soften the closing line" in served[0][1]["content"]
