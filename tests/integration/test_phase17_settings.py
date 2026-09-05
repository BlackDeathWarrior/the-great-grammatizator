"""Phase 17: the settings tab - profiles, provider keys, memory, appearance.

Requires the compose stack.

The through-line of these tests is that a setting must actually take effect.
A settings page that stores a value and changes nothing is worse than no
settings page, because it looks like it worked - so each section is asserted at
the layer that consumes it (the router for keys, the rendered html for themes,
the profile row for notes), not merely at the endpoint that accepted it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db.models import OperatorProfile, ProviderKey
from app.db.session import session_scope
from app.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def profile():
    """A throwaway profile, removed afterwards along with anything it learned."""
    with session_scope() as s:
        row = OperatorProfile(name="Test Operator 17")
        s.add(row)
        s.flush()
        pid = row.id
    yield pid
    with session_scope() as s:
        row = s.get(OperatorProfile, pid)
        if row:
            s.delete(row)


@pytest.fixture
def clean_keys():
    """Leave the key store exactly as it was found.

    Every test here writes a fake credential, and one left behind would
    override the operator's real .env key for every later run - the failure
    would land in an unrelated test, hours away from its cause.
    """
    with session_scope() as s:
        before = {r.provider: (r.ciphertext, r.hint) for r in s.query(ProviderKey).all()}
    yield
    with session_scope() as s:
        for row in s.query(ProviderKey).all():
            s.delete(row)
        s.flush()
        for provider, (ciphertext, hint) in before.items():
            s.add(ProviderKey(provider=provider, ciphertext=ciphertext, hint=hint))
    from app.gateway import router as gateway

    gateway.reset()


# --- the page renders -------------------------------------------------------


def test_settings_page_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    for heading in ("Profiles", "API keys", "Memory", "Appearance"):
        assert heading in r.text


def test_nav_marks_the_current_tab(client):
    """The shell has to say where you are, on both pages."""
    assert 'href="/settings" aria-current="page"' in client.get("/settings").text
    assert 'href="/" aria-current="page"' in client.get("/").text


# --- profiles ---------------------------------------------------------------


def test_create_profile_sets_the_cookie(client):
    r = client.post("/ui/profiles/create", data={"name": "Phase17 Created"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.cookies.get("operator")

    with session_scope() as s:
        row = s.query(OperatorProfile).filter_by(name="Phase17 Created").one_or_none()
        assert row is not None
        s.delete(row)


def test_create_profile_rejects_an_empty_name(client):
    assert client.post("/ui/profiles/create", data={"name": "   "}).status_code == 400


def test_switching_profile_changes_who_is_asking(client, profile):
    r = client.post(f"/ui/profiles/{profile}/switch", follow_redirects=False)
    assert r.status_code == 303
    assert client.cookies.get("operator") == profile
    # The header must show it, or the switch is invisible.
    assert "Test Operator 17" in client.get("/settings").text


def test_switching_to_a_profile_that_is_gone_is_a_404(client):
    assert client.post("/ui/profiles/deadbeefdead/switch").status_code == 404


def test_rename_profile(client, profile):
    r = client.post(f"/ui/profiles/{profile}/rename", data={"name": "Renamed 17"})
    assert r.status_code == 200  # followed the redirect
    with session_scope() as s:
        assert s.get(OperatorProfile, profile).name == "Renamed 17"


def test_rename_refuses_a_name_already_taken(client, profile):
    with session_scope() as s:
        other = OperatorProfile(name="Occupied 17")
        s.add(other)
        s.flush()
        other_id = other.id
    try:
        r = client.post(f"/ui/profiles/{profile}/rename", data={"name": "Occupied 17"})
        assert r.status_code == 400
    finally:
        with session_scope() as s:
            row = s.get(OperatorProfile, other_id)
            if row:
                s.delete(row)


def test_export_then_import_round_trips_the_notes(client, profile):
    """An export must carry enough to reconstitute the profile elsewhere."""
    with session_scope() as s:
        s.get(OperatorProfile, profile).style_notes = [
            {"note": "Open with a number.", "evidence": {"chosen": "A", "rejected": ["B"]}}
        ]

    exported = client.get(f"/ui/profiles/{profile}/export")
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["name"] == "Test Operator 17"
    assert payload["style_notes"][0]["note"] == "Open with a number."

    r = client.post("/ui/profiles/import", data={"payload": exported.text}, follow_redirects=False)
    assert r.status_code == 303
    new_id = r.cookies.get("operator")
    assert new_id and new_id != profile

    with session_scope() as s:
        loaded = s.get(OperatorProfile, new_id)
        try:
            # Renamed rather than overwritten: importing must never clobber the
            # profile whose name it happens to share.
            assert loaded.name == "Test Operator 17 (2)"
            assert loaded.style_notes[0]["note"] == "Open with a number."
        finally:
            s.delete(loaded)


def test_import_rejects_junk(client):
    assert client.post("/ui/profiles/import", data={"payload": "not json"}).status_code == 400
    assert client.post("/ui/profiles/import", data={"payload": '{"a": 1}'}).status_code == 400


def test_deleting_a_profile_signs_you_out_of_it(client):
    r = client.post("/ui/profiles/create", data={"name": "Phase17 Doomed"}, follow_redirects=False)
    pid = r.cookies.get("operator")
    client.cookies.set("operator", pid)

    deleted = client.post(f"/ui/profiles/{pid}/delete", follow_redirects=False)

    # Asserted on the response header rather than the client jar: httpx keeps a
    # cookie that was set on the client by hand, so the jar would still show it
    # even though the server correctly told the browser to drop it.
    assert "Max-Age=0" in deleted.headers.get("set-cookie", "")
    assert 'operator=""' in deleted.headers.get("set-cookie", "")

    with session_scope() as s:
        assert s.get(OperatorProfile, pid) is None


# --- appearance -------------------------------------------------------------


def test_theme_choice_is_stored_on_the_profile(client, profile):
    client.cookies.set("operator", profile)
    client.post("/ui/theme", data={"theme": "dark", "next": "/settings"})

    with session_scope() as s:
        assert s.get(OperatorProfile, profile).theme == "dark"


def test_theme_reaches_the_rendered_page(client, profile):
    """Stamped server-side, or the page renders light and then corrects itself."""
    client.cookies.set("operator", profile)

    client.post("/ui/theme", data={"theme": "dark", "next": "/settings"})
    assert 'data-theme="dark"' in client.get("/settings").text

    client.post("/ui/theme", data={"theme": "light", "next": "/settings"})
    assert 'data-theme="light"' in client.get("/settings").text

    # "system" must stamp NOTHING, or prefers-color-scheme can never apply.
    client.post("/ui/theme", data={"theme": "system", "next": "/settings"})
    assert "data-theme" not in client.get("/settings").text


def test_anonymous_theme_falls_back_to_a_cookie(client):
    client.cookies.clear()
    r = client.post("/ui/theme", data={"theme": "dark", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.cookies.get("theme") == "dark"
    assert 'data-theme="dark"' in client.get("/").text


def test_unknown_theme_is_refused(client):
    assert client.post("/ui/theme", data={"theme": "neon", "next": "/"}).status_code == 400


def test_theme_next_cannot_leave_the_app(client):
    """`next` is a form field, so it is an open redirect if left unchecked."""
    r = client.post(
        "/ui/theme",
        data={"theme": "dark", "next": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"

    r = client.post(
        "/ui/theme", data={"theme": "dark", "next": "//evil.example/x"}, follow_redirects=False
    )
    assert r.headers["location"] == "/"


# --- provider keys ----------------------------------------------------------


def test_saved_key_is_encrypted_and_never_returned(client, clean_keys):
    secret = "gsk_phase17_secret_value_abcd"
    client.post("/ui/keys/groq", data={"key": secret})

    with session_scope() as s:
        row = s.query(ProviderKey).filter_by(provider="groq").one()
        assert secret not in row.ciphertext
        assert row.hint == "abcd"

    # The page shows the hint and nothing more.
    page = client.get("/settings").text
    assert secret not in page
    assert "abcd" in page


def test_saved_key_overrides_env_for_the_gateway(client, clean_keys):
    """The point of the whole feature: a saved key must reach a model call."""
    from app.gateway import router as gateway
    from app.secrets_store import resolve_key

    client.post("/ui/keys/groq", data={"key": "gsk_phase17_override_0001"})
    assert resolve_key("groq") == "gsk_phase17_override_0001"

    built = gateway.build_router()
    fast_keys = [
        m["litellm_params"]["api_key"] for m in built.model_list if m["model_name"] == "fast"
    ]
    assert "gsk_phase17_override_0001" in fast_keys


def test_saving_a_key_bumps_the_version_so_the_worker_rebuilds(client, clean_keys):
    """Without this a key saved here never reaches the container running jobs."""
    from app.gateway import router as gateway
    from app.secrets_store import current_version

    gateway.reset()
    gateway.get_router()
    before_version = current_version()
    cached = gateway._router

    client.post("/ui/keys/groq", data={"key": "gsk_phase17_bump_0002"})
    assert current_version() > before_version
    assert gateway.get_router() is not cached


def test_deleting_a_saved_key_falls_back_to_env_not_to_nothing(client, clean_keys):
    from app.config import get_settings
    from app.secrets_store import resolve_key

    client.post("/ui/keys/groq", data={"key": "gsk_phase17_temp_0003"})
    client.post("/ui/keys/groq/delete")

    assert resolve_key("groq") == (get_settings().groq_api_key or "")
    with session_scope() as s:
        assert s.query(ProviderKey).filter_by(provider="groq").one_or_none() is None


def test_empty_key_is_refused(client, clean_keys):
    assert client.post("/ui/keys/groq", data={"key": "   "}).status_code == 400


def test_unknown_provider_is_refused(client, clean_keys):
    assert client.post("/ui/keys/notaprovider", data={"key": "x"}).status_code == 400


def test_probe_reports_a_missing_key_rather_than_raising(client, clean_keys):
    """A probe reports a failure; it must never become one."""
    import app.secrets_store.store as store

    original = store.resolve_key
    store.resolve_key = lambda provider: ""
    try:
        r = client.post("/ui/keys/groq/test")
        assert r.status_code == 200
        assert "No key configured" in r.text
        assert "probe bad" in r.text
    finally:
        store.resolve_key = original


def test_probe_uses_the_model_the_gateway_actually_routes_to(client):
    """A probe against a hardcoded id would drift from what jobs really call."""
    from app.gateway.router import probe_model_for

    assert probe_model_for("groq") == "groq/openai/gpt-oss-20b"
    assert probe_model_for("embedding") is None


# --- memory -----------------------------------------------------------------


def test_add_edit_and_delete_a_style_note(client, profile):
    client.cookies.set("operator", profile)

    client.post("/ui/memory/notes/add", data={"note": "Open with a number."})
    with session_scope() as s:
        notes = s.get(OperatorProfile, profile).style_notes
        assert len(notes) == 1
        assert notes[0]["note"] == "Open with a number."
        # Honest about its origin rather than posing as something learned.
        assert notes[0]["evidence"]["chosen"] == "written by hand"

    client.post("/ui/memory/notes/0/edit", data={"note": "Open with a statistic."})
    with session_scope() as s:
        notes = s.get(OperatorProfile, profile).style_notes
        assert notes[0]["note"] == "Open with a statistic."
        assert notes[0]["edited"] is True

    client.post("/ui/memory/notes/0/delete")
    with session_scope() as s:
        assert s.get(OperatorProfile, profile).style_notes == []


def test_an_empty_note_is_refused(client, profile):
    client.cookies.set("operator", profile)
    assert client.post("/ui/memory/notes/add", data={"note": "  "}).status_code == 400


def test_a_note_needs_somebody_to_belong_to(client):
    client.cookies.clear()
    r = client.post("/ui/memory/notes/add", data={"note": "Anything."})
    assert r.status_code == 400


def test_editing_a_note_that_does_not_exist_is_a_404(client, profile):
    client.cookies.set("operator", profile)
    assert client.post("/ui/memory/notes/9/edit", data={"note": "x"}).status_code == 404


def test_learned_notes_are_shown_with_their_evidence(client, profile):
    """A preference the operator cannot trace is one they cannot disagree with."""
    client.cookies.set("operator", profile)
    with session_scope() as s:
        s.get(OperatorProfile, profile).style_notes = [
            {"note": "Prefers a data-led opener.", "evidence": {"chosen": "A", "rejected": ["B"]}}
        ]

    page = client.get("/settings").text
    assert "Prefers a data-led opener." in page
    assert "Learned from:" in page


def test_deleting_a_source_in_use_is_refused(client):
    """Its artefacts cite chunk ids that would stop resolving."""
    from app.db.models import Job, JobSource, Source

    with session_scope() as s:
        source = Source(source_hash="phase17hash", source_type="text", title="In use", chunks=[])
        job = Job(formats=["linkedin_post"])
        s.add_all([source, job])
        s.flush()
        s.add(JobSource(job_id=job.id, source_id=source.id))
        sid, jid = source.id, job.id

    try:
        r = client.post(f"/ui/memory/sources/{sid}/delete")
        assert r.status_code == 400
        assert "still reference" in r.text
        with session_scope() as s:
            assert s.get(Source, sid) is not None
    finally:
        with session_scope() as s:
            job = s.get(Job, jid)
            if job:
                s.delete(job)
            s.flush()
            row = s.get(Source, sid)
            if row:
                s.delete(row)


def test_deleting_an_unused_source_succeeds(client):
    from app.db.models import Source

    with session_scope() as s:
        source = Source(source_hash="phase17unused", source_type="text", title="Free", chunks=[])
        s.add(source)
        s.flush()
        sid = source.id

    r = client.post(f"/ui/memory/sources/{sid}/delete")
    assert r.status_code == 200
    with session_scope() as s:
        assert s.get(Source, sid) is None


def test_memory_survives_having_no_profile(client):
    """Anonymous is a supported state, not an error."""
    client.cookies.clear()
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Style notes and ratings belong to a profile" in r.text
