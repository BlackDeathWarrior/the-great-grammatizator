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


def _style_notes_for(session, profile_id: str) -> list[str]:
    """The operator's learned preferences, ready for a prompt.

    Empty for an anonymous job, which is the honest outcome: the platform
    works without a profile, it simply has nobody to learn from.
    """
    if not profile_id:
        return []

    from app.agents import preferences
    from app.db.models import OperatorProfile

    profile = session.get(OperatorProfile, profile_id)
    return preferences.notes_for_prompt(profile.style_notes if profile else [])


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
        # What this operator has been teaching us. Loaded here rather than in
        # build so the pipeline stays free of database concerns.
        style_notes = _style_notes_for(session, job.profile_id)

    try:
        result = await build.run_job(
            job_id, content, parameters, format_ids, style_notes=style_notes
        )
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
        style_notes = _style_notes_for(session, job.profile_id)

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
            style_notes=style_notes,
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


# Modules the worker needs that the API process does not exercise on its own.
# app and worker are separate images built from the same Dockerfile, so
# rebuilding one and not the other leaves this container running older code -
# and the miss surfaces as a JOB dying, minutes in, rather than as a failed
# build. Named here so a stale image says so at boot instead.
_REQUIRED_MODULES = ("cryptography",)


async def startup(ctx: dict) -> None:
    ctx["settings"] = get_settings()

    missing = []
    for name in _REQUIRED_MODULES:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)

    if missing:
        log.error(
            "WORKER IMAGE IS STALE: missing %s. Jobs will fail mid-run. "
            "Rebuild both images: docker compose build app worker "
            "&& docker compose up -d --force-recreate app worker",
            ", ".join(missing),
        )


async def variants_task(
    ctx: dict, job_id: str, output_type: str, count: int, profile_id: str
) -> dict:
    """Generate several takes on ONE artefact for the operator to choose between.

    Re-enters at generation like a regenerate, reusing the stored analysis, so
    the cost is n generations rather than n jobs (Invariant 3).
    """
    from app.agents import preferences
    from app.db.models import Artefact as ArtefactRow
    from app.db.models import Job, OperatorProfile, Variant
    from app.db.session import session_scope
    from app.graph import variants as variants_engine
    from app.graph.state import AnalysisResult, JobStatus, Parameters
    from app.ingest.service import to_content_object
    from app.tools import retrieval

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            return {"error": "unknown job"}
        source_row = job.sources[0].source if job.sources else None
        if source_row is None:
            job.status = JobStatus.FAILED_PERMANENT
            job.operator_message = "This job references no source."
            return {"error": "no source"}

        job.status = JobStatus.RUNNING
        content = to_content_object(source_row)
        parameters = Parameters(**(job.parameters or {}))
        stored_analysis = job.analysis
        style_notes = _style_notes_for(session, job.profile_id)

        profile = session.get(OperatorProfile, profile_id) if profile_id else None
        style_notes = preferences.notes_for_prompt(profile.style_notes if profile else [])

    analysis = AnalysisResult(**stored_analysis) if stored_analysis else None
    if analysis is None:
        from app.agents import analysis as analysis_agent

        analysis = await analysis_agent.analyse(content, parameters, job_id=job_id)

    retrieval.bind_job(job_id, [content.source_id])
    try:
        records = await variants_engine.run(
            output_type,
            content,
            analysis,
            parameters,
            job_id=job_id,
            count=count,
            style_notes=style_notes,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("variants failed for %s/%s", job_id, output_type)
        _fail_job(job_id, JobStatus.FAILED_RECOVERABLE, guardrails.redact(str(exc)))
        return {"error": str(exc)}
    finally:
        retrieval.unbind_job(job_id)

    with session_scope() as session:
        row = (
            session.query(ArtefactRow)
            .filter_by(job_id=job_id, output_type=output_type)
            .one_or_none()
        )
        if row is None:
            return {"error": "unknown artefact"}

        # A fresh round replaces the last one: stale options the operator never
        # picked would make the choice ambiguous.
        for old in list(row.variants):
            session.delete(old)
        session.flush()

        for rec in records:
            session.add(
                Variant(
                    artefact_id=row.id,
                    label=rec["label"],
                    approach=rec["approach"],
                    content=rec["content"],
                    claims=rec["claims"],
                    status=rec["status"],
                    qa=rec["qa"],
                    export_paths=rec["export_paths"],
                )
            )

        job = session.get(Job, job_id)
        if job is not None:
            job.status = JobStatus.DONE
            offered = len(variants_engine.offerable(records))
            job.operator_message = (
                f"{offered} version{'' if offered == 1 else 's'} of {output_type} "
                "ready to compare. Pick the one you prefer."
                if offered
                else f"No version of {output_type} passed the quality checks."
            )

    return {"job_id": job_id, "output_type": output_type, "variants": len(records)}


class WorkerSettings:
    functions = [ping, run_job_task, regenerate_task, variants_task]
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
