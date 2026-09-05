"""The interview: describe what you want, instead of picking from lists.

The dashboard's dropdowns are closed vocabularies for a real reason - a
dropdown gives the tone checker something concrete to compare against, and free
text gives it noise (UC-02, TC-0202). But they are also restrictive: five
audiences, four styles, and no way to say "second-year CS students who have not
seen memory safety before".

This replaces the input METHOD, not the data contract. The conversation is free
form; what comes out is the same six structured Parameters, validated the same
way. So the checkers still get something concrete, the cache key still works,
and an audience richer than "engineers" makes the tone and editorial checkers
better informed rather than worse.

Stateless per turn: the transcript is passed back each time rather than held in
a session. There is no auth and no session store in this system, and adding one
to hold a two-message conversation would be the wrong trade.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from app.gateway import router
from app.graph.state import ContentObject, Parameters
from app.prompts import loader

log = logging.getLogger(__name__)

# Enough of the source for the model to recognise what it is looking at. It is
# proposing a brief, not analysing the document - that is the analysis agent's
# job, and it runs once per job rather than once per turn.
_EXCERPT_CHARS = 2_000

# The operator is describing intent, not writing the artefact. This also bounds
# what reaches the prompt from a free-text field.
MAX_ANSWER_CHARS = 1_000

# A conversation that has not converged by here is not going to. The operator
# can always edit the resulting fields directly.
MAX_TURNS = 8


class Turn(BaseModel):
    role: str  # "assistant" or "operator"
    text: str


class InterviewReply(BaseModel):
    """One turn of the interview."""

    done: bool = False
    message: str = ""
    # The model's best guess so far, shown so the operator can correct it
    # directly rather than answering more questions.
    draft: dict[str, str] = Field(default_factory=dict)
    parameters: Parameters | None = None


async def next_turn(content: ContentObject, transcript: list[Turn]) -> InterviewReply:
    """Ask the next question, or finish with a filled-in brief."""
    if len(transcript) >= MAX_TURNS * 2:
        # Converge rather than loop. Whatever has been understood so far is
        # better than another question.
        return _finish(_draft_from(transcript), "Using what we have so far.")

    prompt = loader.render(
        "interview@v1",
        title=content.title or "Untitled",
        source_type=str(content.source_type),
        excerpt=(content.text or "")[:_EXCERPT_CHARS],
        transcript=[t.model_dump() for t in transcript],
    )

    try:
        raw = await router.complete(
            router.FAST,
            [
                {"role": "system", "content": loader.system("interview@v1")},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
    except router.ProviderError as exc:
        # The interview is a convenience, not the pipeline. If it cannot run,
        # say so plainly and let the operator use the lists - never block them
        # from starting a job.
        log.warning("interview unavailable: %s", exc)
        return InterviewReply(
            done=False,
            message=(
                "The assistant is unavailable right now. Pick from the lists "
                "instead, or try again in a moment."
            ),
        )

    data = _parse(raw)
    draft = _clean(data.get("draft") or data.get("parameters") or {})

    if data.get("done"):
        return _finish(draft, data.get("message", ""))

    return InterviewReply(
        done=False,
        message=str(data.get("message") or "").strip(),
        draft=draft,
    )


def _finish(draft: dict[str, str], message: str) -> InterviewReply:
    """Turn a draft into validated Parameters, falling back to the defaults.

    Validation is Parameters' own (TC-0206): a field the model returned too
    long, blank or multi-line is dropped rather than failing the interview, so
    a bad turn costs a default instead of the whole conversation.
    """
    usable: dict[str, str] = {}
    for field, value in draft.items():
        if field not in Parameters.model_fields:
            continue
        try:
            Parameters(**{field: value})
        except ValueError:
            log.warning("interview proposed an unusable %s; keeping the default", field)
            continue
        usable[field] = value

    return InterviewReply(
        done=True,
        message=message.strip() or "Ready to generate.",
        draft=usable,
        parameters=Parameters(**usable),
    )


def _draft_from(transcript: list[Turn]) -> dict[str, str]:
    """Last draft the model proposed, for the give-up path."""
    for turn in reversed(transcript):
        if turn.role == "assistant":
            try:
                data = json.loads(turn.text)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("draft"), dict):
                return _clean(data["draft"])
    return {}


def _clean(draft: dict) -> dict[str, str]:
    """Keep the known fields, as single-line strings."""
    return {
        k: " ".join(str(v).split())[:MAX_ANSWER_CHARS]
        for k, v in draft.items()
        if k in Parameters.model_fields and v
    }


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # A malformed turn is not worth failing on - but showing the raw reply
        # is not the answer either. A truncated response (the common case on a
        # rate-limited free tier) still LOOKS like JSON, so the operator was
        # shown a wall of braces and field names in the chat bubble.
        #
        # Recover the message field if it survived the truncation; otherwise
        # say plainly that the turn was lost. Either beats printing the
        # protocol at somebody.
        log.warning("interview returned non-JSON; recovering what is readable")
        salvaged = _salvage_message(text)
        return {
            "done": False,
            "message": salvaged
            or "That reply came back garbled. Say it again, or pick from the lists.",
        }
    return data if isinstance(data, dict) else {}


def _salvage_message(text: str) -> str:
    """Pull the human sentence out of a JSON reply that did not finish.

    Deliberately narrow: it reads the "message" field and nothing else. Trying
    to reconstruct the draft from half a document would put values the operator
    never saw into their brief, which is worse than losing the turn.
    """
    match = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not match:
        return ""
    try:
        # Close the string so the standard decoder handles the escapes.
        return json.loads(f'"{match.group(1)}"')[:500].strip()
    except json.JSONDecodeError:
        return ""
