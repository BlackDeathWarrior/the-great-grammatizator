"""arq worker entrypoint. `arq app.worker.WorkerSettings`.

The queue exists because the HTTP request cannot wait 30-90s for generation,
and because rate limiting and backoff live here (ARCHITECTURE.md sec.2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from arq.connections import RedisSettings

from app.config import get_settings
from app.gateway import guardrails

log = logging.getLogger(__name__)


def _fail_job(job_id: str, status, message: str) -> None:
    """Record a terminal failure. Best effort: never mask the original fault.

    A job left at RUNNING is the worst outcome - the operator has no error to
    read and no reason to retry - so this runs even on the cancellation path.
    """
    from app.db.models import Job
    from app.db.session import session_scope

    try:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is not None:
                job.status = status
                job.operator_message = message
    except Exception:  # noqa: BLE001 - the original failure matters more
        log.exception("could not record the failure of job %s", job_id)


async def ping(ctx: dict) -> dict[str, str]:
    """Liveness probe for the queue itself."""
    return {"pong": datetime.now(UTC).isoformat()}


async def run_job_task(ctx: dict, job_id: str) -> dict:
    """Run a whole job and persist every artefact and QA verdict."""
    from app.db.models import Job
    from app.db.session import session_scope
    from app.graph import build
    from app.graph.state import JobStatus, Parameters
    from app.ingest.service import to_content_object

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            log.error("run_job_task: unknown job %s", job_id)
            return {"error": "unknown job"}

        job.status = JobStatus.RUNNING
        source_row = job.sources[0].source if job.sources else None
        if source_row is None:
            job.status = JobStatus.FAILED_PERMANENT
            job.operator_message = "This job references no source."
            return {"error": "no source"}

        content = to_content_object(source_row)
        parameters = Parameters(**(job.parameters or {}))
        format_ids = list(job.formats or [])

    try:
        result = await build.run_job(job_id, content, parameters, format_ids)
    except asyncio.CancelledError:
        # arq's job_timeout cancels the coroutine. Without this the job stayed
        # RUNNING in the database forever: _persist never ran, no message was
        # written, and the dashboard polled a job that would never settle.
        # Re-raised after recording - swallowing a cancellation lies to the
        # event loop about whether we actually stopped.
        log.error("job %s cancelled (job_timeout is %ss)", job_id, WorkerSettings.job_timeout)
        _fail_job(
            job_id,
            JobStatus.FAILED_RECOVERABLE,
            "The job ran out of time and was stopped. Nothing was lost - start "
            "it again, or select fewer formats to shorten it.",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        # Unhandled failures are recoverable by default: the operator can
        # retry, and a wrong "permanent" would tell them not to. Redacted: a
        # provider's error body can echo back prompt text, and this string is
        # rendered straight into the dashboard.
        _fail_job(
            job_id,
            JobStatus.FAILED_RECOVERABLE,
            guardrails.redact(f"The job failed: {exc}"),
        )
        return {"error": str(exc)}

    _persist(job_id, result)

    # The worker process outlives one job, but traces buffer; flush so a
    # completed job is visible in Langfuse immediately rather than whenever
    # the buffer next fills.
    from app.observability import flush

    flush()

    return {"job_id": job_id, "status": str(result["status"])}


async def regenerate_task(ctx: dict, job_id: str, output_type: str, instructions: str) -> dict:
    """UC-09: regenerate ONE artefact, re-entering at generation.

    The source and the cached analysis are untouched, so this costs one
    artefact rather than a whole job (TC-0808).
    """
    from app.db.models import Job
    from app.db.session import session_scope
    from app.graph import build
    from app.graph.state import AnalysisResult, JobStatus, Parameters
    from app.ingest.service import to_content_object
    from app.tools import retrieval

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": "unknown job"}
        # run_job_task guards this; regenerate did not, and an IndexError here
        # left the job RUNNING with no message.
        source_row = job.sources[0].source if job.sources else None
        if source_row is None:
            job.status = JobStatus.FAILED_PERMANENT
            job.operator_message = "This job references no source."
            return {"error": "no source"}
        job.status = JobStatus.RUNNING
        content = to_content_object(source_row)
        parameters = Parameters(**(job.parameters or {}))
        stored_analysis = job.analysis

    # Reuse the stored analysis. Recomputing it would defeat the whole point of
    # re-entering at generation.
    analysis = AnalysisResult(**stored_analysis) if stored_analysis else None
    if analysis is None:
        from app.agents import analysis as analysis_agent

        analysis = await analysis_agent.analyse(content, parameters, job_id=job_id)

    retrieval.bind_job(job_id, [content.source_id])
    try:
        artefact, message = await build.run_artefact(
            output_type,
            content,
            analysis,
            parameters,
            job_id=job_id,
            # Operator instructions, not machine fix notes (UC-09).
            operator_instructions=instructions,
        )
    finally:
        retrieval.unbind_job(job_id)

    _persist(
        job_id,
        {
            "artefacts": {output_type: artefact},
            "analysis": analysis,
            # Recomputed from every artefact, not hardcoded. Forcing DONE here
            # marked the whole job complete on the strength of one regenerated
            # artefact, even when the other six were blocked or still failing.
            "status": _recompute_job_status(job_id, output_type, artefact, message),
            "operator_message": message or None,
        },
    )
    return {"job_id": job_id, "output_type": output_type, "status": str(artefact.status)}


def _recompute_job_status(job_id: str, output_type: str, regenerated, message: str):
    """Job status across ALL artefacts, with the regenerated one substituted in."""
    from app.db.models import Artefact as ArtefactRow
    from app.db.session import session_scope
    from app.graph import build
    from app.graph.state import Artefact, ArtefactStatus

    with session_scope() as session:
        rows = session.query(ArtefactRow).filter_by(job_id=job_id).all()
        others = {
            r.output_type: Artefact(
                output_type=r.output_type,
                status=ArtefactStatus(r.status),
                provider_error_count=r.provider_error_count or 0,
            )
            for r in rows
            if r.output_type != output_type
        }

    others[output_type] = regenerated
    return build._job_status(others, message)


def _persist(job_id: str, result: dict) -> None:
    """Write artefacts and per-attempt QA rows back to Postgres."""
    from app.db.models import Artefact as ArtefactRow
    from app.db.models import Job, QAResultRow
    from app.db.session import session_scope

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return

        analysis = result.get("analysis")
        if analysis is not None:
            job.analysis = analysis.model_dump()
            job.provenance = analysis.provenance

        job.status = result["status"]
        job.operator_message = result.get("operator_message")

        for output_type, artefact in result["artefacts"].items():
            row = next((a for a in job.artefacts if a.output_type == output_type), None)
            if row is None:
                row = ArtefactRow(output_type=output_type)
                job.artefacts.append(row)

            row.status = artefact.status
            row.content = artefact.content
            row.claims = [c.model_dump() for c in artefact.claims]
            row.retry_count = artefact.retry_count
            row.parse_retry_count = artefact.parse_retry_count
            row.provider_error_count = artefact.provider_error_count
            row.tone_retried = artefact.tone_retried
            row.export_paths = artefact.export_paths
            row.error = artefact.error

            if artefact.qa_result:
                # Per attempt, not overwritten: the dashboard and eval runs need
                # to see tone 0.61 -> 0.84 across a retry.
                attempt = artefact.retry_count
                session.flush()
                existing = {(q.checker, q.attempt) for q in row.qa_results}
                for checker in artefact.qa_result.results:
                    if (str(checker.checker), attempt) in existing:
                        continue
                    row.qa_results.append(
                        QAResultRow(
                            attempt=attempt,
                            checker=str(checker.checker),
                            passed=checker.passed,
                            score=checker.score,
                            reason=checker.reason,
                            fix_notes=checker.fix_notes,
                        )
                    )


async def startup(ctx: dict) -> None:
    ctx["settings"] = get_settings()


class WorkerSettings:
    functions = [ping, run_job_task, regenerate_task]
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Generation plus QA for seven formats needs room; the default 300s would
    # kill a healthy multi-format job.
    job_timeout = 900
    # These tasks are NOT idempotent: a rerun regenerates every artefact and
    # pays for every token again. arq's default of 5 would do that silently on
    # any failure the task did not handle itself, so failures are surfaced to
    # the operator once instead of retried behind their back.
    max_tries = 1
