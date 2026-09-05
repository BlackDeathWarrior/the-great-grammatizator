"""Settings: profiles, provider keys, memory, appearance.

Everything here is about the operator rather than about a job. It is a separate
module from routes.py because the studio and its settings change for different
reasons - and routes.py was already long enough that adding four more sections
to it would have made both harder to read.

Nothing in this file is a security boundary. Profiles are names in a cookie
(§13.1) and the key store defends a dumped database, not the dashboard. Both
say so in the UI rather than implying otherwise.
"""

from __future__ import annotations

import json
import logging
import pathlib

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db.session import session_scope
from app.web.routes import PROFILE_COOKIE, THEME_COOKIE, _shell

log = logging.getLogger(__name__)
router = APIRouter(tags=["settings"])

TEMPLATES = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))

THEMES = ("system", "light", "dark")
_YEAR = 31_536_000


# --- the page ---------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.secrets_store import key_status

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "profiles": _all_profiles(profile_id),
            "current_profile": _profile_detail(profile_id),
            "keys": key_status(),
            "memory": _memory(profile_id),
            **_shell(request, "settings"),
        },
    )


def _all_profiles(current_id: str) -> list[dict]:
    """Every profile, so one can be switched to. Oldest first, as created."""
    from app.db.models import OperatorProfile, Preference

    with session_scope() as session:
        rows = session.query(OperatorProfile).order_by(OperatorProfile.created_at).all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "note_count": len(r.style_notes or []),
                "choice_count": session.query(Preference).filter_by(profile_id=r.id).count(),
                "is_current": r.id == current_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def _profile_detail(profile_id: str) -> dict | None:
    from app.db.models import OperatorProfile

    if not profile_id:
        return None
    with session_scope() as session:
        p = session.get(OperatorProfile, profile_id)
        if p is None:
            return None
        return {
            "id": p.id,
            "name": p.name,
            "theme": p.theme or "system",
            "style_notes": list(p.style_notes or []),
        }


def _memory(profile_id: str) -> dict:
    """Everything the platform remembers, in the four shapes it remembers it.

    Sources and jobs are global rather than per-profile: they are not learned
    preferences, and hiding another operator's ingested source would only make
    the same file get uploaded twice.
    """
    from app.db.models import Artefact, Feedback, Job, OperatorProfile, Preference, Source

    with session_scope() as session:
        notes = []
        if profile_id:
            p = session.get(OperatorProfile, profile_id)
            if p is not None:
                notes = list(p.style_notes or [])

        feedback = []
        if profile_id:
            rows = (
                session.query(Feedback)
                .filter_by(profile_id=profile_id)
                .order_by(Feedback.created_at.desc())
                .limit(40)
                .all()
            )
            for f in rows:
                artefact = session.get(Artefact, f.artefact_id)
                feedback.append(
                    {
                        "id": f.id,
                        "output_type": f.output_type,
                        "liked": f.liked,
                        "reason": f.reason,
                        "job_id": artefact.job_id if artefact else "",
                        "created_at": f.created_at,
                    }
                )

        choices = []
        if profile_id:
            for c in (
                session.query(Preference)
                .filter_by(profile_id=profile_id)
                .order_by(Preference.created_at.desc())
                .limit(25)
                .all()
            ):
                choices.append(
                    {
                        "id": c.id,
                        "output_type": c.output_type,
                        "chosen": c.chosen_approach,
                        "rejected": list(c.rejected_approaches or []),
                        "note": c.note,
                        "job_id": c.job_id,
                        "created_at": c.created_at,
                    }
                )

        sources = [
            {
                "id": s.id,
                "title": s.title or "Untitled",
                "source_type": str(s.source_type),
                "chunk_count": len(s.chunks or []),
                "created_at": s.created_at,
            }
            for s in session.query(Source).order_by(Source.created_at.desc()).limit(40).all()
        ]

        jobs = []
        for j in session.query(Job).order_by(Job.created_at.desc()).limit(30).all():
            arts = session.query(Artefact).filter_by(job_id=j.id).all()
            jobs.append(
                {
                    "id": j.id,
                    "status": str(j.status),
                    "formats": list(j.formats or []),
                    "artefact_count": len(arts),
                    # Anything not outright failed counts as delivered: a
                    # flagged artefact still reached the operator.
                    "passed": sum(1 for a in arts if "passed" in str(a.status)),
                    "created_at": j.created_at,
                }
            )

    return {
        "style_notes": notes,
        "feedback": feedback,
        "choices": choices,
        "sources": sources,
        "jobs": jobs,
    }


# --- appearance -------------------------------------------------------------


@router.post("/ui/theme")
async def set_theme(request: Request, theme: str = Form(...), next: str = Form(default="/")):
    """Store the palette choice against the profile, or the cookie if anonymous."""
    from app.db.models import OperatorProfile

    if theme not in THEMES:
        raise HTTPException(400, f"Unknown theme {theme!r}.")

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    # Only ever redirect within this app: `next` arrives from a form field and
    # an absolute URL there would turn the theme switch into an open redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(target, status_code=303)

    stored = False
    if profile_id:
        with session_scope() as session:
            profile = session.get(OperatorProfile, profile_id)
            if profile is not None:
                profile.theme = theme
                stored = True

    if not stored:
        response.set_cookie(THEME_COOKIE, theme, max_age=_YEAR, httponly=True, samesite="lax")
    return response


# --- profiles ---------------------------------------------------------------


@router.post("/ui/profiles/create")
async def create_profile(name: str = Form(default="")):
    from app.db.models import OperatorProfile

    name = " ".join(name.split())[:80]
    if not name:
        raise HTTPException(400, "A profile needs a name.")

    with session_scope() as session:
        profile = session.query(OperatorProfile).filter_by(name=name).one_or_none()
        if profile is None:
            profile = OperatorProfile(name=name)
            session.add(profile)
            session.flush()
        profile_id = profile.id

    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(PROFILE_COOKIE, profile_id, max_age=_YEAR, httponly=True, samesite="lax")
    return response


@router.post("/ui/profiles/{profile_id}/switch")
async def switch_profile(profile_id: str):
    from app.db.models import OperatorProfile

    with session_scope() as session:
        if session.get(OperatorProfile, profile_id) is None:
            raise HTTPException(404, "No such profile.")

    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(PROFILE_COOKIE, profile_id, max_age=_YEAR, httponly=True, samesite="lax")
    return response


@router.post("/ui/profiles/{profile_id}/rename")
async def rename_profile(profile_id: str, name: str = Form(default="")):
    from app.db.models import OperatorProfile

    name = " ".join(name.split())[:80]
    if not name:
        raise HTTPException(400, "A profile needs a name.")

    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "No such profile.")
        clash = session.query(OperatorProfile).filter_by(name=name).one_or_none()
        if clash is not None and clash.id != profile_id:
            raise HTTPException(400, f"There is already a profile called {name!r}.")
        profile.name = name

    return RedirectResponse("/settings", status_code=303)


@router.post("/ui/profiles/{profile_id}/delete")
async def delete_profile(request: Request, profile_id: str):
    """Delete a profile and everything learned about it.

    Preferences cascade with the profile by design: a note whose evidence is
    gone cannot be judged, and leaving orphaned notes would mean the operator
    could not tell why the platform writes the way it does.
    """
    from app.db.models import OperatorProfile

    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "No such profile.")
        session.delete(profile)

    response = RedirectResponse("/settings", status_code=303)
    if request.cookies.get(PROFILE_COOKIE, "") == profile_id:
        response.delete_cookie(PROFILE_COOKIE)
    return response


@router.post("/ui/profiles/signout")
async def sign_out():
    """Stop being anybody. The profile and its notes survive."""
    response = RedirectResponse("/settings", status_code=303)
    response.delete_cookie(PROFILE_COOKIE)
    return response


@router.get("/ui/profiles/{profile_id}/export")
async def export_profile(profile_id: str):
    """Download a profile as JSON: its notes and the choices behind them."""
    from app.db.models import OperatorProfile, Preference

    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "No such profile.")
        payload = {
            "version": 1,
            "name": profile.name,
            "theme": profile.theme or "system",
            "style_notes": list(profile.style_notes or []),
            "preferences": [
                {
                    "output_type": p.output_type,
                    "chosen_approach": p.chosen_approach,
                    "rejected_approaches": list(p.rejected_approaches or []),
                    "note": p.note,
                }
                for p in session.query(Preference).filter_by(profile_id=profile_id).all()
            ],
        }

    safe = "".join(c for c in payload["name"] if c.isalnum() or c in "-_") or "profile"
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="{safe}-profile.json"'},
    )


@router.post("/ui/profiles/import")
async def import_profile(payload: str = Form(default="")):
    """Load an exported profile back in, under its own name.

    Imports as a NEW profile rather than merging into an existing one. Merging
    two sets of learned notes produces a profile that matches neither operator,
    and there is no way to tell afterwards which note came from where.
    """
    from app.db.models import OperatorProfile, Preference

    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        raise HTTPException(400, "That is not valid JSON.") from None

    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise HTTPException(400, "That JSON is not a profile export.")

    name = " ".join(data["name"].split())[:80] or "Imported"
    notes = data.get("style_notes")
    notes = [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []
    theme = data.get("theme") if data.get("theme") in THEMES else "system"

    with session_scope() as session:
        # A clash renames rather than overwrites: an import must never silently
        # replace the notes an operator has been building up.
        candidate, n = name, 2
        while session.query(OperatorProfile).filter_by(name=candidate).one_or_none() is not None:
            candidate, n = f"{name} ({n})", n + 1

        profile = OperatorProfile(name=candidate, theme=theme, style_notes=notes)
        session.add(profile)
        session.flush()
        profile_id = profile.id

        raw_prefs = data.get("preferences")
        for pref in raw_prefs if isinstance(raw_prefs, list) else []:
            if not isinstance(pref, dict):
                continue
            session.add(
                Preference(
                    profile_id=profile_id,
                    job_id="",  # the originating job does not exist here
                    output_type=str(pref.get("output_type", ""))[:64],
                    chosen_approach=str(pref.get("chosen_approach", ""))[:120],
                    rejected_approaches=[
                        str(r) for r in pref.get("rejected_approaches", []) if isinstance(r, str)
                    ],
                    note=str(pref.get("note", "")),
                )
            )

    response = RedirectResponse("/settings", status_code=303)
    response.set_cookie(PROFILE_COOKIE, profile_id, max_age=_YEAR, httponly=True, samesite="lax")
    return response


# --- provider keys ----------------------------------------------------------


@router.post("/ui/keys/{provider}")
async def save_provider_key(provider: str, key: str = Form(default="")):
    from app.secrets_store import save_key

    try:
        save_key(provider, key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    return RedirectResponse("/settings#keys", status_code=303)


@router.post("/ui/keys/{provider}/delete")
async def delete_provider_key(provider: str):
    from app.secrets_store import delete_key

    try:
        delete_key(provider)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None

    return RedirectResponse("/settings#keys", status_code=303)


@router.post("/ui/keys/{provider}/test", response_class=HTMLResponse)
async def test_provider_key(request: Request, provider: str):
    """Spend one trivial call proving the key works.

    A configured key and a working key are different things - a revoked or
    mistyped credential looks identical in the settings table - and the
    difference otherwise surfaces as a job dying mid-demo (the same reasoning
    as the startup preflight, TC-0908).
    """
    from app.secrets_store import probe

    result = await probe(provider)
    return TEMPLATES.TemplateResponse(
        request, "partials/probe.html", {"provider": provider, "result": result}
    )


# --- memory -----------------------------------------------------------------


@router.post("/ui/memory/notes/add")
async def add_note(request: Request, note: str = Form(default="")):
    """Write a style note by hand, rather than waiting to be inferred one."""
    from app.db.models import OperatorProfile

    note = " ".join(note.split())[:400]
    if not note:
        raise HTTPException(400, "An empty note teaches nothing.")

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    if not profile_id:
        raise HTTPException(400, "Create a profile first - notes have to belong to somebody.")

    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "No such profile.")
        notes = list(profile.style_notes or [])
        notes.append(
            {
                "note": note,
                # Same shape as a learned note so the template needs no special
                # case, and honest about where it came from.
                "evidence": {"chosen": "written by hand", "rejected": []},
            }
        )
        profile.style_notes = notes

    return RedirectResponse("/settings#memory", status_code=303)


@router.post("/ui/memory/notes/{index}/edit")
async def edit_note(request: Request, index: int, note: str = Form(default="")):
    from app.db.models import OperatorProfile

    note = " ".join(note.split())[:400]
    if not note:
        raise HTTPException(400, "An empty note teaches nothing.")

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id) if profile_id else None
        if profile is None:
            raise HTTPException(404, "No profile.")
        notes = list(profile.style_notes or [])
        if not 0 <= index < len(notes):
            raise HTTPException(404, "No such note.")
        entry = dict(notes[index])
        entry["note"] = note
        # An edited note is no longer what the evidence showed, and saying so
        # keeps the audit trail honest.
        entry["edited"] = True
        notes[index] = entry
        profile.style_notes = notes

    return RedirectResponse("/settings#memory", status_code=303)


@router.post("/ui/memory/notes/{index}/delete")
async def delete_note(request: Request, index: int):
    from app.db.models import OperatorProfile

    profile_id = request.cookies.get(PROFILE_COOKIE, "")
    with session_scope() as session:
        profile = session.get(OperatorProfile, profile_id) if profile_id else None
        if profile is None:
            raise HTTPException(404, "No profile.")
        notes = list(profile.style_notes or [])
        if 0 <= index < len(notes):
            notes.pop(index)
            profile.style_notes = notes

    return RedirectResponse("/settings#memory", status_code=303)


@router.post("/ui/memory/feedback/{feedback_id}/delete")
async def delete_feedback(feedback_id: str):
    """Undo a thumb. The rating is withdrawn, the artefact is untouched."""
    from app.db.models import Feedback

    with session_scope() as session:
        row = session.get(Feedback, feedback_id)
        if row is None:
            raise HTTPException(404, "No such feedback.")
        session.delete(row)

    return RedirectResponse("/settings#memory", status_code=303)


@router.post("/ui/memory/sources/{source_id}/delete")
async def delete_source(source_id: str):
    """Forget a source: its row, and its vectors.

    Refuses while a job still references it. Deleting would cascade that job's
    association row away and leave artefacts citing chunk ids nothing can
    resolve, which is worse than making the operator delete the job first.
    """
    from app.db.models import JobSource, Source

    with session_scope() as session:
        source = session.get(Source, source_id)
        if source is None:
            raise HTTPException(404, "No such source.")
        in_use = session.query(JobSource).filter_by(source_id=source_id).count()
        if in_use:
            raise HTTPException(
                400,
                f"{in_use} job(s) still reference this source. Delete those first.",
            )
        session.delete(source)

    # Best-effort: an orphaned vector is wasteful, not incorrect, and a cold
    # qdrant must not make the delete look like it failed.
    try:
        from app.ingest.embed import delete_source as drop_vectors

        drop_vectors(source_id)
    except Exception:  # noqa: BLE001
        log.warning("could not drop vectors for source %s", source_id, exc_info=True)

    return RedirectResponse("/settings#memory", status_code=303)
