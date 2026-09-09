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

from pydantic import ValidationError

from app.formats.registry import FormatSpec
from app.gateway import cache, router
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
    _report(job_id, artefact.output_type, results[0], deterministic=True)

    # Source reuse runs only for copyrighted provenance (TC-0512).
    if analysis.commentary_mode:
        results.append(reuse.check(artefact, content))
        _report(job_id, artefact.output_type, results[-1], deterministic=True)

    sem = router.qa_semaphore()

    async def guarded(coro_fn, name: CheckerName, cache_extra: str = ""):
        # A checker is a pure function of the artefact it is shown, so the same
        # content judged twice is a bought answer. That happens constantly on a
        # retry: one field changes and all four checkers rerun from scratch.
        # Not keyed on job_id or attempt - two jobs producing identical content
        # should share a verdict, as TC-0205 requires of generation.
        key = cache.checker_key(str(name), artefact.content, cache_extra)
        hit = cache.get(key)
        if hit is not None:
            try:
                cached = CheckerResult.model_validate_json(hit)
                _report(job_id, artefact.output_type, cached, cached_verdict=True)
                return cached
            except ValidationError:
                log.warning("discarding malformed cached verdict for %s", name)

        _report_pending(job_id, artefact.output_type, name)
        async with sem:
            try:
                result = await coro_fn()
                cache.put(key, result.model_dump_json())
                _report(job_id, artefact.output_type, result)
                return result
            except router.MalformedOutput as exc:
                # The provider rejected the checker's own output as invalid
                # JSON. That is not infrastructure - retrying with backoff just
                # buys the same rejection - so it falls through to the handler
                # below and a hard checker fails CLOSED, exactly as it would if
                # the response had been unparseable on arrival.
                raise ValueError(f"checker output rejected upstream: {exc}") from exc
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
                # Never cached: a checker error is a fact about this moment,
                # not about the artefact.
                errored = CheckerResult(
                    checker=name,
                    passed=not fails_closed,
                    reason=f"checker unavailable: {exc}",
                    checker_error=True,
                )
                _report(job_id, artefact.output_type, errored)
                return errored

    llm_results = await asyncio.gather(
        # Grounding verifies claims AGAINST THE SOURCE, so the source is part
        # of what identifies the verdict: the same claim text can be supported
        # by one document and unsupported by another.
        guarded(
            lambda: grounding.check(artefact, content, job_id=job_id),
            CheckerName.GROUNDING,
            content.source_hash,
        ),
        # Tone and editorial judge against the operator's brief, so the brief
        # is part of what identifies the verdict.
        guarded(
            lambda: tone.check(artefact, parameters),
            CheckerName.TONE,
            parameters.cache_fragment(),
        ),
        guarded(lambda: safety.check(artefact), CheckerName.SAFETY),
        guarded(
            lambda: editorial.check(artefact, content, parameters),
            CheckerName.EDITORIAL,
            parameters.cache_fragment() + "|" + content.source_hash,
        ),
    )
    results.extend(llm_results)

    return QAResult(results=results)


def _report_pending(job_id: str, output_type: str, name: CheckerName) -> None:
    """This checker is now in flight.

    Written before the semaphore is acquired rather than after, so a checker
    waiting on the concurrency cap shows as running rather than as missing.
    An operator watching a slow job needs to see that tone is QUEUED, not
    conclude it never started.
    """
    from app.graph import progress

    progress.report(
        job_id,
        progress.QA,
        progress.ACTIVE,
        key=f"{output_type}:{name}",
        detail="checking",
        data={"checker": str(name)},
    )


def _report(
    job_id: str,
    output_type: str,
    result: CheckerResult,
    *,
    deterministic: bool = False,
    cached_verdict: bool = False,
) -> None:
    """One checker has decided.

    Carries how the verdict was reached alongside it. "format passed" and
    "format passed without spending a model call" are different facts, and the
    second is one the architecture makes a claim about (TC-0503) - so the board
    can show it rather than the operator taking it on trust.
    """
    from app.graph import progress

    score = getattr(result, "score", None)
    if result.checker_error:
        detail = "could not run"
    elif score is not None:
        detail = f"{score:.2f}"
    else:
        detail = "passed" if result.passed else "failed"

    progress.report(
        job_id,
        progress.QA,
        progress.DONE if result.passed else progress.FAILED,
        key=f"{output_type}:{result.checker}",
        detail=detail,
        data={
            "checker": str(result.checker),
            "passed": result.passed,
            "score": score,
            "reason": (result.reason or "")[:200],
            "checker_error": result.checker_error,
            # Provably zero model calls (TC-0503), or a verdict bought once and
            # reused - both worth distinguishing from a fresh model call.
            "deterministic": deterministic,
            "cached": cached_verdict,
        },
    )
