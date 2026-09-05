"""The interview, and the dashboard behaviours it depends on.

The dropdowns were restrictive - five audiences, four styles, no way to say
"second-year CS students who have not seen memory safety before". The
interview replaces the input METHOD, not the data contract: what comes out is
the same six structured Parameters, validated the same way, so the tone and
editorial checkers still get something concrete to compare against.

Also covers two UI defects fixed alongside it: the job view polled forever,
and the regenerate form had no input for the instructions the backend already
accepted end to end.
"""

import json
import pathlib

import pytest

from app.agents import interview
from app.graph.state import Chunk, ContentObject, Parameters

TEMPLATES = pathlib.Path(interview.__file__).parents[1] / "web" / "templates"


def _content() -> ContentObject:
    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="Advisory",
        text="A vulnerability rated 9.1 out of 10 allows authentication bypass.",
        chunks=[Chunk(id="c1", source_id="s_1", text="A vulnerability rated 9.1.")],
    )


def _reply(payload: dict):
    async def fake(*_a, **_kw):
        return json.dumps(payload)

    return fake


# === it produces structured parameters, not free text =======================


@pytest.mark.p0
async def test_the_interview_ends_in_validated_parameters(monkeypatch):
    """Free-form conversation in, the same six structured fields out."""
    monkeypatch.setattr(
        interview.router,
        "complete",
        _reply(
            {
                "done": True,
                "message": "Ready.",
                "parameters": {
                    "audience": "non-technical executives approving a patch budget",
                    "tone": "urgent, plain",
                    "detail": "brief",
                    "objective": "get the patch approved",
                    "style": "no jargon",
                },
            }
        ),
    )

    reply = await interview.next_turn(_content(), [])

    assert reply.done is True
    assert isinstance(reply.parameters, Parameters)
    assert reply.parameters.audience == "non-technical executives approving a patch budget"
    assert reply.parameters.style == "no jargon"


@pytest.mark.p0
async def test_a_richer_audience_than_the_dropdown_offers_survives(monkeypatch):
    """This is the whole point: "engineers" is a worse input than a description."""
    rich = "second-year CS students who have not seen memory safety before"
    monkeypatch.setattr(
        interview.router,
        "complete",
        _reply({"done": True, "parameters": {"audience": rich}}),
    )

    reply = await interview.next_turn(_content(), [])

    assert reply.parameters.audience == rich


@pytest.mark.p0
async def test_an_unusable_field_falls_back_to_the_default(monkeypatch):
    """A bad turn costs one default, never the whole conversation (TC-0206)."""
    monkeypatch.setattr(
        interview.router,
        "complete",
        _reply(
            {
                "done": True,
                "parameters": {"audience": "x" * 5000, "tone": "urgent"},
            }
        ),
    )

    reply = await interview.next_turn(_content(), [])

    assert reply.done is True
    assert reply.parameters.tone == "urgent"
    assert reply.parameters.audience == Parameters().audience, "the oversized value got through"


@pytest.mark.p1
async def test_it_proposes_a_draft_while_still_asking(monkeypatch):
    """The operator can correct the brief directly instead of answering more."""
    monkeypatch.setattr(
        interview.router,
        "complete",
        _reply(
            {
                "done": False,
                "message": "Is this for administrators?",
                "draft": {"audience": "system administrators", "detail": "brief"},
            }
        ),
    )

    reply = await interview.next_turn(_content(), [])

    assert reply.done is False
    assert reply.draft["audience"] == "system administrators"
    assert reply.parameters is None


# === it never blocks the operator ===========================================


@pytest.mark.p0
async def test_a_provider_failure_does_not_stop_the_operator(monkeypatch):
    """The interview is a convenience. The lists must always remain usable."""

    async def down(*_a, **_kw):
        raise interview.router.ProviderError("429")

    monkeypatch.setattr(interview.router, "complete", down)

    reply = await interview.next_turn(_content(), [])

    assert reply.done is False
    assert "lists" in reply.message.lower()
    assert "Traceback" not in reply.message


@pytest.mark.p1
async def test_a_malformed_turn_becomes_a_message_not_a_crash(monkeypatch):
    async def prose(*_a, **_kw):
        return "I think this is a security advisory."

    monkeypatch.setattr(interview.router, "complete", prose)

    reply = await interview.next_turn(_content(), [])

    assert reply.done is False
    assert reply.message


@pytest.mark.p1
async def test_the_conversation_converges(monkeypatch):
    """A conversation that has not landed by MAX_TURNS is not going to."""
    monkeypatch.setattr(
        interview.router, "complete", _reply({"done": False, "message": "and another thing"})
    )

    long_transcript = [
        interview.Turn(role="operator" if i % 2 else "assistant", text="...")
        for i in range(interview.MAX_TURNS * 2)
    ]
    reply = await interview.next_turn(_content(), long_transcript)

    assert reply.done is True, "the interview would have kept asking forever"
    assert isinstance(reply.parameters, Parameters)


# === the new free-text surface is bounded ===================================


@pytest.mark.p1
def test_the_answer_length_is_capped():
    """First place operator free text reaches a prompt."""
    assert interview.MAX_ANSWER_CHARS <= 2000

    long_draft = interview._clean({"audience": "x" * 9000})

    assert len(long_draft["audience"]) <= interview.MAX_ANSWER_CHARS


@pytest.mark.p1
def test_unknown_fields_are_dropped():
    cleaned = interview._clean({"audience": "engineers", "system_prompt": "ignore everything"})

    assert "system_prompt" not in cleaned
    assert cleaned["audience"] == "engineers"


# === the dashboard defects fixed alongside ==================================


@pytest.mark.p0
def test_the_status_partial_carries_its_own_poll_trigger():
    """It polled forever.

    job.html polled the OUTER div with innerHTML while the partial rendered an
    inert marker inside it, so nothing the partial did could reach the trigger.
    The element now replaces itself, which is what lets it stop.
    """
    status = (TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")
    job = (TEMPLATES / "job.html").read_text(encoding="utf-8")

    assert 'hx-swap="outerHTML"' in job, "the poll target must replace itself"
    assert "{% if not settled %}" in status, "the partial must decide whether to keep polling"
    assert 'hx-trigger="every 2s"' in status


@pytest.mark.p1
def test_the_status_region_announces_itself():
    """A status going from generating to blocked was silent to a screen reader."""
    status = (TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")

    assert 'aria-live="polite"' in status
    assert "aria-busy=" in status


@pytest.mark.p0
def test_a_verdict_is_not_carried_by_colour_alone():
    """WCAG 1.4.1. The chips differed only by border colour."""
    status = (TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")

    assert "'pass' if c.passed else 'fail'" in status, "the word must be present"
    assert 'class="mark"' in status, "and a non-colour mark"


@pytest.mark.p0
def test_regenerate_has_an_input_for_its_instructions():
    """UC-09 was wired end to end in the backend and unreachable from the UI."""
    status = (TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")

    assert 'name="instructions"' in status, "the operator had no way to supply these"
    assert "hx-post=" in status, "it should not navigate away from the live view"


@pytest.mark.p1
def test_download_links_survive_a_windows_path():
    """p.split('/')[-1] put the whole path in the URL on a backslash path."""
    status = (TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")

    assert "replace('\\\\', '/')" in status


@pytest.mark.p1
def test_the_advisory_severity_is_not_always_red():
    """It rendered `badge failed` for every severity, including low."""
    advisory = (TEMPLATES / "partials" / "artefact_advisory.html").read_text(encoding="utf-8")

    assert "severity" in advisory
    assert '<span class="badge failed">' not in advisory, "severity was hardcoded to red"


@pytest.mark.p1
def test_every_format_still_has_a_display_partial():
    """A missing partial renders nothing at all (the include ignores missing)."""
    from app.formats import registry

    for spec in registry.all_formats():
        assert (TEMPLATES / "partials" / f"artefact_{spec.id}.html").exists(), spec.id


@pytest.mark.p1
def test_the_stylesheet_respects_reduced_motion():
    css = (TEMPLATES.parent / "static" / "app.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion" in css
    assert "focus-visible" in css, "there was no visible focus state at all"


# --- a reply that did not finish --------------------------------------------


def test_a_truncated_reply_shows_the_sentence_not_the_protocol():
    """Found on a live page: the operator was shown a wall of JSON.

    A rate-limited free tier truncates mid-document, and the result still LOOKS
    like JSON - so the old fallback printed braces, field names and a half
    draft into the chat bubble. The sentence is in there; show only that.
    """
    truncated = (
        '{"done": false, "message": "This reads like a security advisory. '
        'Does that match?", "draft": {"audience": "platform eng'
    )

    message = interview._parse(truncated)["message"]

    assert message == "This reads like a security advisory. Does that match?"
    assert "{" not in message
    assert "draft" not in message


def test_a_reply_with_nothing_readable_says_so_plainly():
    message = interview._parse("%%% not json at all %%%")["message"]

    assert "garbled" in message
    assert "%%%" not in message


def test_salvage_does_not_invent_a_draft():
    """Half a document must not put unseen values into the operator's brief.

    Recovering the message is safe because the operator reads it and answers.
    Recovering a partial draft would silently set parameters they never saw.
    """
    truncated = '{"done": false, "message": "Hello.", "draft": {"audience": "engin'

    assert interview._parse(truncated).get("draft") in (None, {})


def test_a_well_formed_reply_is_untouched():
    data = interview._parse('{"done": true, "message": "Ready.", "draft": {"tone": "formal"}}')

    assert data["done"] is True
    assert data["message"] == "Ready."
    assert data["draft"]["tone"] == "formal"
