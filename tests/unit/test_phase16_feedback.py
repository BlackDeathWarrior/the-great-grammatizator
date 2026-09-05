"""Rating an artefact, learning from it, and refusing to be too short.

Four changes, one theme: the platform now takes the operator's word for
whether the output was any good, and the deterministic checker takes
responsibility for whether there is enough of it.
"""

import pytest

from app.agents import improve
from app.agents.qa import format_check
from app.config import get_settings
from app.formats import registry
from app.graph.state import Artefact


def _artefact(content: dict, output_type: str = "linkedin_post") -> Artefact:
    return Artefact(output_type=output_type, content=content)


def _linkedin(body: str) -> dict:
    return {
        "hook": "A specific, concrete hook about the thing that happened.",
        "body": body,
        "call_to_action": "Patch to 22.7R2.6 and review your logs.",
        "hashtags": ["#CyberSecurity"],
        "claims": [],
    }


# === too short is now a failure, deterministically ==========================


@pytest.mark.p0
def test_a_two_sentence_post_no_longer_passes():
    """The complaint that started this: every format had a maximum and almost
    none had a minimum, so a 200-character LinkedIn post passed cleanly."""
    short = _artefact(_linkedin("ElevenLabs launched an AI platform. It reduces tickets."))

    result = format_check.check(registry.get("linkedin_post"), short)

    assert result.passed is False
    assert any("at least" in n for n in result.fix_notes)


@pytest.mark.p0
def test_the_fix_note_says_how_much_more_and_warns_against_padding():
    """A bare "too short" gets padding back. The note has to say what to add."""
    short = _artefact(_linkedin("Too short."))

    note = next(
        n
        for n in format_check.check(registry.get("linkedin_post"), short).fix_notes
        if "at least" in n
    )

    assert "characters" in note
    assert "not padding" in note, "the model will pad to hit a number otherwise"


@pytest.mark.p1
def test_a_substantial_post_passes():
    full = _artefact(_linkedin("Development of the point with a real specific. " * 25))

    result = format_check.check(registry.get("linkedin_post"), full)

    assert result.passed is True, result.fix_notes


@pytest.mark.p0
def test_every_prose_format_has_a_floor():
    """A maximum without a minimum only constrains one end of the problem."""
    for fid in ("linkedin_post", "exec_summary", "advisory"):
        c = registry.get(fid).constraints
        assert c.get("min_chars"), f"{fid} has no minimum length"
        assert c["min_chars"] < c["max_chars"], f"{fid}'s floor is above its ceiling"


@pytest.mark.p1
def test_a_one_line_tweet_thread_is_refused():
    thin = _artefact({"tweets": ["Short.", "Also short."], "claims": []}, "twitter_x")

    result = format_check.check(registry.get("twitter_x"), thin)

    assert result.passed is False


@pytest.mark.p1
def test_slides_with_thin_speaker_notes_are_refused():
    """The notes are half the deliverable; a deck without them is unusable."""
    bare = _artefact(
        {
            "title": "Deck",
            "slides": [
                {"heading": f"Slide {i}", "bullets": ["a", "b"], "speaker_notes": "Say this."}
                for i in range(4)
            ],
            "claims": [],
        },
        "presentation",
    )

    result = format_check.check(registry.get("presentation"), bare)

    assert result.passed is False
    assert any("speaker notes" in n.lower() for n in result.fix_notes)


@pytest.mark.p0
def test_the_generator_is_told_the_floor_before_it_writes():
    """Prevention is cheaper than a retry."""
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

    rendered = loader.render(
        spec.prompt_template,
        content=content,
        analysis=AnalysisResult(objective="inform", audience="all"),
        parameters=Parameters(),
        constraints=spec.constraints,
        fix_notes=[],
    )

    assert "LENGTH" in rendered
    assert str(spec.constraints["min_chars"]) in rendered
    assert "never by padding" in rendered


# === the self-improvement loop ==============================================


@pytest.mark.p0
def test_a_pattern_must_recur_to_count():
    """One event is an anecdote. A rule from an anecdote steers every job."""
    once = ["Body is only 200 characters; at least 900 is expected."]

    assert improve.recurring_fix_notes(once) == []


@pytest.mark.p0
def test_the_same_objection_phrased_differently_still_counts():
    """Exact matching would find no repeats at all, and the loop would never
    learn anything: a checker rephrases its objection every time."""
    notes = [
        "Body is only 210 characters; at least 900 is expected. Develop the points.",
        "Body is only 340 characters; at least 900 is expected. Develop the points.",
    ]

    recurring = improve.recurring_fix_notes(notes)

    assert len(recurring) == 1


@pytest.mark.p0
async def test_thin_evidence_teaches_nothing(monkeypatch):
    """Asking anyway invites the model to invent a pattern to be helpful."""
    called = {"n": 0}

    async def counting(*_a, **_kw):
        called["n"] += 1
        return '{"rules": ["something"]}'

    monkeypatch.setattr(improve.router, "complete", counting)

    rules = await improve.derive_rules(dislikes=[], liked=[], fix_notes=["one note"])

    assert rules == []
    assert called["n"] == 0, "it asked a model about a single data point"


@pytest.mark.p0
async def test_repeated_dislikes_produce_standing_rules(monkeypatch):
    async def reply(*_a, **_kw):
        return '{"rules": ["Write to the stated minimum and develop each point with a figure."]}'

    monkeypatch.setattr(improve.router, "complete", reply)

    rules = await improve.derive_rules(
        dislikes=[
            {"output_type": "linkedin_post", "reason": "too short"},
            {"output_type": "exec_summary", "reason": "too short again"},
        ],
        liked=[],
        fix_notes=[],
    )

    assert len(rules) == 1
    assert "minimum" in rules[0]


@pytest.mark.p1
async def test_a_provider_failure_leaves_the_prompts_alone(monkeypatch):
    """The signals are stored; only this pass is lost, and the next sees them."""

    async def down(*_a, **_kw):
        raise improve.router.ProviderError("429")

    monkeypatch.setattr(improve.router, "complete", down)

    rules = await improve.derive_rules(
        dislikes=[{"output_type": "a", "reason": "x"}, {"output_type": "b", "reason": "y"}],
        liked=[],
        fix_notes=[],
    )

    assert rules == []


@pytest.mark.p0
def test_the_loop_can_only_write_prompt_text():
    """The safety property of an autonomous loop.

    It must not be able to move a threshold, relax a constraint, or touch
    code - the worst it can do is give the generator a bad instruction, which
    is visible in the dashboard and deletable in one click.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(improve.__file__).read_text(encoding="utf-8"))

    # Names the module actually references, not words in its prose: the
    # docstring says "cannot change a threshold", which a plain grep counts as
    # a violation of the very thing it promises.
    referenced = (
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        | {
            alias.name.split(".")[-1]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
    )

    for forbidden in ("get_settings", "registry", "config", "Settings"):
        assert forbidden not in referenced, f"the improvement loop can reach {forbidden}"


@pytest.mark.p1
def test_rules_are_bounded_like_every_other_note():
    from app.agents import preferences

    notes = improve.apply_rules([], [f"Rule number {i} about openings" for i in range(20)], {})

    assert len(notes) <= preferences.MAX_NOTES


# === several formats run one at a time ======================================


@pytest.mark.p0
def test_formats_are_generated_sequentially():
    """Concurrency looks faster and is worse here: several generations plus
    their checkers compete for one free-tier quota, 429 each other, and land
    lower-quality drafts."""
    assert get_settings().fanout_concurrency == 1


@pytest.mark.p1
def test_the_fan_out_cap_comes_from_config():
    """It must be raisable when a paid tier removes the contention."""
    import pathlib

    from app.graph import build

    source = pathlib.Path(build.__file__).read_text(encoding="utf-8")

    assert "fanout_concurrency" in source
