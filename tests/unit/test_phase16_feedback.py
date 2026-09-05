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
    full = _artefact(_linkedin("Development of the point with a real specific. " * 40))

    result = format_check.check(registry.get("linkedin_post"), full)

    assert result.passed is True, result.fix_notes


@pytest.mark.p0
def test_every_prose_format_has_a_floor():
    """A maximum without a minimum only constrains one end of the problem."""
    for fid in ("linkedin_post", "advisory"):
        c = registry.get(fid).constraints
        assert c.get("min_chars"), f"{fid} has no minimum length"
        assert c["min_chars"] < c["max_chars"], f"{fid}'s floor is above its ceiling"

    # exec_summary carries its prose in key_points, so a floor on the body
    # field would demand a one-paragraph "bottom line" instead.
    assert registry.get("exec_summary").constraints.get("key_point_min_chars")


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


# === the floor measures the prose, not the object ===========================


@pytest.mark.p0
def test_the_floor_measures_the_body_not_every_field_combined():
    """A 685-character body cleared a 900 floor once the hook, call to action
    and hashtags were summed with it - so the check passed while the part the
    reader actually reads stayed thin. Observed on a live job."""
    thin_body_fat_extras = _artefact(
        {
            "hook": "A hook." + " padding to inflate the total." * 30,
            "body": "Short body that a reader would call thin.",
            "call_to_action": "Act now." + " more padding here." * 30,
            "hashtags": ["#One", "#Two", "#Three"],
            "claims": [],
        }
    )

    result = format_check.check(registry.get("linkedin_post"), thin_body_fat_extras)

    assert result.passed is False, "extras were counted toward the body's floor"
    assert any("main text" in n.lower() for n in result.fix_notes)


@pytest.mark.p0
def test_the_generator_is_told_to_aim_above_the_floor():
    """A draft landing 6 characters short burned all three retries on
    arithmetic rather than on the argument. Observed on a live job."""
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

    assert "AIM FOR" in rendered
    assert "MAIN TEXT" in rendered, "the target must name the field it is measured on"


@pytest.mark.p1
def test_exec_summary_is_measured_on_its_key_points():
    """Its prose lives in key_points; a floor on bottom_line would demand a
    one-paragraph summary line instead of a substantial briefing."""
    thin = _artefact(
        {
            "title": "Briefing",
            "bottom_line": "x" * 2000,
            "key_points": ["Rated 9.1", "Fix available", "Exploited"],
            "recommended_action": "Upgrade.",
            "residual_risk": "Some.",
            "claims": [],
        },
        "exec_summary",
    )

    result = format_check.check(registry.get("exec_summary"), thin)

    assert result.passed is False
    assert any("key point" in n.lower() for n in result.fix_notes)


# === what is learned reaches an ORDINARY job ================================


@pytest.mark.p0
def test_an_ordinary_job_loads_the_operators_learned_notes():
    """The gap that made the feedback loop cosmetic.

    style_notes were loaded only in variants_task, so a thumb down changed
    the profile and nothing else: clicking Generate produced output that had
    never seen a single thing the operator taught the system.
    """
    import inspect

    from app import worker

    source = inspect.getsource(worker.run_job_task)

    assert "_style_notes_for" in source, "an ordinary job ignores learned preferences"
    assert "style_notes=style_notes" in source, "they are loaded but not passed on"


@pytest.mark.p0
def test_a_job_records_which_operator_asked_for_it():
    """Without this the worker has nothing to look the profile up by."""
    from app.db.models import Job

    assert hasattr(Job, "profile_id")


@pytest.mark.p1
def test_a_regenerate_also_gets_the_learned_notes():
    """An operator correcting one artefact should get everything they have
    taught the system, not a draft written as though they were a stranger."""
    import inspect

    from app import worker

    assert "style_notes" in inspect.getsource(worker.regenerate_task)


@pytest.mark.p1
def test_an_anonymous_job_simply_learns_nothing():
    """The platform must work without a profile; it just cannot learn."""
    from app import worker

    assert worker._style_notes_for(None, "") == []
