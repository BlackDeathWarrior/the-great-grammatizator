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

# Checkers whose verdict is load-bearing. If one of these cannot run, the
# artefact is unverified and must not be delivered as though it passed.
_FAIL_CLOSED = frozenset({CheckerName.GROUNDING, CheckerName.SAFETY, CheckerName.EDITORIAL})


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
    from app.agents.qa import editorial, format_check, grounding, reuse, safety, tone

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
                # A checker that crashed did not assess anything. Reporting a
                # pass here would silently delete the gate: before this, any
                # bug in safety.check - a bad response shape, a template error,
                # a ValueError parsing a score - made every artefact "safe".
                #
                # Hard checkers therefore fail CLOSED. Tone stays open because
                # it is advisory by policy (verdict.py): it can never block on
                # its own, so failing it closed would only cause false retries.
                fails_closed = name in _FAIL_CLOSED
                log.warning(
                    "%s checker errored (%s): %s",
                    name,
                    "failing closed" if fails_closed else "advisory, passing",
                    exc,
                )
                return CheckerResult(
                    checker=name,
                    passed=not fails_closed,
                    reason=f"checker unavailable: {exc}",
                    checker_error=True,
                )

    llm_results = await asyncio.gather(
        guarded(lambda: grounding.check(artefact, content, job_id=job_id), CheckerName.GROUNDING),
        guarded(lambda: tone.check(artefact, parameters), CheckerName.TONE),
        guarded(lambda: safety.check(artefact), CheckerName.SAFETY),
        guarded(lambda: editorial.check(artefact, content, parameters), CheckerName.EDITORIAL),
    )
    results.extend(llm_results)

    return QAResult(results=results)
