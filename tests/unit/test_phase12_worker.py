"""The worker had no tests at all.

Nothing covered the job lifecycle, _persist, the timeout path, arq's retry
behaviour, or regenerate_task - which is how a job could sit at RUNNING
forever and a regenerate could mark six unrelated artefacts complete.
"""

import asyncio

import pytest

from app import worker
from app.graph import build
from app.graph.state import Artefact, ArtefactStatus, JobStatus

# === arq must not silently rerun a non-idempotent job =======================


@pytest.mark.p0
def test_the_worker_does_not_retry_a_non_idempotent_task():
    """arq defaults to max_tries=5.

    A rerun regenerates every artefact and pays for every token again, behind
    the operator's back, for a job that already reported a failure.
    """
    assert worker.WorkerSettings.max_tries == 1


@pytest.mark.p1
def test_the_job_timeout_leaves_room_for_a_seven_format_job():
    assert worker.WorkerSettings.job_timeout >= 900


# === a cancelled job must not be left RUNNING ===============================


@pytest.mark.p0
async def test_a_cancelled_job_is_recorded_not_left_running(monkeypatch):
    """arq's job_timeout cancels the coroutine.

    Without handling, _persist never ran and the dashboard polled a job that
    would never settle - no error, no reason to retry, nothing to read.
    """
    recorded: dict = {}

    def fake_fail(job_id, status, message):
        recorded.update(job_id=job_id, status=status, message=message)

    monkeypatch.setattr(worker, "_fail_job", fake_fail)

    async def cancelled(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(build, "run_job", cancelled)
    _patch_job_lookup(monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        await worker.run_job_task({}, "j_1")

    assert recorded["status"] is JobStatus.FAILED_RECOVERABLE
    assert "ran out of time" in recorded["message"]
    assert "Traceback" not in recorded["message"]


@pytest.mark.p0
async def test_an_unhandled_failure_is_recorded_as_recoverable(monkeypatch):
    recorded: dict = {}
    monkeypatch.setattr(worker, "_fail_job", lambda j, s, m: recorded.update(status=s, message=m))

    async def boom(*_a, **_kw):
        raise RuntimeError("qdrant unreachable")

    monkeypatch.setattr(build, "run_job", boom)
    _patch_job_lookup(monkeypatch)

    out = await worker.run_job_task({}, "j_1")

    assert out["error"] == "qdrant unreachable"
    assert recorded["status"] is JobStatus.FAILED_RECOVERABLE
    assert "qdrant unreachable" in recorded["message"]


@pytest.mark.p1
async def test_the_operator_message_is_redacted(monkeypatch):
    """A provider's error body can echo prompt text into the dashboard."""
    recorded: dict = {}
    monkeypatch.setattr(worker, "_fail_job", lambda j, s, m: recorded.update(message=m))

    async def leaky(*_a, **_kw):
        raise RuntimeError("rejected prompt for alice@example.com")

    monkeypatch.setattr(build, "run_job", leaky)
    _patch_job_lookup(monkeypatch)

    await worker.run_job_task({}, "j_1")

    assert "alice@example.com" not in recorded["message"]
    assert "[redacted]" in recorded["message"]


def _patch_job_lookup(monkeypatch):
    """Stand in for the DB read at the top of run_job_task."""
    import app.db.session as session_mod

    class _Job:
        status = JobStatus.QUEUED
        parameters: dict = {}
        formats = ["linkedin_post"]
        profile_id = ""
        sources = [type("JS", (), {"source": _FakeSourceRow()})()]
        operator_message = None

    class _Session:
        def get(self, _model, _id):
            return _Job()

        def query(self, *_a, **_kw):
            return self

        def filter_by(self, **_kw):
            return self

        def all(self):
            return []

    import contextlib

    @contextlib.contextmanager
    def fake_scope():
        yield _Session()

    monkeypatch.setattr(session_mod, "session_scope", fake_scope)

    from app.ingest import service

    monkeypatch.setattr(service, "to_content_object", lambda _row: _fake_content())


class _FakeSourceRow:
    id = "s_1"
    chunks: list = []


def _fake_content():
    from app.graph.state import Chunk, ContentObject

    return ContentObject(
        source_id="s_1",
        source_hash="sha256:a",
        source_type="text",
        title="t",
        text="body",
        chunks=[Chunk(id="c1", source_id="s_1", text="body")],
    )


# === job status must be honest ==============================================


def _artefact(status: ArtefactStatus, provider_errors: int = 0) -> Artefact:
    return Artefact(
        output_type="linkedin_post", status=status, provider_error_count=provider_errors
    )


@pytest.mark.p0
def test_a_job_where_everything_was_blocked_is_not_done():
    """ "done" tells the operator to collect output that does not exist."""
    artefacts = {
        "linkedin_post": _artefact(ArtefactStatus.BLOCKED),
        "exec_summary": _artefact(ArtefactStatus.BLOCKED),
    }
    assert build._job_status(artefacts, "") is not JobStatus.DONE


@pytest.mark.p1
def test_a_partial_result_is_still_done():
    """Some landed, some did not; no work remains, and the badges carry detail."""
    artefacts = {
        "linkedin_post": _artefact(ArtefactStatus.PASSED),
        "exec_summary": _artefact(ArtefactStatus.BLOCKED),
    }
    assert build._job_status(artefacts, "") is JobStatus.DONE


@pytest.mark.p1
def test_an_all_passed_job_is_done():
    artefacts = {
        "linkedin_post": _artefact(ArtefactStatus.PASSED),
        "exec_summary": _artefact(ArtefactStatus.PASSED_FLAGGED),
    }
    assert build._job_status(artefacts, "") is JobStatus.DONE


@pytest.mark.p1
def test_an_all_infrastructure_failure_is_recoverable():
    artefacts = {
        "linkedin_post": _artefact(ArtefactStatus.FAILED, provider_errors=3),
        "exec_summary": _artefact(ArtefactStatus.FAILED, provider_errors=3),
    }
    assert build._job_status(artefacts, "") is JobStatus.FAILED_RECOVERABLE


@pytest.mark.p1
def test_the_qa_budget_message_still_wins():
    artefacts = {"linkedin_post": _artefact(ArtefactStatus.PASSED)}
    assert build._job_status(artefacts, "stopped") is JobStatus.STOPPED_QA_BUDGET


# === regenerate must not speak for the whole job ============================


@pytest.mark.p0
def test_regenerate_recomputes_status_instead_of_forcing_done():
    """It hardcoded JobStatus.DONE, marking six other artefacts complete."""
    import inspect

    source = inspect.getsource(worker.regenerate_task)
    assert '"status": JobStatus.DONE' not in source, "regenerate still forces the job to done"
    assert "_recompute_job_status" in source


@pytest.mark.p1
def test_regenerate_guards_a_job_with_no_source():
    """job.sources[0] was unguarded: an IndexError left the job RUNNING."""
    import inspect

    source = inspect.getsource(worker.regenerate_task)
    assert "if job.sources else None" in source
