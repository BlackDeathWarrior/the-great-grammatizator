"""POST /jobs, GET /jobs/{id}, regenerate.

POST returns in under a second with a job_id (TC-0801): the HTTP request cannot
wait 30-90s for generation, so work goes to the queue.
"""

from __future__ import annotations

import hashlib
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
    # Who is asking, so the worker can load their learned preferences. Empty
    # is fine: the platform works anonymously, it just cannot learn.
    profile_id: str = ""


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
    # Alternatives awaiting a decision. Empty for the ordinary single-draft
    # path, so the dashboard renders variants only when there are some.
    variants: list[VariantOut] = []
    # None = not yet rated. Lets the buttons show which way this artefact went.
    liked: bool | None = None
    # A short digest of everything the dashboard draws for this artefact.
    #
    # The status region replaces itself wholesale every two seconds, so an
    # element's birth says nothing about whether work happened - a one-shot
    # animation keyed to it fires forever, and a sound keyed to it fires at
    # every poll. This changes only when something actually moved, which is
    # what lets the client tell a real event from a redraw.
    state_token: str = ""


class StageOut(BaseModel):
    """One phase of the run, as a light on a board.

    `state` is one of pending / active / done / failed, and every value is
    DERIVED FROM EVIDENCE rather than assumed: a stage shows done because
    something it produced exists (chunks, a cached analysis, an artefact row),
    not because the stage before it finished. A progress bar that runs ahead of
    the work is worse than no progress bar - it teaches the operator to
    distrust the one signal they have.
    """

    id: str
    label: str
    state: str
    # What the stage produced, in the operator's terms: "5 chunks", "3 of 7".
    detail: str = ""


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
    # The run as a sequence, for the pipeline view.
    stages: list[StageOut] = []
    # Live per-checker state while a job runs, keyed by output_type. Empty once
    # it settles - the artefact's own `qa` list carries the same verdicts and is
    # the authoritative record.
    live_checkers: dict[str, list[dict]] = {}
    # Degraded modes the operator should know about, surfaced rather than left
    # in a container log (POC.md §5: silent success is the worst failure mode).
    warnings: list[str] = []


class VariantOut(BaseModel):
    """One candidate the operator can choose. See app/graph/variants.py."""

    label: str
    approach: str
    status: str
    content: dict | None = None
    claims: list[dict] = []
    qa: list[dict] = []
    export_paths: list[str] = []
    chosen: bool = False


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
            profile_id=payload.profile_id or "",
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
    # One query rather than one per artefact.
    from app.db.models import Feedback

    feedback_by_artefact: dict[str, bool] = {
        f.artefact_id: f.liked
        for f in session.query(Feedback)
        .filter(Feedback.artefact_id.in_([a.id for a in job.artefacts] or [""]))
        .all()
    }

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
                variants=[
                    VariantOut(
                        label=v.label,
                        approach=v.approach,
                        status=v.status,
                        content=v.content,
                        claims=v.claims or [],
                        qa=v.qa or [],
                        export_paths=v.export_paths or [],
                        chosen=v.chosen,
                    )
                    for v in sorted(row.variants, key=lambda v: v.label)
                ],
                liked=feedback_by_artefact.get(row.id),
            )
        )

    total = len(artefacts)
    live = _checker_rows(job.id)

    # Stamp each artefact with a digest of what the dashboard will draw for it,
    # including the mid-flight checker states that live outside the row.
    for out in artefacts:
        out.state_token = _state_token(out, live.get(out.output_type, []))

    return JobDetail(
        job_id=job.id,
        status=str(job.status),
        progress=f"{settled} of {total} done",
        done_count=settled,
        total_count=total,
        operator_message=job.operator_message,
        parameters=job.parameters or {},
        artefacts=artefacts,
        stages=_stages(session, job, artefacts, settled),
        live_checkers=live,
        warnings=_active_warnings(),
    )


def _state_token(artefact: ArtefactOut, live: list[dict]) -> str:
    """A digest of everything about one artefact the operator can see change.

    Deliberately NOT a hash of the whole record: content is large and changes
    identity on every regeneration, which would report movement on a redraw of
    identical text. What is included is what an operator would call an event -
    a verdict landed, a checker advanced, a retry was spent, the status moved.
    """
    parts = [
        artefact.status,
        str(artefact.retry_count),
        str(artefact.parse_retry_count),
        str(artefact.provider_error_count),
        str(len(artefact.qa)),
        ",".join(f"{c.checker}:{c.passed}" for c in artefact.qa),
        ",".join(f"{c.get('checker')}:{c.get('state')}" for c in live),
        ",".join(f"{v.label}:{v.status}:{v.chosen}" for v in artefact.variants),
        str(artefact.liked),
        str(bool(artefact.content)),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _stages(session, job: Job, artefacts: list[ArtefactOut], settled: int) -> list[StageOut]:
    """The run as four lights.

    Evidence first, progress second. Each stage is derived from something that
    EXISTS - chunks on the source, analysis cached on the job row, artefacts
    with content - and the live progress rows only sharpen a stage that the
    evidence has already left ambiguous. Done that way round, losing the
    progress table degrades the board to the coarse-but-correct version rather
    than to a wrong one.

    Nothing here predicts. A stage that cannot be shown as finished stays
    active, because an operator watching a stalled job needs to see WHICH step
    stalled, and a board that always advances cannot tell them.
    """
    from app.db.models import Source
    from app.graph import progress as progress_rows

    live = progress_rows.snapshot(job.id)

    running = job.status in (JobStatus.QUEUED, JobStatus.RUNNING)
    stopped = job.status in (
        JobStatus.FAILED_RECOVERABLE,
        JobStatus.FAILED_PERMANENT,
        JobStatus.STOPPED_QA_BUDGET,
    )

    # 1. Ingest. Its output is chunks, and a job cannot exist without them, so
    #    by the time anyone is watching this is done.
    chunks = 0
    for link in job.sources:
        src = session.get(Source, link.source_id)
        if src is not None:
            chunks += len(src.chunks or [])
    ingest = StageOut(
        id="ingest",
        label="Ingest",
        state="done" if chunks else "active",
        detail=f"{chunks} chunk{'' if chunks == 1 else 's'}" if chunks else "reading the source",
    )

    # 2. Analysis. Cached on the job row once written (Invariant 3), but that
    #    only lands with the final transaction - so the live row is what makes
    #    this stage light up while the pass is actually happening.
    analysed = bool(job.analysis)
    live_analysis = live.get("analysis", {})
    if analysed:
        analysis_state = "done"
        analysis_detail = (job.analysis or {}).get("content_type", "") or "analysed"
    elif live_analysis:
        analysis_state = live_analysis["state"]
        analysis_detail = live_analysis["detail"]
    else:
        analysis_state = "active" if running else ("failed" if stopped else "pending")
        analysis_detail = "one shared pass"
    analysis = StageOut(
        id="analysis", label="Analyse", state=analysis_state, detail=analysis_detail
    )

    # 3. Generation. An artefact counts as written once it HAS content, not
    #    once its status leaves pending - which happens before the model has
    #    answered. Mid-run the count comes from the live rows.
    total = len(artefacts)
    persisted = sum(1 for a in artefacts if a.content)
    live_written = sum(
        1 for k, v in live.items() if v["stage"] == "generate" and v["key"] and v["state"] == "done"
    )
    written = max(persisted, live_written)
    generate = StageOut(
        id="generate",
        label="Generate",
        state=_phase(written, total, running, stopped, upstream_done=analysis_state == "done"),
        detail=f"{written} of {total}" if total else "waiting on formats",
    )

    # 4. QA. Counted by artefacts that reached a VERDICT, which is what the
    #    operator is waiting on - not by individual checker calls, of which
    #    there are six per artefact per attempt.
    live_verdicts = sum(
        1
        for v in live.values()
        if v["stage"] == "qa"
        and v["key"]
        and ":" not in v["key"]
        and v["state"] in ("done", "failed")
    )
    verdicts = max(settled, live_verdicts)
    qa = StageOut(
        id="qa",
        label="Check",
        state=_phase(verdicts, total, running, stopped, upstream_done=written > 0),
        detail=f"{verdicts} of {total} verdicts" if total else "six checkers each",
    )

    return [ingest, analysis, generate, qa]


def _phase(done: int, total: int, running: bool, stopped: bool, *, upstream_done: bool) -> str:
    """One stage's light, from a count against a total.

    Shared by generate and QA because they answer the same question and had
    started to disagree about the edge cases when written twice.
    """
    if total and done >= total:
        return "done"
    if stopped:
        return "failed" if not done else "done"
    if done or (running and upstream_done):
        return "active"
    return "pending"


def _checker_rows(job_id: str) -> dict[str, list[dict]]:
    """Per-artefact checker state, for the board's chips.

    Keyed by output_type. Only meaningful while a job runs; once it settles the
    artefact's own qa list is authoritative and carries the same verdicts.
    """
    from app.graph import progress as progress_rows

    out: dict[str, list[dict]] = {}
    for v in progress_rows.snapshot(job_id).values():
        if v["stage"] != "qa" or ":" not in (v["key"] or ""):
            continue
        output_type, checker = v["key"].split(":", 1)
        out.setdefault(output_type, []).append(
            {"checker": checker, "state": v["state"], "detail": v["detail"], **(v["data"] or {})}
        )
    for rows in out.values():
        rows.sort(key=lambda r: r["checker"])
    return out


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
