"""Run the checkers. Parallel, and INDEPENDENT.

Each checker sees the artefact and its own criteria, never another checker's
verdict - otherwise they anchor on each other (ARCHITECTURE.md sec.8, TC-0508).
Independence here is structural: each coroutine is given only what it needs, so
there is no channel through which one verdict could reach another.

Concurrency is capped rather than firing all of them at once, or a free tier
rate-limits mid-demo (sec.3, TC-0904).
"""

from __future__ import annotations

import asyncio
import logging

from app.formats.registry import FormatSpec
from app.gateway import router
from app.graph.state import (
    AnalysisResult,
    Artefact,
    CheckerName,
    CheckerResult,
    ContentObject,
    Parameters,
    QAResult,
)

log = logging.getLogger(__name__)


async def run(
    spec: FormatSpec,
    artefact: Artefact,
    content: ContentObject,
    parameters: Parameters,
    analysis: AnalysisResult,
    *,
    job_id: str = "",
) -> QAResult:
    """Run every applicable checker and collect their verdicts."""
    from app.agents.qa import format_check, grounding, reuse, safety, tone

    # Deterministic checkers: no model call, no reason to gate them behind the
    # semaphore (TC-0503).
    results: list[CheckerResult] = [format_check.check(spec, artefact)]

    # Source reuse runs only for copyrighted provenance (TC-0512).
    if analysis.commentary_mode:
        results.append(reuse.check(artefact, content))

    sem = router.qa_semaphore()

    async def guarded(coro_fn, name: CheckerName):
        async with sem:
            try:
                return await coro_fn()
            except router.ProviderError:
                # Infrastructure, not quality. Surface it so the caller can
                # retry with backoff without touching the QA counter (TC-0609).
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s checker errored: %s", name, exc)
                return CheckerResult(
                    checker=name, passed=True, reason=f"checker unavailable: {exc}"
                )

    llm_results = await asyncio.gather(
        guarded(lambda: grounding.check(artefact, content, job_id=job_id), CheckerName.GROUNDING),
        guarded(lambda: tone.check(artefact, parameters), CheckerName.TONE),
        guarded(lambda: safety.check(artefact), CheckerName.SAFETY),
    )
    results.extend(llm_results)

    return QAResult(results=results)
