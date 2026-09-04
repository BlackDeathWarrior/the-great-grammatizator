"""Tone checker. LLM score against the operator's stated audience and style.

Returns a numeric score plus a reason string, never a bare boolean (TC-0506).
Advisory: below threshold retries once, then passes with a warning flag
(ARCHITECTURE.md sec.8, TC-0605).
"""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.gateway import router
from app.graph.state import Artefact, CheckerName, CheckerResult, Parameters
from app.prompts import loader

log = logging.getLogger(__name__)


async def check(artefact: Artefact, parameters: Parameters) -> CheckerResult:
    threshold = get_settings().tone_threshold
    prompt = loader.render(
        "tone@v1",
        artefact=json.dumps(artefact.content or {}, indent=2)[:6000],
        parameters=parameters,
    )

    raw = await router.complete(
        router.FAST,
        [
            {"role": "system", "content": loader.system("tone@v1")},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    data = _parse(raw)
    # Tone is advisory: it can never block on its own, so an unreadable
    # response degrades to "no opinion" (a pass) rather than a false retry.
    # float() on a non-numeric score used to raise here and be swallowed as a
    # pass several frames away; keeping it local makes the default deliberate.
    try:
        score = float(data.get("score", 1.0))
    except (TypeError, ValueError):
        log.warning(
            "tone returned a non-numeric score %r; scoring as no-opinion",
            data.get("score"),
        )
        score = 1.0
    reason = data.get("reason", "")
    suggestion = data.get("suggestion", "")

    passed = score >= threshold
    notes: list[str] = []
    if not passed:
        # The note must name what to change. "Quality insufficient" returns the
        # same output (ARCHITECTURE.md sec.8, TC-0604).
        notes.append(
            suggestion or f"Tone scored {score:.2f} against a {threshold:.2f} threshold: {reason}"
        )

    return CheckerResult(
        checker=CheckerName.TONE,
        passed=passed,
        score=score,
        reason=reason or f"scored {score:.2f}",
        fix_notes=notes,
    )


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        log.warning("tone returned non-JSON; scoring as pass to avoid a false retry")
        return {}
