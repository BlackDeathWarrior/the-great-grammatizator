"""Editorial checker. Is the writing any GOOD?

Nothing else in the QA layer asks this. Format checks lengths and counts,
grounding checks that claims cite chunks, safety checks harm, source reuse
only runs for copyrighted provenance. Tone comes closest and explicitly does
not: its own system prompt says "You judge fit only."

So an artefact could be accurate, correctly sized, safe, on-tone - and still be
the source text lightly trimmed, opening with a sentence that restates the
title. That is what the first live run produced, and every checker passed it.

Scored on four dimensions so a fix note can name the failing one; a bare
"quality insufficient" returns the same output (sec.8, TC-0604).

One deterministic input feeds the model call: the longest verbatim run against
the source, borrowed from the reuse checker. Copying is measurable, so it is
measured rather than judged - the model is told the number and weighs it.
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.gateway import router
from app.graph.state import (
    Artefact,
    CheckerName,
    CheckerResult,
    ContentObject,
    Parameters,
)
from app.prompts import loader

log = logging.getLogger(__name__)

# Below this the artefact reads as transcription rather than transformation,
# whatever the model thinks of the rest. Deliberately well above the reuse
# checker's copyright thresholds: this is about craft, not licensing.
_VERBATIM_FAIL_WORDS = 25

_DIMENSIONS = ("specificity", "substance", "originality", "structure")


def _artefact_words(artefact: Artefact) -> list[str]:
    from app.agents.qa.reuse import _words

    return _words(" ".join(_strings(artefact.content or {})))


def _strings(value) -> list[str]:
    """Every string in a nested artefact body, claims excluded.

    Claims quote the source by design - that is what makes them checkable - so
    counting them as copying would penalise correct citation.
    """
    out: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "claims":
                continue
            out.extend(_strings(inner))
    elif isinstance(value, list):
        for inner in value:
            out.extend(_strings(inner))
    elif isinstance(value, str):
        out.append(value)
    return out


def verbatim_run(artefact: Artefact, content: ContentObject) -> int:
    """Longest run of words copied straight from the source. Deterministic."""
    from app.agents.qa.reuse import _words, longest_common_run

    return longest_common_run(_artefact_words(artefact), _words(content.text))


async def check(
    artefact: Artefact, content: ContentObject, parameters: Parameters
) -> CheckerResult:
    threshold = get_settings().editorial_threshold
    reuse_len = verbatim_run(artefact, content)

    prompt = loader.render(
        "editorial@v1",
        artefact=json.dumps(artefact.content or {}, indent=2)[:6000],
        parameters=parameters,
        longest_reuse=reuse_len,
    )

    raw = await router.complete(
        router.LONG,
        [
            {"role": "system", "content": loader.system("editorial@v1")},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        # This checker asks for a nested object - four sub-scores plus a
        # suggestion - where the others want a flat one. It needs room: at 400
        # the reply came back empty or was rejected upstream as invalid JSON,
        # and because a hard checker fails closed that withheld the artefact as
        # unverified for what was really a token budget. The reply is small; the
        # ceiling only has to be comfortably above it.
        max_tokens=1200,
    )

    data = _parse(raw)
    score = _score(data)
    dimensions = {d: _as_float(data.get("dimensions", {}).get(d)) for d in _DIMENSIONS}
    worst = data.get("worst") or _worst(dimensions)
    suggestion = (data.get("suggestion") or "").strip()

    notes: list[str] = []
    passed = score >= threshold

    # Transcription is not transformation. Measured, so it does not depend on
    # the model noticing.
    if reuse_len >= _VERBATIM_FAIL_WORDS:
        passed = False
        notes.append(
            f"{reuse_len} consecutive words are copied verbatim from the source. "
            "Rewrite that passage in your own words for the stated audience; "
            "quote only inside a claim, where a citation makes it checkable."
        )

    if score < threshold:
        notes.append(
            suggestion
            or f"Editorial quality scored {score:.2f} against a {threshold:.2f} "
            f"threshold; weakest dimension: {worst}."
        )

    return CheckerResult(
        checker=CheckerName.EDITORIAL,
        passed=passed,
        score=score,
        reason=(
            f"scored {score:.2f}"
            + (f", weakest: {worst}" if worst else "")
            + (f", {reuse_len} words verbatim" if reuse_len >= 12 else "")
        ),
        fix_notes=notes,
    )


def _score(data: dict) -> float:
    """Overall score, falling back to the mean of the dimensions."""
    if "score" in data:
        return _as_float(data["score"])
    values = [_as_float(data.get("dimensions", {}).get(d)) for d in _DIMENSIONS]
    known = [v for v in values if v is not None]
    return sum(known) / len(known) if known else 0.0


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _worst(dimensions: dict) -> str:
    scored = {k: v for k, v in dimensions.items() if v is not None}
    return min(scored, key=scored.get) if scored else ""


def _salvage(text: str) -> dict | None:
    """Recover the scores from a reply that was cut off mid-write.

    Closes the object at the last complete key/value pair and re-parses. Returns
    None when nothing usable survives, so the caller can still fail closed - the
    point is to rescue a real judgement, never to manufacture one.
    """
    if not text.startswith("{"):
        return None
    # Walk back to the last comma that sits at brace depth 0 or 1, which is the
    # boundary of the last fully written entry.
    depth = 0
    cut = -1
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth in (1, 2):
            cut = i
    if cut < 0:
        return None
    candidate = text[:cut] + "}" * max(text[:cut].count("{") - text[:cut].count("}"), 0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "score" not in data:
        return None
    return data


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # A truncated reply is the common case here, and it is not the same as
        # an unreadable one: this checker writes its four scores BEFORE its
        # prose suggestion, so a response cut off mid-suggestion still carries
        # everything the verdict needs. Salvaging it turns a withheld artefact
        # into a judged one, without inventing anything - if the scores did not
        # arrive, the salvage fails and the raise below still stands.
        data = _salvage(text)
        if data is None:
            # Editorial is a hard checker, so an unreadable response must not
            # read as a pass. The runner turns this into a checker_error and
            # the verdict withholds the artefact as unverified.
            raise ValueError(f"editorial returned non-JSON: {text[:120]!r}") from exc
        log.warning("editorial reply truncated; recovered %d key(s)", len(data))
    if not isinstance(data, dict):
        raise ValueError(f"editorial returned {type(data).__name__}, expected an object")
    return data
