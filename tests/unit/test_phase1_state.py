"""Phase 1: state objects encode the invariants they are supposed to encode."""

import pytest
from pydantic import ValidationError

from app.graph.state import (
    AnalysisResult,
    Artefact,
    ArtefactStatus,
    CheckerName,
    CheckerResult,
    Chunk,
    Claim,
    ContentObject,
    JobStatus,
    Parameters,
    Provenance,
    QAResult,
    SourceType,
    Verdict,
    merge_artefacts,
)


def _content() -> ContentObject:
    """Worked Example A: 4 chunks, advisory (USE-CASES.md)."""
    return ContentObject(
        source_id="s_8f2a",
        source_hash="sha256:abc",
        source_type=SourceType.PDF,
        title="Critical auth bypass in NetGuard Connect Secure",
        text="full extracted text",
        chunks=[
            Chunk(id="c1", text="rated 9.1 out of 10", page=1, source_id="s_8f2a"),
            Chunk(id="c2", text="already being exploited", page=1, source_id="s_8f2a"),
            Chunk(id="c3", text="fix is 22.7R2.6", page=2, source_id="s_8f2a"),
            Chunk(id="c4", text="around 14,000 systems", page=3, source_id="s_8f2a"),
        ],
    )


# --- Invariant 2: chunks are immutable -------------------------------------


@pytest.mark.p0
def test_chunks_are_immutable():
    """Invariant 2. Agents read chunks; they never write them."""
    c = _content().chunks[0]
    with pytest.raises(ValidationError):
        c.text = "mutated"


@pytest.mark.p0
def test_content_object_is_frozen_after_ingest():
    """ARCHITECTURE.md §2: generation runs against a frozen content object."""
    content = _content()
    with pytest.raises(ValidationError):
        content.text = "mutated"


@pytest.mark.p0
def test_nonexistent_chunk_id_returns_none_not_crash():
    """TC-0404: model cites c99 -> caught as grounding failure, not an exception."""
    content = _content()
    assert content.chunk_by_id("c99") is None
    assert content.chunk_by_id("c1") is not None
    assert content.chunk_ids() == {"c1", "c2", "c3", "c4"}


# --- Invariant 8: two separate failure counters -----------------------------


@pytest.mark.p0
def test_artefact_has_three_independent_counters():
    """Invariant 8, TC-0405, TC-0609.

    Only retry_count may gate the 3-strike stop. Parse failures and provider
    errors must be tracked separately or a flaky free tier kills healthy jobs.
    """
    a = Artefact(output_type="linkedin_post")
    a.parse_retry_count += 1
    a.provider_error_count += 3
    assert a.retry_count == 0, "parse/provider failures must not touch the QA counter"


@pytest.mark.p0
def test_retry_counters_are_per_artefact():
    """TC-0608: artefact A at 2 retries; artefact B still starts at 0.

    Invariant 7 - a failing tweet must not regenerate the deck.
    """
    artefacts = {
        "linkedin_post": Artefact(output_type="linkedin_post", retry_count=2),
        "presentation": Artefact(output_type="presentation"),
    }
    assert artefacts["linkedin_post"].retry_count == 2
    assert artefacts["presentation"].retry_count == 0


# --- job lifecycle ----------------------------------------------------------


@pytest.mark.p1
def test_job_status_distinguishes_recoverable_from_permanent():
    """TC-0804. Listed as an open gap in ARCHITECTURE.md; closed here."""
    assert JobStatus.FAILED_RECOVERABLE != JobStatus.FAILED_PERMANENT
    assert JobStatus.STOPPED_QA_BUDGET.value == "stopped_qa_budget"


# --- parameters -------------------------------------------------------------


@pytest.mark.p0
def test_parameters_have_defaults():
    """TC-0201: an unset parameter means a sensible default, never an empty slot."""
    p = Parameters()
    assert all(v for v in p.model_dump().values()), "no parameter may default to empty"


@pytest.mark.p0
def test_parameter_change_changes_cache_fragment():
    """TC-0204: same source + format, tone changed -> cache MISS.

    Parameters are part of the cache key, so the fragment must differ.
    """
    base = Parameters(tone="informative")
    changed = Parameters(tone="urgent")
    assert base.cache_fragment() != changed.cache_fragment()


@pytest.mark.p1
def test_identical_parameters_produce_identical_fragment():
    """TC-0205: same everything -> cache HIT. Order of construction must not matter."""
    a = Parameters(tone="urgent", audience="general public")
    b = Parameters(audience="general public", tone="urgent")
    assert a.cache_fragment() == b.cache_fragment()


# --- provenance / commentary mode -------------------------------------------


@pytest.mark.p1
def test_copyrighted_provenance_enables_commentary_mode():
    """Worked Example B: literary_copyrighted -> paraphrase, not reproduction."""
    lit = AnalysisResult(
        objective="support close reading",
        audience="undergraduate students",
        provenance=Provenance.LITERARY_COPYRIGHTED,
    )
    plain = AnalysisResult(objective="inform", audience="general public")
    assert lit.commentary_mode is True
    assert plain.commentary_mode is False


# --- QA shapes --------------------------------------------------------------


@pytest.mark.p1
def test_tone_result_carries_score_and_reason_not_bare_boolean():
    """TC-0506: numeric score plus a reason string."""
    r = CheckerResult(
        checker=CheckerName.TONE,
        passed=False,
        score=0.61,
        reason="closing line alarmist for stated audience",
        fix_notes=["Soften the closing line. Keep urgency factual."],
    )
    assert r.score == 0.61
    assert r.reason
    assert r.fix_notes


@pytest.mark.p0
def test_fix_notes_are_collected_for_the_retry_prompt():
    """TC-0604: the retry prompt gets the specific claim or constraint."""
    qa = QAResult(
        results=[
            CheckerResult(checker=CheckerName.GROUNDING, passed=False, fix_notes=["claim 3"]),
            CheckerResult(checker=CheckerName.FORMAT, passed=True),
            CheckerResult(checker=CheckerName.TONE, passed=False, fix_notes=["soften close"]),
        ],
        verdict=Verdict.RETRY,
    )
    assert qa.all_fix_notes() == ["claim 3", "soften close"]
    assert qa.by_checker(CheckerName.FORMAT).passed is True
    assert qa.by_checker(CheckerName.SAFETY) is None


@pytest.mark.p1
def test_claims_carry_chunk_ids():
    """TC-0403: every claim cites a chunk. This is what makes grounding cheap."""
    claim = Claim(text="rated 9.1 out of 10", chunk_id="c1")
    assert claim.chunk_id in _content().chunk_ids()


# --- fan-out reducer --------------------------------------------------------


@pytest.mark.p0
def test_artefact_merge_does_not_clobber_concurrent_branches():
    """Seven generators write concurrently; each owns exactly one key (TC-0407)."""
    left = {"linkedin_post": Artefact(output_type="linkedin_post", retry_count=1)}
    right = {"twitter_x": Artefact(output_type="twitter_x", status=ArtefactStatus.PASSED)}
    merged = merge_artefacts(left, right)
    assert set(merged) == {"linkedin_post", "twitter_x"}
    assert merged["linkedin_post"].retry_count == 1
    assert merged["twitter_x"].status is ArtefactStatus.PASSED
