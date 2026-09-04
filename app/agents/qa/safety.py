"""Safety checker: PII and policy. Hard gate - a fail BLOCKS outright.

"Three of four passed" is meaningless when the failure is safety
(ARCHITECTURE.md sec.8). A blocked artefact is never delivered (TC-0602).

Deterministic PII detection runs first: an email address in the output is not a
judgement call, and catching it without a model call is faster and certain.
"""

from __future__ import annotations

import json
import logging

from app.gateway import guardrails, router
from app.graph.state import Artefact, CheckerName, CheckerResult
from app.prompts import loader

log = logging.getLogger(__name__)


async def check(artefact: Artefact) -> CheckerResult:
    body = json.dumps(artefact.content or {})

    # Deterministic first pass (TC-0507).
    pii = guardrails.find_pii(body)
    if pii:
        return CheckerResult(
            checker=CheckerName.SAFETY,
            passed=False,
            reason=f"personal data present: {', '.join(pii)}",
            fix_notes=[
                f"The artefact contains {', '.join(pii)}. Remove it entirely; "
                "do not substitute a placeholder that still identifies anyone."
            ],
        )

    raw = await router.complete(
        router.FAST,
        [
            {"role": "system", "content": loader.system("safety@v1")},
            {"role": "user", "content": loader.render("safety@v1", artefact=body[:6000])},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    data = _parse(raw)
    if "safe" not in data:
        # The one key this checker exists to produce. Defaulting it to True
        # would let a model that ignored the schema wave the artefact through.
        raise ValueError(f"safety response has no 'safe' key: {sorted(data)}")
    safe = bool(data["safe"])
    reason = data.get("reason", "")

    return CheckerResult(
        checker=CheckerName.SAFETY,
        passed=safe,
        reason=reason or ("no policy concerns" if safe else "policy concern"),
        fix_notes=[] if safe else [data.get("suggestion") or f"Safety concern: {reason}"],
    )


def _parse(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # This used to return {} - which `data.get("safe", True)` read as SAFE.
        # The deterministic PII pass does carry a real guarantee, but it only
        # covers PII: it says nothing about the policy judgement this call was
        # supposed to make, so treating its absence as a pass overstated what
        # had been checked.
        #
        # Raising routes this through the runner as a checker_error, which the
        # verdict reports as "unverified, withheld" - an infrastructure fault
        # the operator can act on, distinct from a quality failure, and it
        # never spends the QA retry budget.
        raise ValueError(f"safety returned non-JSON: {text[:120]!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"safety returned {type(data).__name__}, expected an object")
    return data
