"""Job orchestration.

analyse -> fan-out generate -> fan-out QA -> verdict -> export.

Every node reads and writes JobState and nothing else (ARCHITECTURE.md sec.4).

The per-artefact generate/QA/verdict loop is expressed directly rather than as
LangGraph conditional edges. Retries are per artefact (Invariant 7) and the
artefacts are independent, so an explicit bounded loop inside one fan-out branch
says exactly that; routing it through shared graph edges would make one
artefact's retry state observable to another, which is the thing the invariant
forbids.
"""

from __future__ import annotations

import asyncio
import logging

from app.agents import generator
from app.agents import verdict as verdict_policy
from app.agents.generator import ParseFailure
from app.agents.qa import runner
from app.config import get_settings
from app.formats import registry
from app.gateway import router
from app.graph.state import (
    AnalysisResult,
    Artefact,
    ArtefactStatus,
    ContentObject,
    JobStatus,
    Parameters,
    Verdict,
)

log = logging.getLogger(__name__)

# A provider error is infrastructure, so it must not spend the QA budget
# (Invariant 8). But returning immediately meant one transient 429 - outliving
# LiteLLM's own internal retries - permanently killed one format of seven for
# the whole job. Retry it here, bounded, on its own counter.
_PROVIDER_ATTEMPTS = 3
_PROVIDER_BACKOFF_SECONDS = 2.0


async def _with_provider_retry(coro_fn, *, what: str, artefact: Artefact):
    """Run coro_fn, retrying provider failures with exponential backoff.

    Raises the final ProviderError if every attempt fails. Every failure bumps
    the artefact's provider_error_count, which is diagnostic and gates nothing.
    """
    last: router.ProviderError | None = None
    for attempt in range(_PROVIDER_ATTEMPTS):
        try:
            return await coro_fn()
        except router.ProviderError as exc:
            last = exc
            artefact.provider_error_count += 1
            if attempt == _PROVIDER_ATTEMPTS - 1:
                break
            delay = _PROVIDER_BACKOFF_SECONDS * (2**attempt)
            log.warning(
                "%s: provider error on %s (attempt %d/%d), retrying in %.0fs: %s",
                artefact.output_type,
                what,
                attempt + 1,
                _PROVIDER_ATTEMPTS,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
    raise last  # type: ignore[misc]


async def run_artefact(
    format_id: str,
    content: ContentObject,
    analysis: AnalysisResult,
    parameters: Parameters,
    *,
    job_id: str = "",
    on_attempt=None,
    operator_instructions: str = "",
    approach: str = "",
    style_notes: list[str] | None = None,
) -> tuple[Artefact, str]:
    """Generate one artefact and drive it through QA until settled.

    Returns (artefact, operator_message). The message is non-empty only when the
    QA budget is exhausted and the job must stop.
    """
    spec = registry.get(format_id)
    settings = get_settings()
    artefact = Artefact(output_type=format_id, status=ArtefactStatus.GENERATING)
    # UC-09: a regenerate carries the OPERATOR's instructions, not machine fix
    # notes from a failed check. Seeded here so the first attempt already has
    # them; QA fix notes replace them on any subsequent retry.
    fix_notes: list[str] = [operator_instructions] if operator_instructions else []
    message = ""

    # Bounded by the QA budget: at most one initial attempt plus qa_max_retries.
    for attempt in range(settings.qa_max_retries + 1):
        try:
            # Loop variables bound as defaults: a bare closure would read
            # whatever fix_notes/attempt held when the retry finally ran, not
            # when it was scheduled (ruff B023).
            generated = await _with_provider_retry(
                lambda _notes=fix_notes, _n=attempt: generator.generate(
                    spec,
                    content,
                    analysis,
                    parameters,
                    fix_notes=_notes,
                    attempt=_n,
                    job_id=job_id,
                    approach=approach,
                    style_notes=style_notes or [],
                ),
                what="generation",
                artefact=artefact,
            )
        except ParseFailure as exc:
            # The model never spoke the protocol. Separate counter; the QA
            # budget is untouched (TC-0405, TC-0406).
            artefact.parse_retry_count += 1
            artefact.status = ArtefactStatus.FAILED
            artefact.error = str(exc)
            log.warning("%s: parse failure, QA counter unchanged: %s", format_id, exc)
            return artefact, ""
        except router.ProviderError as exc:
            # Every retry is spent. Infrastructure, so still diagnostic only and
            # never counted against quality (Invariant 8, TC-0609); the count
            # was already incremented per failed attempt.
            artefact.status = ArtefactStatus.FAILED
            artefact.error = f"provider error after {_PROVIDER_ATTEMPTS} attempts: {exc}"
            log.warning("%s: provider error, retries exhausted: %s", format_id, exc)
            return artefact, ""

        # Carry the counters forward; generate() returns a fresh artefact.
        generated.retry_count = artefact.retry_count
        generated.parse_retry_count = artefact.parse_retry_count
        generated.provider_error_count = artefact.provider_error_count
        generated.tone_retried = artefact.tone_retried
        generated.status = ArtefactStatus.QA
        artefact = generated

        try:
            qa = await _with_provider_retry(
                lambda _a=artefact: runner.run(
                    spec, _a, content, parameters, analysis, job_id=job_id
                ),
                what="QA",
                artefact=artefact,
            )
        except router.ProviderError as exc:
            artefact.status = ArtefactStatus.FAILED
            artefact.error = f"provider error during QA after {_PROVIDER_ATTEMPTS} attempts: {exc}"
            return artefact, ""

        artefact, message = verdict_policy.apply(qa, artefact)
        if on_attempt:
            on_attempt(attempt, artefact, qa)

        if artefact.qa_result.verdict is not Verdict.RETRY:
            if artefact.status in (ArtefactStatus.PASSED, ArtefactStatus.PASSED_FLAGGED):
                artefact.export_paths = _export(spec, artefact, job_id)
            return artefact, message

        fix_notes = artefact.qa_result.all_fix_notes()
        log.info(
            "%s: retry %d/%d with %d fix note(s)",
            format_id,
            artefact.retry_count,
            settings.qa_max_retries,
            len(fix_notes),
        )

    return artefact, message


async def run_job(
    job_id: str,
    content: ContentObject,
    parameters: Parameters,
    format_ids: list[str],
    *,
    analysis: AnalysisResult | None = None,
    style_notes: list[str] | None = None,
) -> dict:
    """Run a whole job: analyse once, then fan out across formats."""
    from app.agents import analysis as analysis_agent
    from app.tools import retrieval

    retrieval.bind_job(job_id, [content.source_id])
    try:
        # Analysis runs ONCE and is shared by every generator (Invariant 3).
        if analysis is None:
            analysis = await analysis_agent.analyse(content, parameters, job_id=job_id)

        # Capped: seven formats firing at once is the same rate-limit
        # scenario the QA cap already guards against, and on a free tier it is
        # the likeliest way to turn a healthy job into seven 429s (TC-0904).
        # The fan-out is still concurrent, just not unbounded.
        fanout = asyncio.Semaphore(get_settings().fanout_concurrency)

        async def _capped(fid: str):
            async with fanout:
                return await run_artefact(
                    fid,
                    content,
                    analysis,
                    parameters,
                    job_id=job_id,
                    style_notes=style_notes or [],
                )

        # One generator failing must not stop the others (TC-0407).
        results = await asyncio.gather(
            *(_capped(fid) for fid in format_ids),
            return_exceptions=True,
        )

        artefacts: dict[str, Artefact] = {}
        operator_message = ""
        for fid, result in zip(format_ids, results, strict=True):
            if isinstance(result, BaseException):
                log.exception("%s failed outright", fid, exc_info=result)
                artefacts[fid] = Artefact(
                    output_type=fid, status=ArtefactStatus.FAILED, error=str(result)
                )
                continue
            artefact, message = result
            artefacts[fid] = artefact
            if message and not operator_message:
                operator_message = message

        return {
            "job_id": job_id,
            "analysis": analysis,
            "artefacts": artefacts,
            "status": _job_status(artefacts, operator_message),
            "operator_message": operator_message or None,
        }
    finally:
        retrieval.unbind_job(job_id)


def _export(spec, artefact: Artefact, job_id: str) -> list[str]:
    """Render a passed artefact to files.

    Export runs through the tool registry so it is subject to the same
    allowlist as everything else, and so the call shows up in the audit trail.
    A failure here must not undo a passed artefact - the content is fine, only
    the file is missing.
    """
    import os

    from app.config import get_settings
    from app.tools.registry import Caller, call

    out_dir = os.path.join(get_settings().storage_dir, job_id, spec.id)
    try:
        return call(
            Caller.EXPORT,
            "render_document",
            artefact=artefact.content or {},
            renderer=spec.renderer,
            out_dir=out_dir,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("export failed for %s: %s", spec.id, exc)
        return []


def _job_status(artefacts: dict[str, Artefact], operator_message: str) -> JobStatus:
    """Report honestly. Partial completion is not "complete" (TC-0803)."""
    if operator_message:
        return JobStatus.STOPPED_QA_BUDGET

    if not artefacts:
        return JobStatus.FAILED_PERMANENT

    statuses = {a.status for a in artefacts.values()}
    if statuses <= {ArtefactStatus.PASSED, ArtefactStatus.PASSED_FLAGGED}:
        return JobStatus.DONE
    if all(
        a.status in (ArtefactStatus.FAILED,) and a.provider_error_count for a in artefacts.values()
    ):
        # Every artefact died on infrastructure: recoverable, worth retrying.
        return JobStatus.FAILED_RECOVERABLE
    if not statuses & {ArtefactStatus.PASSED, ArtefactStatus.PASSED_FLAGGED}:
        # Nothing was delivered. Reporting "done" for a job whose every
        # artefact was blocked or failed tells the operator to go and collect
        # output that does not exist.
        return JobStatus.FAILED_RECOVERABLE
    # A partial result: some artefacts landed, others did not. Still "done" in
    # the sense that no work remains, and the per-artefact statuses carry the
    # detail (TC-0803, TC-0610).
    return JobStatus.DONE
