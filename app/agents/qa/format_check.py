"""Format checker. DETERMINISTIC - no LLM, ever.

A tweet over 280 characters is not a judgement call (ARCHITECTURE.md sec.8).
Spending a model call on a length check would be slower, less reliable and
non-reproducible. TC-0503 asserts zero completion calls in this module.

Constraints come from the registry entry, so the limit the model was told and
the limit enforced here are the same number (TC-0304).
"""

from __future__ import annotations

import json

from app.formats.registry import FormatSpec
from app.graph.state import Artefact, CheckerName, CheckerResult


def check(spec: FormatSpec, artefact: Artefact) -> CheckerResult:
    """Validate against the registry constraints. Pure function, no I/O."""
    content = artefact.content or {}
    c = spec.constraints
    failures: list[str] = []

    total = len(json.dumps(content))

    # Every format had a maximum and almost none had a minimum, so a
    # two-sentence LinkedIn post passed this checker cleanly. Length is
    # measurable, so it is measured here rather than left to the editorial
    # checker's judgement.
    if "min_chars" in c or "max_chars" in c:
        body = _text_len(content)
        if "max_chars" in c and body > c["max_chars"]:
            failures.append(
                f"Body is {body} characters; the limit is {c['max_chars']}. "
                f"Cut roughly {body - c['max_chars']} characters."
            )
        # Measured on the MAIN PROSE FIELD, not the whole object. Summing
        # every field let a 685-character body clear a 900 floor once the
        # hook, call to action and hashtags were counted with it - so the
        # floor passed while the part the reader actually reads stayed thin.
        if "min_chars" in c:
            main = _main_prose(content)
            if len(main) < c["min_chars"]:
                failures.append(
                    f"The main text is only {len(main)} characters; at least "
                    f"{c['min_chars']} is expected. Add roughly "
                    f"{c['min_chars'] - len(main)} more characters of substance, "
                    "not padding: develop each point with a specific from the "
                    "source - a figure, a date, a version, a named system - and "
                    "say what it means for the reader and what they should do."
                )

    if "hashtags_max" in c:
        tags = content.get("hashtags") or []
        if len(tags) > c["hashtags_max"]:
            failures.append(
                f"{len(tags)} hashtags; the maximum is {c['hashtags_max']}. "
                f"Remove {len(tags) - c['hashtags_max']}."
            )

    if "max_chars_per_tweet" in c:
        limit = c["max_chars_per_tweet"]
        for i, tweet in enumerate(content.get("tweets") or [], start=1):
            if len(tweet) > limit:
                failures.append(
                    f"Tweet {i} is {len(tweet)} characters; the limit is {limit}. "
                    f"Cut {len(tweet) - limit}."
                )

    if "min_chars_per_tweet" in c:
        floor = c["min_chars_per_tweet"]
        for i, tweet in enumerate(content.get("tweets") or [], start=1):
            if len(tweet) < floor:
                failures.append(
                    f"Tweet {i} is only {len(tweet)} characters; at least {floor} "
                    "is expected. A tweet that says nothing specific is filler."
                )

    failures += _range(content, "tweets", c.get("tweets_min"), c.get("tweets_max"), "tweets")
    failures += _range(content, "scenes", c.get("scenes_min"), c.get("scenes_max"), "scenes")
    failures += _range(content, "panels", c.get("panels_min"), c.get("panels_max"), "panels")
    failures += _range(content, "slides", c.get("slides_min"), c.get("slides_max"), "slides")
    failures += _range(
        content, "key_points", c.get("key_points_min"), c.get("key_points_max"), "key points"
    )
    failures += _range(
        content,
        "recommendations",
        c.get("recommendations_min"),
        c.get("recommendations_max"),
        "recommendations",
    )

    if "headline_max_chars" in c:
        headline = content.get("headline") or ""
        if len(headline) > c["headline_max_chars"]:
            failures.append(
                f"Headline is {len(headline)} characters; the limit is {c['headline_max_chars']}."
            )

    if "bullets_max_per_slide" in c:
        limit = c["bullets_max_per_slide"]
        for i, slide in enumerate(content.get("slides") or [], start=1):
            bullets = slide.get("bullets") or []
            if len(bullets) > limit:
                failures.append(f"Slide {i} has {len(bullets)} bullets; the maximum is {limit}.")

    if "runtime_target_sec" in c and content.get("scenes"):
        target = c["runtime_target_sec"]
        tolerance = c.get("runtime_tolerance_sec", 30)
        total_sec = sum(s.get("duration_sec", 0) for s in content["scenes"])
        if abs(total_sec - target) > tolerance:
            failures.append(
                f"Scenes total {total_sec:.0f}s; the target is {target}s "
                f"(+/-{tolerance}s). Adjust scene durations."
            )

    if "key_point_min_chars" in c:
        floor = c["key_point_min_chars"]
        for i, point in enumerate(content.get("key_points") or [], start=1):
            if len(point or "") < floor:
                failures.append(
                    f"Key point {i} is only {len(point or '')} characters; at "
                    f"least {floor} is expected. A key point is a finding with "
                    "its consequence, not a headline."
                )

    if "caption_min_chars" in c:
        floor = c["caption_min_chars"]
        for i, panel in enumerate(content.get("panels") or [], start=1):
            caption = (panel or {}).get("caption") or ""
            if len(caption) < floor:
                failures.append(
                    f"Panel {i}'s caption is only {len(caption)} characters; at "
                    f"least {floor} is expected. Say what the number means."
                )

    if "speaker_notes_min_chars" in c:
        floor = c["speaker_notes_min_chars"]
        for i, slide in enumerate(content.get("slides") or [], start=1):
            notes = (slide or {}).get("speaker_notes") or ""
            if len(notes) < floor:
                failures.append(
                    f"Slide {i}'s speaker notes are only {len(notes)} characters; "
                    f"at least {floor} is expected. The notes are half the "
                    "deliverable - a deck without them is unusable."
                )

    if "narration_min_chars_per_scene" in c:
        floor = c["narration_min_chars_per_scene"]
        for i, scene in enumerate(content.get("scenes") or [], start=1):
            narration = (scene or {}).get("narration") or ""
            if len(narration) < floor:
                failures.append(
                    f"Scene {i}'s narration is only {len(narration)} characters; "
                    f"at least {floor} is expected to fill the scene's duration."
                )

    if "narration_max_chars_per_scene" in c:
        limit = c["narration_max_chars_per_scene"]
        for i, scene in enumerate(content.get("scenes") or [], start=1):
            narration = scene.get("narration") or ""
            if len(narration) > limit:
                failures.append(
                    f"Scene {i} narration is {len(narration)} characters; the limit is {limit}."
                )

    return CheckerResult(
        checker=CheckerName.FORMAT,
        passed=not failures,
        reason=(
            f"{len(failures)} constraint violation(s)"
            if failures
            else f"within all constraints ({total} chars serialised)"
        ),
        fix_notes=failures,
    )


# The field a reader actually reads, per format. A floor applied to the whole
# object is not a floor on the prose.
_MAIN_FIELDS = ("body", "summary", "bottom_line")


def _main_prose(content: dict) -> str:
    """The principal prose field, or the longest string if none is named."""
    for key in _MAIN_FIELDS:
        value = content.get(key)
        if isinstance(value, str):
            return value
    strings = [v for v in content.values() if isinstance(v, str)]
    return max(strings, key=len) if strings else ""


def _text_len(content: dict) -> int:
    """Length of the human-visible prose, excluding structural metadata.

    Counting the serialised JSON would penalise a format for its own field
    names, so claims and machine fields are excluded.
    """
    skip = {"claims", "subtitles_srt", "source_chunks", "icon_hint"}
    total = 0
    for key, value in content.items():
        if key in skip:
            continue
        total += _measure(value)
    return total


def _measure(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_measure(v) for v in value)
    if isinstance(value, dict):
        return sum(_measure(v) for k, v in value.items() if k not in ("source_chunks",))
    return 0


def _range(content: dict, key: str, low, high, label: str) -> list[str]:
    if low is None and high is None:
        return []
    items = content.get(key)
    if items is None:
        return []
    n = len(items)
    if low is not None and n < low:
        return [f"{n} {label}; at least {low} required."]
    if high is not None and n > high:
        return [f"{n} {label}; at most {high} allowed."]
    return []
