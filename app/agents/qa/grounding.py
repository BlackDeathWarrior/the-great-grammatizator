"""Grounding checker: claims vs the chunks they cite.

This checker HAS NO WEB SEARCH. With it, it would verify claims against the
internet instead of the source document and silently defeat itself. The denial
is enforced by the tool registry allowlist, not by this module and not by a
prompt (Invariant 6, TC-1001).

Verification is per-claim against the cited chunk, which is what makes grounding
cheap (ARCHITECTURE.md sec.7).
"""

from __future__ import annotations

import json
import logging

from app.gateway import router
from app.graph.state import Artefact, CheckerName, CheckerResult, ContentObject
from app.prompts import loader

log = logging.getLogger(__name__)


async def check(artefact: Artefact, content: ContentObject, *, job_id: str = "") -> CheckerResult:
    """Verify every claim against the chunk it cites."""
    claims = artefact.claims
    if not claims:
        return CheckerResult(
            checker=CheckerName.GROUNDING,
            passed=False,
            reason="no claims cited",
            fix_notes=[
                "The artefact cites no sources. Every factual statement must "
                "appear in claims[] with the chunk_id it came from."
            ],
        )

    valid_ids = content.chunk_ids()

    # A cited id that does not exist is a grounding failure, not a crash
    # (TC-0404). Caught here deterministically - no model call needed to know
    # that c99 is not in the document.
    invented = [c for c in claims if c.chunk_id not in valid_ids]
    if invented:
        notes = [
            f'Claim "{c.text[:90]}" cites {c.chunk_id}, which does not exist. '
            f"Cite one of: {', '.join(sorted(valid_ids))}."
            for c in invented
        ]
        return CheckerResult(
            checker=CheckerName.GROUNDING,
            passed=False,
            reason=f"{len(invented)} claim(s) cite a nonexistent chunk",
            fix_notes=notes,
        )

    pairs = [
        {
            "index": i,
            "claim": c.text,
            "chunk_id": c.chunk_id,
            "chunk_text": (content.chunk_by_id(c.chunk_id).text or "")[:1200],
        }
        for i, c in enumerate(claims, start=1)
    ]

    prompt = loader.render("grounding@v1", pairs=pairs)
    try:
        raw = await router.complete(
            router.FAST,
            [
                {"role": "system", "content": loader.system("grounding@v1")},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
        )
        verdicts = _parse(raw)
    except router.ProviderError:
        raise  # infrastructure, not quality - the caller must not count it

    failures = []
    for item in verdicts:
        if not item.get("supported", True):
            idx = item.get("index", 0)
            claim = claims[idx - 1] if 1 <= idx <= len(claims) else None
            text = claim.text if claim else item.get("claim", "")
            chunk_id = claim.chunk_id if claim else item.get("chunk_id", "")
            reason = item.get("reason", "not supported by the cited chunk")
            failures.append(
                f'Claim "{text[:90]}" is not supported by {chunk_id}: {reason}. '
                "Either cite the chunk that does support it, or remove the claim."
            )

    return CheckerResult(
        checker=CheckerName.GROUNDING,
        passed=not failures,
        score=(len(claims) - len(failures)) / len(claims),
        reason=f"{len(claims) - len(failures)}/{len(claims)} claims supported",
        fix_notes=failures,
    )


def _parse(raw: str) -> list[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        # Previously this returned [] - no failures - so an unparseable
        # response silently certified every claim as grounded. Raise instead:
        # the runner turns this into a checker_error and the verdict blocks,
        # because "we could not check" must never render as "it checked out".
        raise ValueError(f"grounding returned non-JSON: {text[:120]!r}") from exc
    if isinstance(data, dict):
        data = data.get("claims") or data.get("verdicts") or []
    return data if isinstance(data, list) else []
