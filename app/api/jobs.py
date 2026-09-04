"""POST /jobs, GET /jobs/{id}, regenerate.

POST returns in under a second with a job_id (TC-0801): the HTTP request cannot
wait 30-90s for generation, so work goes to the queue.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db.models import Artefact, Job, JobSource, Source
from app.db.session import session_scope
from app.formats import registry
from app.graph.state import ArtefactStatus, JobStatus, Parameters

log = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


class JobIn(BaseModel):
    source_ids: list[str] = Field(min_length=1)
    formats: list[str] = Field(min_length=1)
    parameters: Parameters = Parameters()


class JobOut(BaseModel):
    job_id: str
    status: str


class CheckerOut(BaseModel):
    checker: str
    passed: bool
    score: float | None
    reason: str
    fix_notes: list[str]


class ArtefactOut(BaseModel):
    output_type: str
    label: str
    status: str
    content: dict | None
    claims: list[dict]
    qa: list[CheckerOut]
    retry_count: int
    parse_retry_count: int
    provider_error_count: int
    export_paths: list[str]
    error: str | None


class JobDetail(BaseModel):
    job_id: str
    status: str
    # Honest partial reporting: "4 of 7 done", never "complete" (TC-0803).
    progress: str
    done_count: int
    total_count: int
    operator_message: str | None
    parameters: dict
    artefacts: list[ArtefactOut]
    # Degraded modes the operator should know about, surfaced rather than left
    # in a container log (POC.md §5: silent success is the worst failure mode).
    warnings: list[str] = []


@router.post("/jobs", response_model=JobOut, status_code=202)
async def create_job(payload: JobIn) -> JobOut:
    """Enqueue a job. Returns immediately (TC-0801)."""
    # Validate the whole selection BEFORE creating anything (TC-0301).
    try:
        registry.validate_selection(payload.formats)
    except registry.UnknownFormat as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    with session_scope() as session:
        found = session.query(Source).filter(Source.id.in_(payload.source_ids)).all()
        missing = set(payload.source_ids) - {s.id for s in found}
        if missing:
            raise HTTPException(404, f"Unknown source(s): {', '.join(sorted(missing))}")

        job = Job(
            status=JobStatus.QUEUED,
            parameters=payload.parameters.model_dump(),
            formats=payload.formats,
        )
        for source_id in payload.source_ids:
            job.sources.append(JobSource(source_id=source_id))
        for format_id in payload.formats:
            job.artefacts.append(Artefact(output_type=format_id))
        session.add(job)
        session.flush()
        job_id = job.id

    await _enqueue(job_id)
    return JobOut(job_id=job_id, status=JobStatus.QUEUED.value)


async def _enqueue(job_id: str) -> None:
    """Hand off to the worker.

    If redis is unreachable the job stays queued rather than vanishing, and the
    operator sees it stuck instead of silently losing work.
    """
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.config import get_settings

    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        await pool.enqueue_job("run_job_task", job_id)
    except Exception as exc:  # noqa: BLE001
        log.error("could not enqueue job %s: %s", job_id, exc)


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: str) -> JobDetail:
    """Status plus per-artefact state. Polled by the dashboard (TC-0802)."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, f"Unknown job {job_id}")
        return _detail(session, job)


@router.post("/jobs/{job_id}/artefacts/{output_type}/regenerate", response_model=JobOut)
async def regenerate(job_id: str, output_type: str, instructions: str = "") -> JobOut:
    """UC-09: re-enter at GENERATION, not ingestion.

    Source and analysis are untouched, so a regenerate costs one artefact, not
    a whole job (TC-0808).
    """
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, f"Unknown job {job_id}")

        artefact = next((a for a in job.artefacts if a.output_type == output_type), None)
        if artefact is None:
            raise HTTPException(404, f"Job {job_id} has no {output_type} artefact")

        # Reset only this artefact. Counters start fresh because the operator is
        # asking for something different, not retrying the same request.
        artefact.status = ArtefactStatus.PENDING
        artefact.retry_count = 0
        artefact.parse_retry_count = 0
        artefact.error = None
        job.status = JobStatus.QUEUED
        session.flush()

    await _enqueue_regenerate(job_id, output_type, instructions)
    return JobOut(job_id=job_id, status=JobStatus.QUEUED.value)


async def _enqueue_regenerate(job_id: str, output_type: str, instructions: str) -> None:
    from arq import create_pool
    from arq.connections import RedisSettings

    from app.config import get_settings

    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        await pool.enqueue_job("regenerate_task", job_id, output_type, instructions)
    except Exception as exc:  # noqa: BLE001
        log.error("could not enqueue regenerate for %s/%s: %s", job_id, output_type, exc)


def _detail(session, job: Job) -> JobDetail:
    artefacts = []
    settled = 0

    for row in sorted(job.artefacts, key=lambda a: a.output_type):
        try:
            label = registry.get(row.output_type).label
        except registry.UnknownFormat:
            label = row.output_type

        if row.status in (
            ArtefactStatus.PASSED,
            ArtefactStatus.PASSED_FLAGGED,
            ArtefactStatus.BLOCKED,
            ArtefactStatus.FAILED,
        ):
            settled += 1

        # Only the latest attempt's verdicts, so the panel shows the current
        # state rather than every historical score.
        latest = max((q.attempt for q in row.qa_results), default=0)
        checkers = [
            CheckerOut(
                checker=q.checker,
                passed=q.passed,
                score=q.score,
                reason=q.reason,
                fix_notes=q.fix_notes or [],
            )
            for q in sorted(row.qa_results, key=lambda q: q.checker)
            if q.attempt == latest
        ]

        artefacts.append(
            ArtefactOut(
                output_type=row.output_type,
                label=label,
                status=str(row.status),
                content=row.content,
                claims=row.claims or [],
                qa=checkers,
                retry_count=row.retry_count,
                parse_retry_count=row.parse_retry_count,
                provider_error_count=row.provider_error_count,
                export_paths=row.export_paths or [],
                error=row.error,
            )
        )

    total = len(artefacts)
    return JobDetail(
        job_id=job.id,
        status=str(job.status),
        progress=f"{settled} of {total} done",
        done_count=settled,
        total_count=total,
        operator_message=job.operator_message,
        parameters=job.parameters or {},
        artefacts=artefacts,
        warnings=_active_warnings(),
    )


def _active_warnings() -> list[str]:
    """Degraded modes worth telling the operator about."""
    from app.ingest import embed

    return [embed.degraded_reason()] if embed.is_degraded() else []


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(limit: int = 20) -> list[JobOut]:
    """Job history (UC-12, TC-0806)."""
    with session_scope() as session:
        rows = session.query(Job).order_by(Job.created_at.desc()).limit(limit).all()
        return [JobOut(job_id=j.id, status=str(j.status)) for j in rows]
