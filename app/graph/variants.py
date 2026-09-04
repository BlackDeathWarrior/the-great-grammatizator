"""Two or three genuinely different takes, for the operator to choose between.

A single draft asks "is this acceptable?", which is a poor question: the
operator has nothing to compare against and usually says yes. Showing several
takes asks "which of these?", which they can answer immediately and which
teaches the system something durable (app/agents/preferences.py).

Two properties that make this worth doing rather than decorative:

- **Every variant clears the same bar.** Each is QA'd independently and only
  passing ones are offered, so the choice is between publishable drafts rather
  than between a good one and two strawmen. A variant that fails is dropped and
  said to be dropped, never quietly shown.
- **The angles are genuinely different.** Each draft is told to commit to one
  approach, because three hedged drafts are indistinguishable and teach nothing.

Variants cost one generation plus one QA pass each. That is the price of a
choice, and it is why this is opt-in per job rather than always on.
"""

from __future__ import annotations

import asyncio
import logging

from app.graph.state import AnalysisResult, ArtefactStatus, ContentObject, Parameters

log = logging.getLogger(__name__)

# Ordered: with 2 variants an operator gets the first two, which are the most
# usefully opposed pair. The phrasing is what reaches the generator prompt.
APPROACHES: list[tuple[str, str]] = [
    (
        "data-led",
        "Open with the single most concrete fact - a figure, a version, a date - "
        "and let it carry the urgency. No rhetorical question, no scene-setting.",
    ),
    (
        "consequence-led",
        "Open with what this means for the reader and what they must decide. "
        "Facts support the argument; they do not lead it.",
    ),
    (
        "narrative",
        "Open with the concrete situation as it unfolded, in plain sequence, and "
        "let the reader draw the conclusion. Conversational, never breathless.",
    ),
]

MIN_VARIANTS = 2
MAX_VARIANTS = 3


def approaches(n: int) -> list[tuple[str, str]]:
    """The first n approaches, clamped to what is actually useful."""
    n = max(MIN_VARIANTS, min(int(n or MIN_VARIANTS), MAX_VARIANTS))
    return APPROACHES[:n]


async def run(
    format_id: str,
    content: ContentObject,
    analysis: AnalysisResult,
    parameters: Parameters,
    *,
    job_id: str = "",
    count: int = MIN_VARIANTS,
    style_notes: list[str] | None = None,
) -> list[dict]:
    """Generate n variants concurrently. Returns one record per variant.

    Failures are reported, not hidden: a variant that could not be produced
    appears with its status and error rather than silently reducing the choice
    to one option with no explanation.
    """
    from app.graph.build import run_artefact

    chosen = approaches(count)

    results = await asyncio.gather(
        *(
            run_artefact(
                format_id,
                content,
                analysis,
                parameters,
                job_id=job_id,
                approach=instruction,
                style_notes=style_notes or [],
            )
            for _name, instruction in chosen
        ),
        return_exceptions=True,
    )

    records: list[dict] = []
    for (name, _instruction), outcome, label in zip(chosen, results, "ABCDEFG", strict=False):
        if isinstance(outcome, BaseException):
            log.warning("variant %s (%s) raised: %s", label, name, outcome)
            records.append(
                {
                    "label": label,
                    "approach": name,
                    "status": str(ArtefactStatus.FAILED),
                    "error": str(outcome),
                    "content": None,
                    "claims": [],
                    "qa": [],
                    "export_paths": [],
                }
            )
            continue

        artefact, _message = outcome
        qa_rows = artefact.qa_result.results if artefact.qa_result else []
        records.append(
            {
                "label": label,
                "approach": name,
                "status": str(artefact.status),
                "error": artefact.error or "",
                "content": artefact.content,
                "claims": [c.model_dump() for c in artefact.claims],
                "qa": [r.model_dump() for r in qa_rows],
                "export_paths": artefact.export_paths,
            }
        )

    return records


def offerable(records: list[dict]) -> list[dict]:
    """Only variants that actually cleared QA are worth choosing between."""
    return [
        r
        for r in records
        if r["status"] in (str(ArtefactStatus.PASSED), str(ArtefactStatus.PASSED_FLAGGED))
    ]
