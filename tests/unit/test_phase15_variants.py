"""Human approval: several takes, a choice, and a preference that lasts.

A single draft asks "is this acceptable?", which the operator almost always
answers yes to because they have nothing to compare against. Several takes ask
"which of these?", which is answerable immediately and which teaches something
durable.

The learning is deliberately legible rather than statistical: notes are
sentences, carry the evidence they were derived from, and can be deleted one at
a time. A preference the operator cannot read is one they cannot correct.
"""

import pytest

from app.agents import preferences
from app.graph import variants
from app.graph.state import ArtefactStatus

# === the takes are genuinely different ======================================


@pytest.mark.p0
def test_variants_are_opposed_not_variations():
    """Three hedged drafts are indistinguishable and teach nothing.

    Each angle must commit to something a different angle would not do.
    """
    chosen = variants.approaches(3)

    assert len(chosen) == 3
    names = [n for n, _ in chosen]
    assert len(set(names)) == 3, "the angles repeat"
    instructions = [i for _, i in chosen]
    assert len(set(instructions)) == 3


@pytest.mark.p1
def test_the_count_is_clamped_to_something_useful():
    """One is not a choice; five is a survey nobody finishes."""
    assert len(variants.approaches(1)) == variants.MIN_VARIANTS
    assert len(variants.approaches(99)) == variants.MAX_VARIANTS
    assert len(variants.approaches(0)) == variants.MIN_VARIANTS


@pytest.mark.p0
def test_only_variants_that_passed_qa_are_offered():
    """The choice must be between publishable drafts, not a good one and two
    strawmen. A failing variant is dropped, and the UI says how many."""
    records = [
        {"label": "A", "status": str(ArtefactStatus.PASSED)},
        {"label": "B", "status": str(ArtefactStatus.PASSED_FLAGGED)},
        {"label": "C", "status": str(ArtefactStatus.BLOCKED)},
        {"label": "D", "status": str(ArtefactStatus.FAILED)},
    ]

    offered = variants.offerable(records)

    assert [r["label"] for r in offered] == ["A", "B"]


@pytest.mark.p0
async def test_one_failing_variant_does_not_lose_the_others(monkeypatch):
    """return_exceptions: a crash in one angle must not cost the whole set."""
    from app.graph import build
    from app.graph.state import AnalysisResult, Artefact, ContentObject, Parameters

    calls = {"n": 0}

    async def flaky(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("this angle exploded")
        return Artefact(output_type="linkedin_post", status=ArtefactStatus.PASSED), ""

    monkeypatch.setattr(build, "run_artefact", flaky)

    content = ContentObject(source_id="s", source_hash="h", source_type="text", title="t", text="b")
    records = await variants.run(
        "linkedin_post",
        content,
        AnalysisResult(objective="inform", audience="all"),
        Parameters(),
        count=3,
    )

    assert len(records) == 3, "a failing angle removed a row instead of marking it"
    failed = [r for r in records if r["status"] == str(ArtefactStatus.FAILED)]
    assert len(failed) == 1
    assert "exploded" in failed[0]["error"], "the failure was hidden"


# === the choice teaches something reusable ==================================


@pytest.mark.p0
def test_a_note_records_the_evidence_it_came_from():
    """An operator who disagrees needs to see what the note was derived FROM."""
    merged = preferences.merge_note(
        [],
        "Open with the concrete figure, not a rhetorical question.",
        {"output_type": "linkedin_post", "chosen": "data-led", "rejected": ["narrative"]},
    )

    assert len(merged) == 1
    assert merged[0]["evidence"]["chosen"] == "data-led"
    assert merged[0]["evidence"]["rejected"] == ["narrative"]


@pytest.mark.p0
def test_the_same_preference_twice_replaces_rather_than_accumulates():
    """Five variations on one preference would crowd out everything else."""
    notes = preferences.merge_note([], "Open with the concrete figure, not a question.", {})
    notes = preferences.merge_note(notes, "Open with the concrete figure, not a question.", {})

    assert len(notes) == 1


@pytest.mark.p1
def test_notes_stay_bounded():
    """Taste changes, and an unbounded pile would contradict itself."""
    notes: list[dict] = []
    for i in range(preferences.MAX_NOTES + 6):
        notes = preferences.merge_note(notes, f"Distinct preference number {i} about style", {})

    assert len(notes) <= preferences.MAX_NOTES
    # Newest survive: a preference from twenty jobs ago is not current taste.
    assert str(preferences.MAX_NOTES + 5) in notes[-1]["note"]


@pytest.mark.p1
def test_an_empty_note_is_not_recorded():
    """The model returns "" when a choice teaches nothing general.

    An invented rule is worse than no rule: it steers every future job.
    """
    assert preferences.merge_note([], "", {}) == []
    assert preferences.merge_note([], "   ", {}) == []


@pytest.mark.p1
def test_a_note_is_one_instruction_not_a_paragraph():
    notes = preferences.merge_note([], "x" * 900, {})

    assert len(notes[0]["note"]) <= preferences.MAX_NOTE_CHARS


@pytest.mark.p0
async def test_a_choice_with_nothing_to_compare_teaches_nothing(monkeypatch):
    """No rejected options means no signal, and no model call to find one."""
    called = {"n": 0}

    async def counting(*_a, **_kw):
        called["n"] += 1
        return '{"note": "something"}'

    monkeypatch.setattr(preferences.router, "complete", counting)

    note = await preferences.derive_note(
        output_type="linkedin_post", chosen_approach="data-led", rejected_approaches=[]
    )

    assert note == ""
    assert called["n"] == 0, "it asked a model about a choice with no alternatives"


@pytest.mark.p1
async def test_a_provider_failure_loses_the_lesson_not_the_choice(monkeypatch):
    """The choice is already recorded by the time this runs."""

    async def down(*_a, **_kw):
        raise preferences.router.ProviderError("429")

    monkeypatch.setattr(preferences.router, "complete", down)

    note = await preferences.derive_note(
        output_type="linkedin_post",
        chosen_approach="data-led",
        rejected_approaches=["narrative"],
    )

    assert note == ""


# === what is learned reaches the next draft =================================


def _prompt(**extra) -> str:
    from app.formats import registry
    from app.graph.state import AnalysisResult, Chunk, ContentObject, Parameters
    from app.prompts import loader

    spec = registry.get("linkedin_post")
    content = ContentObject(
        source_id="s",
        source_hash="h",
        source_type="text",
        title="t",
        text="body",
        chunks=[Chunk(id="c1", source_id="s", text="body")],
    )
    return loader.render(
        spec.prompt_template,
        content=content,
        analysis=AnalysisResult(objective="inform", audience="all"),
        parameters=Parameters(),
        constraints=spec.constraints,
        fix_notes=[],
        **extra,
    )


@pytest.mark.p0
def test_learned_notes_reach_the_generator_prompt():
    """Otherwise this is a survey, not a preference system."""
    rendered = _prompt(
        style_notes=["Open with the concrete figure, not a rhetorical question."],
        approach="",
    )

    assert "WHAT THIS OPERATOR PREFERS" in rendered
    assert "Open with the concrete figure" in rendered
    # The brief must still win: notes are habits, the parameters are this job.
    assert "the parameters win" in rendered


@pytest.mark.p0
def test_the_angle_reaches_the_prompt_and_asks_for_commitment():
    rendered = _prompt(style_notes=[], approach="Open with the single most concrete fact.")

    assert "YOUR ANGLE FOR THIS DRAFT" in rendered
    assert "commit to this one" in rendered


@pytest.mark.p0
def test_two_variants_cannot_be_served_the_same_cached_draft():
    """The angle changes the output, so it must change the cache key.

    Without this, variant B is served variant A's cached generation and the
    operator is asked to choose between two identical drafts.
    """
    import inspect

    from app.agents import generator

    source = inspect.getsource(generator.generate)
    after_salt = source.split("attempt_salt")[1][:120]
    assert "approach" in after_salt, "the angle does not participate in the cache key"


@pytest.mark.p1
def test_the_prompt_survives_a_job_with_no_preferences():
    """StrictUndefined: an optional block must have a default, not a crash."""
    rendered = _prompt()

    assert "WHAT THIS OPERATOR PREFERS" not in rendered
    assert "YOUR ANGLE" not in rendered
