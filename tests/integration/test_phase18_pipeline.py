"""Phase 18: the live pipeline board.

Requires the compose stack.

What these tests defend is honesty rather than appearance. A progress board is
only worth having if a stalled run looks different from a healthy one, so the
assertions are mostly about what the board must REFUSE to say: no stage claimed
done without the output that proves it, no verdict colour on a checker that has
not decided, and no failure caused by the board itself.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.models import Artefact, Job, JobProgress, JobSource, Source
from app.db.session import session_scope
from app.graph import progress
from app.main import app

pytestmark = pytest.mark.integration

_JOB = "phase18job01"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def job():
    """A job with one source and two formats, built directly.

    Constructed rather than run: these tests are about how state is REPORTED,
    and driving a real generation would make them slow, rate-limit-dependent,
    and unable to pin the mid-run states that matter most.
    """
    with session_scope() as s:
        source = Source(
            source_hash="phase18hash",
            source_type="text",
            title="Phase 18",
            chunks=[{"chunk_id": "c1", "text": "one"}, {"chunk_id": "c2", "text": "two"}],
        )
        s.add(source)
        s.flush()
        row = Job(id=_JOB, status="running", formats=["linkedin_post", "advisory"])
        s.add(row)
        s.flush()
        s.add(JobSource(job_id=_JOB, source_id=source.id))
        s.add_all(
            [
                Artefact(job_id=_JOB, output_type="linkedin_post", status="pending"),
                Artefact(job_id=_JOB, output_type="advisory", status="pending"),
            ]
        )
        sid = source.id

    yield _JOB

    with session_scope() as s:
        s.query(JobProgress).filter_by(job_id=_JOB).delete()
        row = s.get(Job, _JOB)
        if row:
            s.delete(row)
        s.flush()
        src = s.get(Source, sid)
        if src:
            s.delete(src)


def _stages(client, job_id: str) -> dict[str, dict]:
    body = client.get(f"/jobs/{job_id}").json()
    return {st["id"]: st for st in body["stages"]}


# --- the progress store -----------------------------------------------------


def test_report_then_snapshot_round_trips(job):
    progress.report(job, progress.ANALYSIS, progress.ACTIVE, detail="one shared pass")
    snap = progress.snapshot(job)

    assert snap["analysis"]["state"] == "active"
    assert snap["analysis"]["detail"] == "one shared pass"


def test_reporting_the_same_step_twice_updates_rather_than_duplicates(job):
    progress.report(job, progress.QA, progress.ACTIVE, key="linkedin_post:tone")
    progress.report(job, progress.QA, progress.DONE, key="linkedin_post:tone", detail="0.86")

    snap = progress.snapshot(job)
    assert snap["qa:linkedin_post:tone"]["state"] == "done"
    with session_scope() as s:
        rows = s.query(JobProgress).filter_by(job_id=job, stage="qa").all()
        assert len(rows) == 1


def test_a_progress_write_never_raises(monkeypatch):
    """Rule 1: a light on a board must not be able to fail a job."""

    def broken_scope(*_a, **_k):
        raise RuntimeError("postgres is having a day")

    monkeypatch.setattr("app.db.session.session_scope", broken_scope)

    # Must not raise, and must degrade to "nothing known" rather than lying.
    progress.report("somejob", progress.QA, progress.DONE)
    progress.clear("somejob")
    assert progress.snapshot("somejob") == {}


def test_no_job_id_is_a_no_op_not_an_error():
    """The graph is usable without a database - variants and tests run that way."""
    progress.report("", progress.QA, progress.DONE)
    assert progress.snapshot("") == {}


def test_clear_drops_a_previous_run(job):
    progress.report(job, progress.QA, progress.DONE, key="linkedin_post:tone", detail="0.4")
    progress.clear(job)

    # A regenerate must not show the last run's verdicts as though they were
    # this one's.
    assert progress.snapshot(job) == {}


# --- stages on the API ------------------------------------------------------


def test_ingest_is_done_because_chunks_exist(client, job):
    """Evidence, not sequence: the stage is done because its output is there."""
    stages = _stages(client, job)
    assert stages["ingest"]["state"] == "done"
    assert "2 chunks" in stages["ingest"]["detail"]


def test_analysis_is_not_claimed_done_before_it_is_written(client, job):
    stages = _stages(client, job)
    assert stages["analysis"]["state"] != "done"


def test_analysis_shows_done_once_cached_on_the_job(client, job):
    with session_scope() as s:
        s.get(Job, job).analysis = {"content_type": "advisory"}

    stages = _stages(client, job)
    assert stages["analysis"]["state"] == "done"
    assert stages["analysis"]["detail"] == "advisory"


def test_a_live_row_lights_analysis_before_the_final_write(client, job):
    """The whole point of phase 18.

    Artefacts and analysis land in ONE transaction at the end of a run, so
    without live rows this stage stayed "pending" for the entire job and then
    jumped to done. An operator watching a stuck job learned nothing.
    """
    progress.report(job, progress.ANALYSIS, progress.ACTIVE, detail="one shared pass")

    stages = _stages(client, job)
    assert stages["analysis"]["state"] == "active"
    assert stages["analysis"]["detail"] == "one shared pass"


def test_generation_counts_written_artefacts_while_the_run_is_open(client, job):
    progress.report(job, progress.ANALYSIS, progress.DONE)
    progress.report(job, progress.GENERATE, progress.DONE, key="linkedin_post")

    stages = _stages(client, job)
    assert stages["generate"]["state"] == "active"
    assert stages["generate"]["detail"] == "1 of 2"


def test_generation_is_done_only_when_every_format_is_written(client, job):
    progress.report(job, progress.ANALYSIS, progress.DONE)
    for fid in ("linkedin_post", "advisory"):
        progress.report(job, progress.GENERATE, progress.DONE, key=fid)

    assert _stages(client, job)["generate"]["detail"] == "2 of 2"
    assert _stages(client, job)["generate"]["state"] == "done"


def test_qa_counts_verdicts_not_individual_checkers(client, job):
    """Six checkers run per artefact; the operator is waiting on the verdict."""
    for checker in ("grounding", "format", "tone", "safety", "editorial"):
        progress.report(job, progress.QA, progress.DONE, key=f"linkedin_post:{checker}")

    # Five checker rows, but no artefact-level verdict yet.
    assert _stages(client, job)["qa"]["detail"] == "0 of 2 verdicts"

    progress.report(job, progress.QA, progress.DONE, key="linkedin_post")
    assert _stages(client, job)["qa"]["detail"] == "1 of 2 verdicts"


def test_a_stalled_stage_does_not_advance_on_its_own(client, job):
    """The board's reason to exist.

    Nothing may extrapolate. With analysis done and nothing generated, generate
    reports active and QA stays pending - a bar that kept creeping would hide
    exactly the situation an operator opened the page to diagnose.
    """
    progress.report(job, progress.ANALYSIS, progress.DONE)

    stages = _stages(client, job)
    assert stages["generate"]["state"] == "active"
    assert stages["generate"]["detail"] == "0 of 2"
    assert stages["qa"]["state"] == "pending"


def test_stages_survive_an_empty_progress_table(client, job):
    """Degrade to coarse-but-correct, never to wrong.

    Progress rows are disposable. Losing them must cost detail, not accuracy,
    which is why the API derives from artefact evidence first.
    """
    with session_scope() as s:
        s.query(JobProgress).filter_by(job_id=job).delete()
        s.get(Job, job).analysis = {"content_type": "advisory"}

    stages = _stages(client, job)
    assert stages["ingest"]["state"] == "done"
    assert stages["analysis"]["state"] == "done"
    assert len(stages) == 4


# --- live checkers ----------------------------------------------------------


def test_live_checkers_carry_how_the_verdict_was_reached(client, job):
    """A pass and a pass without a model call are different facts.

    The architecture makes a claim about the deterministic checkers (TC-0503),
    and showing it beats asking the operator to take it on trust.
    """
    progress.report(
        job,
        progress.QA,
        progress.DONE,
        key="linkedin_post:format",
        detail="passed",
        data={"checker": "format", "passed": True, "deterministic": True, "cached": False},
    )

    body = client.get(f"/jobs/{job}").json()
    rows = body["live_checkers"]["linkedin_post"]
    assert rows[0]["checker"] == "format"
    assert rows[0]["deterministic"] is True


def test_an_in_flight_checker_is_neither_pass_nor_fail(client, job):
    """A checker with no verdict must not borrow a verdict colour."""
    progress.report(job, progress.QA, progress.ACTIVE, key="advisory:grounding", detail="checking")

    rows = client.get(f"/jobs/{job}").json()["live_checkers"]["advisory"]
    assert rows[0]["state"] == "active"
    assert rows[0]["state"] not in ("done", "failed")


def test_a_checker_that_errored_is_reported_not_hidden(client, job):
    """Fail-closed is only useful if the operator can see it happened."""
    progress.report(
        job,
        progress.QA,
        progress.FAILED,
        key="advisory:editorial",
        detail="could not run",
        data={"checker": "editorial", "passed": False, "checker_error": True},
    )

    rows = client.get(f"/jobs/{job}").json()["live_checkers"]["advisory"]
    assert rows[0]["state"] == "failed"
    assert rows[0]["checker_error"] is True


def test_checkers_are_grouped_by_artefact(client, job):
    progress.report(job, progress.QA, progress.DONE, key="linkedin_post:tone", detail="0.9")
    progress.report(job, progress.QA, progress.ACTIVE, key="advisory:tone", detail="checking")

    live = client.get(f"/jobs/{job}").json()["live_checkers"]
    assert set(live) == {"linkedin_post", "advisory"}
    assert live["linkedin_post"][0]["detail"] == "0.9"


def test_the_artefact_level_row_is_not_mistaken_for_a_checker(client, job):
    """A verdict row and a checker row share a stage but not a shape."""
    progress.report(job, progress.QA, progress.DONE, key="linkedin_post", detail="passed")

    live = client.get(f"/jobs/{job}").json()["live_checkers"]
    assert "linkedin_post" not in live


# --- the rendered board -----------------------------------------------------


def test_the_board_renders_four_named_stages(client, job):
    html = client.get(f"/ui/jobs/{job}/status").text
    assert 'class="pipeline"' in html
    for label in ("Ingest", "Analyse", "Generate", "Check"):
        assert f">{label}<" in html


def test_stage_state_is_not_carried_by_colour_alone(client, job):
    """WCAG 1.4.1: the state has to survive a monochrome screenshot."""
    progress.report(job, progress.ANALYSIS, progress.ACTIVE, detail="one shared pass")
    html = client.get(f"/ui/jobs/{job}/status").text

    # A class for CSS, and the state in text for everything else.
    assert "is-active" in html
    assert "one shared pass" in html
    assert "sr-only" in html


def test_live_chips_appear_before_any_verdict_is_stored(client, job):
    progress.report(job, progress.QA, progress.ACTIVE, key="advisory:grounding", detail="checking")
    html = client.get(f"/ui/jobs/{job}/status").text

    assert "checker live" in html
    assert "grounding" in html
