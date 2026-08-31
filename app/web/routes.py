"""Dashboard routes. Jinja + HTMX, served by FastAPI - no separate build step.

ARCHITECTURE.md sec.1: upload, recent sources, parameters, format multi-select,
job status polling, per-artefact results with QA findings, download.
"""

from __future__ import annotations

import logging
import os
import pathlib

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api import jobs as jobs_api
from app.config import get_settings
from app.db.models import Job, Source
from app.db.session import session_scope
from app.formats import registry
from app.graph.state import Parameters
from app.ingest import service
from app.ingest.errors import IngestError

log = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])

TEMPLATES = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))

# Closed vocabularies, never free text (UC-02, TC-0202). A dropdown gives the
# tone checker something concrete to compare against; free text gives it noise.
VOCAB: dict[str, list[str]] = {
    "audience": [
        "general public",
        "security leaders",
        "engineers",
        "executives",
        "undergraduate students",
    ],
    "tone": ["informative", "informative, urgent", "explanatory, engaging", "formal", "plain"],
    "language": ["en"],
    "detail": ["brief", "moderate", "detailed"],
    "objective": [
        "inform",
        "drive awareness and patching",
        "support decision making",
        "support close reading for a seminar",
    ],
    "style": ["plain, no jargon", "accessible, analytical", "technical", "conversational"],
}


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, source_id: str | None = None):
    with session_scope() as session:
        rows = session.query(Source).order_by(Source.created_at.desc()).limit(10).all()
        sources = [
            {
                "source_id": r.id,
                "title": r.title or "Untitled",
                "source_type": str(r.source_type),
                "chunk_count": len(r.chunks or []),
            }
            for r in rows
        ]
        recent_jobs = [
            {"job_id": j.id, "status": str(j.status)}
            for j in session.query(Job).order_by(Job.created_at.desc()).limit(8).all()
        ]

    selected = next((s for s in sources if s["source_id"] == source_id), None)
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "sources": sources,
            "selected_source": selected,
            "formats": registry.all_formats(),
            "vocab": VOCAB,
            "jobs": recent_jobs,
        },
    )


@router.post("/ui/sources")
async def ui_ingest(
    file: UploadFile | None = File(default=None),
    url: str | None = Form(default=None),
    text: str | None = Form(default=None),
):
    """Ingest, then land the operator on parameters for that source."""
    data = await file.read() if file and file.filename else None
    filename = file.filename if file and file.filename else ""

    if data is None and url:
        from app.ingest.extract import html

        try:
            data = html.fetch(url).encode("utf-8")
            filename = "fetched.html"
        except IngestError as exc:
            raise HTTPException(exc.http_status, exc.message) from exc

    if data is None and not (text or "").strip():
        raise HTTPException(400, "Provide a file, a URL, or some text.")

    try:
        with session_scope() as session:
            content, _reused = service.ingest(
                session, data=data, filename=filename, text=text, url=url
            )
            source_id = content.source_id
    except IngestError as exc:
        raise HTTPException(exc.http_status, exc.message) from exc

    return RedirectResponse(f"/?source_id={source_id}", status_code=303)


@router.post("/ui/jobs")
async def ui_create_job(request: Request):
    """Create a job from the form, then redirect to its live view."""
    form = await request.form()
    source_id = form.get("source_id")
    formats = form.getlist("formats")

    if not formats:
        raise HTTPException(400, "Select at least one output format.")

    parameters = Parameters(
        **{field: form.get(field) or getattr(Parameters(), field) for field in VOCAB}
    )

    result = await jobs_api.create_job(
        jobs_api.JobIn(source_ids=[source_id], formats=formats, parameters=parameters)
    )
    return RedirectResponse(f"/jobs/{result.job_id}/view", status_code=303)


@router.get("/jobs/{job_id}/view", response_class=HTMLResponse)
async def job_view(request: Request, job_id: str):
    return TEMPLATES.TemplateResponse(request, "job.html", {"job_id": job_id})


@router.get("/ui/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str):
    """HTMX polling target (TC-0802).

    Blocked and flagged artefacts are rendered, marked - hiding them leaves the
    operator wondering where their deck went (TC-0610).
    """
    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


@router.post("/ui/jobs/{job_id}/artefacts/{output_type}/regenerate")
async def ui_regenerate(job_id: str, output_type: str, instructions: str = Form(default="")):
    await jobs_api.regenerate(job_id, output_type, instructions)
    return RedirectResponse(f"/jobs/{job_id}/view", status_code=303)


@router.get("/downloads/{job_id}/{output_type}/{filename}")
async def download(job_id: str, output_type: str, filename: str):
    """Serve a rendered artefact (TC-0706).

    Paths are rebuilt from the URL segments rather than taken from the request,
    and the result must stay inside the storage root - a filename like
    "../../etc/passwd" resolves outside it and is refused.
    """
    root = pathlib.Path(get_settings().storage_dir).resolve()
    candidate = (root / job_id / output_type / filename).resolve()

    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(404, "No such file")

    return FileResponse(candidate, filename=os.path.basename(candidate))
