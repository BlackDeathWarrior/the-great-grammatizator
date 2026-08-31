"""Phase 10: the dashboard and the jobs API.

Requires the compose stack.
"""

import pytest
from fastapi.testclient import TestClient

from app.db.models import Job, Source
from app.db.session import session_scope
from app.formats import registry
from app.ingest import embed
from app.main import app

pytestmark = pytest.mark.integration

ADVISORY = (
    "Critical authentication bypass in NetGuard Connect Secure. A vulnerability "
    "rated 9.1 out of 10 allows an unauthenticated attacker to bypass "
    "authentication. A fix is available in version 22.7R2.6."
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def source_id(client):
    r = client.post("/sources", data={"text": ADVISORY})
    assert r.status_code == 201, r.text
    sid = r.json()["source_id"]
    yield sid
    with session_scope() as s:
        for job in s.query(Job).all():
            s.delete(job)
        row = s.get(Source, sid)
        if row:
            embed.delete_source(sid)
            s.delete(row)


# --- job creation -----------------------------------------------------------


@pytest.mark.p0
def test_post_jobs_returns_immediately_with_a_job_id(client, source_id):
    """TC-0801: under a second. The request cannot wait 30-90s for generation."""
    import time

    start = time.monotonic()
    r = client.post(
        "/jobs",
        json={"source_ids": [source_id], "formats": ["linkedin_post"], "parameters": {}},
    )
    elapsed = time.monotonic() - start

    assert r.status_code == 202
    assert r.json()["job_id"]
    assert r.json()["status"] == "queued"
    assert elapsed < 1.0, f"POST /jobs took {elapsed:.2f}s"


@pytest.mark.p0
def test_unknown_format_is_rejected_before_the_job_exists(client, source_id):
    """TC-0301: rejected before any job is created."""
    before = _job_count()
    r = client.post(
        "/jobs",
        json={"source_ids": [source_id], "formats": ["linkedin_post", "telepathy"]},
    )
    assert r.status_code == 400
    assert "telepathy" in r.json()["detail"]
    assert _job_count() == before, "a rejected selection still created a job"


@pytest.mark.p1
def test_unknown_source_is_rejected(client):
    r = client.post("/jobs", json={"source_ids": ["s_nope"], "formats": ["linkedin_post"]})
    assert r.status_code == 404


# --- status reporting -------------------------------------------------------


@pytest.mark.p0
def test_status_reports_partial_progress_honestly(client, source_id):
    """TC-0803: "0 of 7 done", never "complete" while work remains."""
    formats = registry.ids()
    job_id = client.post("/jobs", json={"source_ids": [source_id], "formats": formats}).json()[
        "job_id"
    ]

    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["total_count"] == len(formats)
    assert detail["done_count"] == 0
    assert detail["progress"] == f"0 of {len(formats)} done"
    assert "complete" not in detail["progress"].lower()


@pytest.mark.p0
def test_one_artefact_record_per_selected_format(client, source_id):
    """UC-03."""
    job_id = client.post(
        "/jobs",
        json={"source_ids": [source_id], "formats": ["linkedin_post", "exec_summary"]},
    ).json()["job_id"]

    detail = client.get(f"/jobs/{job_id}").json()
    assert {a["output_type"] for a in detail["artefacts"]} == {"linkedin_post", "exec_summary"}


@pytest.mark.p1
def test_unknown_job_returns_404(client):
    assert client.get("/jobs/nonexistent").status_code == 404


# --- dashboard --------------------------------------------------------------


@pytest.mark.p0
def test_dashboard_offers_every_registered_format(client, source_id):
    """The multi-select is driven by the registry, so a new format appears free."""
    html = client.get(f"/?source_id={source_id}").text
    for spec in registry.all_formats():
        assert f'value="{spec.id}"' in html, f"{spec.id} missing from the dashboard"
        assert spec.label in html


@pytest.mark.p0
def test_parameters_are_dropdowns_not_free_text(client, source_id):
    """UC-02, TC-0202: closed vocabularies.

    Free text gives the tone checker nothing concrete to compare against.
    """
    import re

    html = client.get(f"/?source_id={source_id}").text

    selects = set(re.findall(r'<select id="(\w+)"', html))
    assert {"audience", "tone", "detail", "objective", "style"} <= selects

    # No free-text input may carry a parameter name.
    for field in selects:
        assert f'<input type="text" id="{field}"' not in html


@pytest.mark.p1
def test_recent_sources_are_listed_for_reuse(client, source_id):
    """UC-12, TC-0807: selecting an existing source skips ingestion."""
    html = client.get("/").text
    assert source_id in html
    assert "Recent sources" in html


@pytest.mark.p0
def test_status_partial_renders_every_artefact(client, source_id):
    """TC-0610: blocked and flagged artefacts still surface, marked.

    Hiding them leaves the operator wondering where their deck went.
    """
    from app.db.models import Artefact
    from app.graph.state import ArtefactStatus

    job_id = client.post(
        "/jobs",
        json={"source_ids": [source_id], "formats": ["linkedin_post", "exec_summary"]},
    ).json()["job_id"]

    # Force one blocked and one passed.
    with session_scope() as s:
        rows = s.query(Artefact).filter_by(job_id=job_id).all()
        rows[0].status = ArtefactStatus.BLOCKED
        rows[1].status = ArtefactStatus.PASSED

    html = client.get(f"/ui/jobs/{job_id}/status").text
    assert "blocked" in html, "a blocked artefact was hidden from the operator"
    assert "passed" in html
    assert "2 of 2 done" in html


@pytest.mark.p1
def test_operator_message_is_surfaced_not_a_stack_trace(client, source_id):
    """TC-0805: after 3 strikes the operator sees a restart instruction."""
    job_id = client.post(
        "/jobs", json={"source_ids": [source_id], "formats": ["linkedin_post"]}
    ).json()["job_id"]

    with session_scope() as s:
        job = s.get(Job, job_id)
        job.operator_message = (
            "Quality checks failed three times for linkedin_post. The job has "
            "been stopped. Review the findings, adjust, and start a new job."
        )

    html = client.get(f"/ui/jobs/{job_id}/status").text
    assert "Job stopped" in html
    assert "start a new job" in html
    assert "Traceback" not in html


# --- downloads --------------------------------------------------------------


@pytest.mark.p0
@pytest.mark.parametrize(
    "attack",
    ["../../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd", "....//....//etc/passwd"],
)
def test_download_refuses_path_traversal(client, attack):
    """The download path is built from URL segments, so it must be contained.

    Not in the TC register - found while writing the endpoint. An operator-facing
    download route that accepts arbitrary paths would serve any file in the
    container.
    """
    r = client.get(f"/downloads/jobid/linkedin_post/{attack}")
    assert r.status_code == 404
    assert b"root:" not in r.content


@pytest.mark.p1
def test_download_serves_a_real_rendered_file(client, tmp_path, monkeypatch):
    """TC-0706: the returned URL resolves to the written asset."""
    from app.config import get_settings
    from app.export import dispatch

    settings = get_settings()
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))

    out_dir = tmp_path / "job1" / "linkedin_post"
    paths = dispatch.render(
        {"hook": "h", "body": "b", "call_to_action": "c", "hashtags": [], "claims": []},
        "markdown",
        str(out_dir),
        basename="artefact",
    )
    filename = paths[0].split("/")[-1].split("\\")[-1]

    r = client.get(f"/downloads/job1/linkedin_post/{filename}")
    assert r.status_code == 200
    assert b"h" in r.content


def _job_count() -> int:
    with session_scope() as s:
        return s.query(Job).count()
