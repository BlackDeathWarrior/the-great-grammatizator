"""What this operator likes, learned from what they chose.

The platform generates two or three genuinely different takes on the same
artefact, the operator picks one, and the difference between the winner and the
losers is the signal. That is a better teacher than a rating: "preferred the
data-led opener to the question opener" is actionable where "7/10" is not.

Three deliberate constraints:

1. **Notes are sentences, not weights.** They go into the generator prompt
   verbatim, are shown back in the dashboard, and can be deleted one at a time.
   A preference the operator cannot read is one they cannot correct.
2. **A note is traceable.** Every note records the choice it came from, so an
   operator who disagrees can see the evidence rather than argue with a number.
3. **Notes are capped and recent-weighted.** Taste changes, and an unbounded
   pile of instructions would eventually contradict itself and blow the prompt.

This is NOT authentication (§13.1). A profile is a name in a cookie: enough to
keep two operators' tastes apart, and no more than that.
"""

from __future__ import annotations

import json
import logging

from app.gateway import router
from app.prompts import loader

log = logging.getLogger(__name__)

# Enough to shape a voice, few enough to stay coherent and bounded in the
# prompt. Oldest notes fall off the end as taste moves.
MAX_NOTES = 8

# A note is one instruction. Anything longer is a paragraph the operator did
# not write and cannot audit at a glance.
MAX_NOTE_CHARS = 180


def notes_for_prompt(style_notes: list[dict]) -> list[str]:
    """The note text a generator should see, newest last."""
    return [n["note"] for n in (style_notes or [])[-MAX_NOTES:] if n.get("note")]


async def derive_note(
    *,
    output_type: str,
    chosen_approach: str,
    rejected_approaches: list[str],
    chosen_content: dict | None = None,
) -> str:
    """Turn one choice into a durable, reusable preference.

    Returns "" when nothing general can be concluded - which is the right
    answer more often than it looks. A choice between two near-identical drafts
    teaches nothing, and inventing a rule from it would poison later jobs.
    """
    if not chosen_approach or not rejected_approaches:
        return ""

    try:
        raw = await router.complete(
            router.FAST,
            [
                {"role": "system", "content": loader.system("preference@v1")},
                {
                    "role": "user",
                    "content": loader.render(
                        "preference@v1",
                        output_type=output_type,
                        chosen=chosen_approach,
                        rejected=rejected_approaches,
                        excerpt=json.dumps(chosen_content or {})[:1200],
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except router.ProviderError as exc:
        # The choice is already recorded; only the generalisation is lost.
        log.warning("could not derive a style note: %s", exc)
        return ""

    return _clean(_parse(raw).get("note", ""))


def merge_note(style_notes: list[dict], note: str, evidence: dict) -> list[dict]:
    """Append a note, keeping the list bounded and free of near-duplicates."""
    note = _clean(note)
    if not note:
        return list(style_notes or [])

    kept = [n for n in (style_notes or []) if not _similar(n.get("note", ""), note)]
    kept.append({"note": note, "evidence": evidence})
    return kept[-MAX_NOTES:]


def _similar(a: str, b: str) -> bool:
    """Cheap near-duplicate check.

    The same preference expressed twice should replace, not accumulate: five
    variations on "prefers short openers" would crowd out everything else.
    """
    aw, bw = set(a.lower().split()), set(b.lower().split())
    if not aw or not bw:
        return False
    return len(aw & bw) / len(aw | bw) > 0.6


def _clean(note: str) -> str:
    return " ".join(str(note or "").split())[:MAX_NOTE_CHARS]


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("preference model returned non-JSON; learning nothing here")
        return {}
    return data if isinstance(data, dict) else {}
