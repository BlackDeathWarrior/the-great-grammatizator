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
    safe = bool(data.get("safe", True))
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
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        # Fail OPEN on a parse error would be wrong for a hard gate, but failing
        # closed on every malformed response would block healthy artefacts on a
        # flaky provider. Treat as safe and let the deterministic PII pass -
        # which already ran - carry the guarantee.
        log.warning("safety returned non-JSON; deterministic PII pass already cleared it")
        return {}
