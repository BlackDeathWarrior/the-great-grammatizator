"""Dashboard routes. Jinja + HTMX, served by FastAPI - no separate build step.

ARCHITECTURE.md sec.1: upload, recent sources, parameters, format multi-select,
job status polling, per-artefact results with QA findings, download.
"""

from __future__ import annotations

import json
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
from app.web.errors import explain as explain_error

log = logging.getLogger(__name__)
router = APIRouter(tags=["ui"])

TEMPLATES = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))

# Registered as a filter rather than passed in context: status.html is rendered
# by five different endpoints, and a context key would have to be remembered at
# every one of them - which is exactly the kind of thing that gets forgotten in
# the endpoint added next.
TEMPLATES.env.filters["explain_error"] = explain_error

PROFILE_COOKIE = "operator"
THEME_COOKIE = "theme"


def _shell(request: Request, active_tab: str) -> dict:
    """Context every page needs: who is asking, and which palette to draw in.

    Centralised because the header renders on every route. A page that forgot
    to pass `theme` would silently fall back to the OS preference and quietly
    ignore a choice the operator had made, which looks like the setting is
    broken rather than unset.
    """
    from app.db.models import OperatorProfile

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    name, theme = "", "system"
    if profile_id:
        with session_scope() as session:
            profile = session.get(OperatorProfile, profile_id)
            if profile is not None:
                name, theme = profile.name, (profile.theme or "system")

    # An anonymous operator still gets a theme; it just lives in a cookie
    # instead of a profile, because there is no row to hang it on.
    if not profile_id:
        theme = request.cookies.get(THEME_COOKIE, "system")

    return {
        "profile_name": name,
        "theme": theme,
        "active_tab": active_tab,
        "asset_version": _asset_version(),
    }


def _asset_version() -> str:
    """Fingerprint for the static asset URLs, from the newest mtime among them.

    A browser that has cached app.css keeps serving it across a rebuild, so an
    edited rule appears to do nothing at all - and the obvious next move, a
    hard refresh, is exactly what an operator watching a demo will not do.
    Cheap to compute, and stat() is not worth caching against a page that
    already makes several database queries.

    Every fingerprinted file must be listed here. Stat only the stylesheet and
    a console.js change ships invisibly, which is the same bug in a harder
    place to see - the CSS at least looks wrong, whereas stale behaviour just
    looks like the feature was never built.
    """
    newest = 0
    for path in _ASSET_PATHS:
        try:
            newest = max(newest, int(path.stat().st_mtime))
        except OSError:
            continue
    return str(newest)


_STATIC = pathlib.Path(__file__).parent / "static"
_ASSET_PATHS = (_STATIC / "app.css", _STATIC / "console.js")


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


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    """The front door. What the machine is, before anyone feeds it anything.

    Deliberately NOT at "/". An operator who uses this daily should land on the
    Studio, not on a page explaining the Studio to them.

    The formats come from the registry rather than the template, because a
    landing page that lists them by hand is a second place to forget when an
    eighth one is added (Invariant 4).
    """
    return TEMPLATES.TemplateResponse(
        request,
        "welcome.html",
        {"formats": registry.all_formats(), **_shell(request, "welcome")},
    )


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    """A sign-in screen that signs nobody in.

    There is no authentication anywhere in this platform (ARCHITECTURE.md
    sec.13.1) and the Settings page says so in as many words. This page exists
    to show what one would look like, and says on its face that it is a mock -
    the form has no action and posts nowhere. A convincing login that quietly
    did nothing would be the one dishonest screen in the interface.
    """
    return TEMPLATES.TemplateResponse(request, "login.html", {**_shell(request, "login")})


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

    profile_name, style_notes = _profile_summary(request)

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
            "profile_name": profile_name,
            "style_notes": style_notes,
            **_shell(request, "studio"),
        },
    )


def _profile_summary(request: Request) -> tuple[str, list[dict]]:
    """The current operator's name and what has been learned about them."""
    from app.db.models import OperatorProfile

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    if not profile_id:
        return "", []
    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            return "", []
        return profile.name, list(profile.style_notes or [])


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
        jobs_api.JobIn(
            source_ids=[source_id],
            formats=formats,
            parameters=parameters,
            profile_id=_profile_id(request),
        )
    )
    return RedirectResponse(f"/jobs/{result.job_id}/view", status_code=303)


@router.post("/ui/interview", response_class=HTMLResponse)
async def ui_interview(request: Request):
    """One turn of the interview (UC-02, Phase 5).

    Stateless: the transcript comes back with each request rather than living
    in a session. There is no auth and no session store here, and adding one to
    hold a two-message conversation would be the wrong trade.

    The interview is a convenience, never a gate. Any failure returns the panel
    with a message and leaves the lists usable - an operator must always be
    able to start a job.
    """
    from app.agents import interview

    form = await request.form()
    source_id = str(form.get("source_id") or "")
    answer = str(form.get("answer") or "").strip()

    # This is the first place operator free text reaches a prompt. Cap it here
    # as well as in the agent: the dashboard is not the only caller.
    if len(answer) > interview.MAX_ANSWER_CHARS:
        answer = answer[: interview.MAX_ANSWER_CHARS]

    transcript = _parse_transcript(str(form.get("transcript") or ""))
    if answer:
        transcript.append(interview.Turn(role="operator", text=answer))

    with session_scope() as session:
        row = session.get(Source, source_id)
        if row is None:
            raise HTTPException(404, "No such source.")
        content = service.to_content_object(row)

    reply = await interview.next_turn(content, transcript)
    transcript.append(interview.Turn(role="assistant", text=reply.message))

    # The proposal fills the same dropdowns the operator can still edit, so
    # what the model decided is always visible and always overridable.
    values = dict(reply.draft)
    if reply.parameters is not None:
        values = reply.parameters.model_dump()

    return TEMPLATES.TemplateResponse(
        request,
        "partials/interview.html",
        {
            "source_id": source_id,
            "reply": reply,
            "transcript": transcript,
            "transcript_json": json.dumps([t.model_dump() for t in transcript]),
            "vocab": VOCAB,
            "values": values,
        },
    )


def _parse_transcript(raw: str) -> list:
    """Rebuild the conversation from the form field, defensively.

    It round-trips through the browser, so it is untrusted input: a malformed
    value costs the conversation so far, never a 500.
    """
    from app.agents import interview

    try:
        data = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        log.warning("discarding a malformed interview transcript")
        return []
    if not isinstance(data, list):
        return []
    turns = []
    for item in data[: interview.MAX_TURNS * 2]:
        if isinstance(item, dict) and item.get("role") in ("operator", "assistant"):
            turns.append(
                interview.Turn(
                    role=item["role"],
                    text=str(item.get("text") or "")[: interview.MAX_ANSWER_CHARS],
                )
            )
    return turns


@router.get("/jobs/{job_id}/view", response_class=HTMLResponse)
async def job_view(request: Request, job_id: str):
    return TEMPLATES.TemplateResponse(
        request, "job.html", {"job_id": job_id, **_shell(request, "studio")}
    )


@router.post("/ui/jobs/{job_id}/stop", response_class=HTMLResponse)
async def ui_stop_job(request: Request, job_id: str):
    """Ask a running job to stop, and return the live view.

    Cooperative, not a kill: the worker checks between artefacts, so anything
    already finished is kept and exported. An operator who stops at five of
    seven keeps those five - which is why this is a "stop" and not a "cancel",
    and why the status it produces is not a failure.
    """
    from app.graph import cancel

    cancel.request(job_id)
    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


@router.post("/ui/jobs/{job_id}/remember", response_class=HTMLResponse)
async def ui_remember_job(request: Request, job_id: str):
    """Save what worked in this job to the operator's profile.

    The platform already learns from a variant choice - the difference between
    the take an operator picked and the ones they did not. This is the coarser
    signal that had no home: "this whole run came out right, do more of that".

    What is written is the BRIEF, not the artefacts. A note saying "prefers
    detailed, technical writing for security leaders" shapes later drafts;
    pasting an advisory into the profile would just be storage. The evidence
    travels with it so the operator can see in Settings why the note exists and
    delete it if they disagree.
    """
    from app.agents import preferences
    from app.db.models import OperatorProfile

    profile_id = _profile_id(request)
    detail = await jobs_api.get_job(job_id)

    if profile_id:
        params = detail.parameters or {}
        passed = [a.output_type for a in detail.artefacts if str(a.status).startswith("passed")]
        bits = [params.get(k) for k in ("audience", "style", "detail", "tone")]
        described = ", ".join(str(b) for b in bits if b)
        note = (
            f"Approved a run written for {described}."
            if described
            else "Approved a run with these settings."
        )
        with session_scope() as session:
            profile = session.get(OperatorProfile, profile_id)
            if profile is not None:
                profile.style_notes = preferences.merge_note(
                    list(profile.style_notes or []),
                    note,
                    {
                        "job_id": job_id,
                        "parameters": params,
                        "formats_passed": passed,
                    },
                )

    return TEMPLATES.TemplateResponse(
        request,
        "partials/status.html",
        {"job": detail.model_dump(), "remembered": True},
    )


@router.get("/ui/jobs/{job_id}/status", response_class=HTMLResponse)
async def job_status(request: Request, job_id: str):
    """HTMX polling target (TC-0802).

    Blocked and flagged artefacts are rendered, marked - hiding them leaves the
    operator wondering where their deck went (TC-0610).
    """
    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


@router.post("/ui/jobs/{job_id}/artefacts/{output_type}/regenerate", response_class=HTMLResponse)
async def ui_regenerate(
    request: Request, job_id: str, output_type: str, instructions: str = Form(default="")
):
    """UC-09. Returns the status partial so the operator stays on the live view.

    It used to redirect, which reloaded the page and restarted polling from
    scratch - losing scroll position and any open disclosure in the process.
    """
    await jobs_api.regenerate(job_id, output_type, instructions.strip()[:1000])
    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


# --- who is asking ---------------------------------------------------------


def _profile_id(request: Request) -> str:
    """The current operator profile, or "" when nobody has said who they are.

    A cookie, not a session, and explicitly NOT authentication (§13.1). It
    exists so preferences have something to hang off; it grants no access and
    protects nothing. Anyone who edits the cookie becomes that profile, which
    is fine for a demo and must be replaced wholesale if auth is ever built.
    """
    return request.cookies.get(PROFILE_COOKIE, "")


# --- choosing between versions ---------------------------------------------


@router.post("/ui/jobs/{job_id}/artefacts/{output_type}/variants", response_class=HTMLResponse)
async def ui_request_variants(
    request: Request, job_id: str, output_type: str, count: int = Form(default=2)
):
    """Generate several takes for the operator to choose between (UC-08)."""
    from arq import create_pool
    from arq.connections import RedisSettings

    count = max(2, min(int(count or 2), 3))

    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        await pool.enqueue_job("variants_task", job_id, output_type, count, _profile_id(request))
    except Exception as exc:  # noqa: BLE001
        log.error("could not enqueue variants for %s: %s", job_id, exc)
        raise HTTPException(503, "The queue is unavailable; try again shortly.") from exc

    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


@router.post("/ui/jobs/{job_id}/artefacts/{output_type}/choose", response_class=HTMLResponse)
async def ui_choose_variant(
    request: Request, job_id: str, output_type: str, label: str = Form(...)
):
    """Record the operator's choice, and learn from it.

    The chosen variant becomes the artefact - so export, download and the QA
    record all refer to what the operator actually picked, not to whichever
    draft happened to be generated first.
    """
    from app.agents import preferences
    from app.db.models import Artefact as ArtefactRow
    from app.db.models import OperatorProfile, Preference

    with session_scope() as session:
        row = (
            session.query(ArtefactRow)
            .filter_by(job_id=job_id, output_type=output_type)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "No such artefact.")

        chosen = next((v for v in row.variants if v.label == label), None)
        if chosen is None:
            raise HTTPException(404, f"No version {label!r} to choose.")

        rejected = [v.approach for v in row.variants if v.label != label and v.approach]

        for v in row.variants:
            v.chosen = v.label == label

        # The choice IS the artefact from here on.
        row.content = chosen.content
        row.claims = chosen.claims or []
        row.export_paths = chosen.export_paths or []
        row.status = chosen.status

        chosen_approach = chosen.approach
        chosen_content = chosen.content
        profile_id = _profile_id(request)

    # Derived outside the transaction: a model call must not hold a DB
    # connection open, and the choice is already safely recorded.
    note = ""
    if profile_id and rejected:
        note = await preferences.derive_note(
            output_type=output_type,
            chosen_approach=chosen_approach,
            rejected_approaches=rejected,
            chosen_content=chosen_content,
        )

    if profile_id:
        with session_scope() as session:
            profile = session.get(OperatorProfile, profile_id)
            if profile is not None:
                evidence = {
                    "output_type": output_type,
                    "chosen": chosen_approach,
                    "rejected": rejected,
                }
                profile.style_notes = preferences.merge_note(
                    list(profile.style_notes or []), note, evidence
                )
                session.add(
                    Preference(
                        profile_id=profile_id,
                        job_id=job_id,
                        output_type=output_type,
                        chosen_approach=chosen_approach,
                        rejected_approaches=rejected,
                        note=note,
                    )
                )

    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


# --- rating an artefact, and learning from it ------------------------------


@router.post("/ui/jobs/{job_id}/artefacts/{output_type}/feedback", response_class=HTMLResponse)
async def ui_feedback(
    request: Request,
    job_id: str,
    output_type: str,
    liked: str = Form(...),
    reason: str = Form(default=""),
):
    """Thumb up or down, and run an improvement pass over what we now know.

    A dislike is the strongest signal the platform receives: a person
    rejecting work that already cleared every automated check. That is exactly
    the gap QA cannot see, so it is the moment to look for a pattern.
    """
    from app.db.models import Artefact as ArtefactRow
    from app.db.models import Feedback

    is_liked = str(liked).lower() in ("1", "true", "yes", "up", "like")
    reason = " ".join(str(reason or "").split())[:400]
    profile_id = _profile_id(request)

    with session_scope() as session:
        row = (
            session.query(ArtefactRow)
            .filter_by(job_id=job_id, output_type=output_type)
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "No such artefact.")

        existing = (
            session.query(Feedback)
            .filter_by(artefact_id=row.id, profile_id=profile_id)
            .one_or_none()
        )
        if existing is None:
            session.add(
                Feedback(
                    artefact_id=row.id,
                    profile_id=profile_id,
                    output_type=output_type,
                    liked=is_liked,
                    reason=reason,
                )
            )
        else:
            # A second opinion replaces the first rather than double-counting.
            existing.liked = is_liked
            existing.reason = reason or existing.reason

    # The improvement pass runs outside the write, and only for a known
    # operator: rules with nobody to belong to would steer every job on the
    # instance.
    if profile_id:
        await _improve(profile_id)

    detail = await jobs_api.get_job(job_id)
    return TEMPLATES.TemplateResponse(request, "partials/status.html", {"job": detail.model_dump()})


async def _improve(profile_id: str) -> None:
    """One pass of the self-improvement loop for this operator.

    Reads a window of their recent signals, asks what recurs, and folds any
    resulting rules into their style notes. Writes prompt text and nothing
    else - it cannot touch a threshold, a constraint, or code.
    """
    from app.agents import improve
    from app.db.models import Feedback, OperatorProfile, QAResultRow

    with session_scope() as session:
        rows = (
            session.query(Feedback)
            .filter_by(profile_id=profile_id)
            .order_by(Feedback.created_at.desc())
            .limit(improve.WINDOW)
            .all()
        )
        dislikes = [{"output_type": f.output_type, "reason": f.reason} for f in rows if not f.liked]
        liked = [{"output_type": f.output_type} for f in rows if f.liked]

        # Fix notes from the artefacts this operator actually rated: notes
        # from someone else's jobs are not evidence about their taste.
        artefact_ids = [f.artefact_id for f in rows]
        fix_notes: list[str] = []
        if artefact_ids:
            for qa in (
                session.query(QAResultRow)
                .filter(QAResultRow.artefact_id.in_(artefact_ids))
                .order_by(QAResultRow.created_at.desc())
                .limit(200)
                .all()
            ):
                fix_notes.extend(qa.fix_notes or [])

    rules = await improve.derive_rules(dislikes=dislikes, liked=liked, fix_notes=fix_notes)
    if not rules:
        return

    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            return
        profile.style_notes = improve.apply_rules(
            list(profile.style_notes or []),
            rules,
            {
                "source": "improvement loop",
                "chosen": f"{len(dislikes)} dislike(s), {len(liked)} like(s)",
                "rejected": [],
            },
        )
        log.info("improvement loop added %d rule(s) for %s", len(rules), profile_id)


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
