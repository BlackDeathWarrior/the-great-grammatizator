"""Live progress, written as the run happens.

Artefacts and their QA verdicts are persisted in ONE transaction when a job
finishes (app/worker.py:_persist), and that atomicity is worth keeping: a
half-written artefact is worse than a late one. The cost is that the dashboard
saw nothing until the very end - a seventy-second job showed "analysing" for
sixty-eight seconds and then completed every stage at once, which tells an
operator watching a stuck job precisely nothing about which step is stuck.

This module is the other half: small rows, written outside that transaction,
as each step is reached.

Three rules it keeps:

1. **Never raise.** A progress write is a light on a board. If Postgres is
   busy, the job must not fail because the animation did - so every call
   swallows its own errors and logs them once.
2. **Never claim.** A stage is written `done` only where the work is actually
   finished. Nothing here predicts, extrapolates, or fills in a gap to keep a
   bar moving; a board that always shows progress cannot report a stall, which
   is the one thing it exists for.
3. **Disposable.** Losing a row loses a light, never a result. Nothing reads
   progress to make a decision, and the authoritative record stays the artefact
   rows.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

PENDING = "pending"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"

# Job-wide stages, in the order the operator meets them.
INGEST = "ingest"
ANALYSIS = "analysis"
GENERATE = "generate"
QA = "qa"


def report(
    job_id: str,
    stage: str,
    state: str,
    *,
    key: str = "",
    detail: str = "",
    data: dict | None = None,
) -> None:
    """Record one step. Best-effort by design; see rule 1 above."""
    if not job_id:
        # A test or a variant run with no job row to hang progress off. Not an
        # error - the graph is usable without a database.
        return

    try:
        from app.db.models import JobProgress
        from app.db.session import session_scope

        with session_scope() as session:
            row = session.get(JobProgress, {"job_id": job_id, "stage": stage, "key": key})
            if row is None:
                row = JobProgress(job_id=job_id, stage=stage, key=key)
                session.add(row)
            row.state = state
            row.detail = detail
            if data is not None:
                row.data = data
    except Exception:  # noqa: BLE001 - a board must never break a run
        log.debug("could not write progress %s/%s/%s", job_id, stage, key, exc_info=True)


def clear(job_id: str) -> None:
    """Drop a job's progress rows.

    Called when a run starts, so a regenerate does not show the previous run's
    checker verdicts as though they belonged to this one.
    """
    if not job_id:
        return

    try:
        from app.db.models import JobProgress
        from app.db.session import session_scope

        with session_scope() as session:
            session.query(JobProgress).filter_by(job_id=job_id).delete()
    except Exception:  # noqa: BLE001
        log.debug("could not clear progress for %s", job_id, exc_info=True)


def snapshot(job_id: str) -> dict[str, dict]:
    """Everything recorded for a job, keyed "stage:key".

    Returns {} on any failure, which callers must treat as "nothing known yet"
    rather than as "nothing happened" - the difference matters, and it is why
    the API derives a stage from artefact evidence FIRST and only consults
    progress to sharpen what it already knows.
    """
    if not job_id:
        return {}

    try:
        from app.db.models import JobProgress
        from app.db.session import session_scope

        with session_scope() as session:
            rows = session.query(JobProgress).filter_by(job_id=job_id).all()
            return {
                f"{r.stage}:{r.key}" if r.key else r.stage: {
                    "stage": r.stage,
                    "key": r.key,
                    "state": r.state,
                    "detail": r.detail,
                    "data": r.data or {},
                }
                for r in rows
            }
    except Exception:  # noqa: BLE001
        log.debug("could not read progress for %s", job_id, exc_info=True)
        return {}
