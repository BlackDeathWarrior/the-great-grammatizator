"""Phase 19: format identity - tiles, artefact cards, side-by-side comparison.

Requires the compose stack.

The load-bearing claim here is Invariant 4: a new format is a config entry plus
a template plus a schema, with zero code change (TC-0302). Stage 3 puts seven
formats on screen with their own colour, shape and export type, and every one
of those is read off the registry - so the tests assert that no format id had
to be spelled anywhere to make it happen.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.db.models import Artefact, Job, JobSource, Source, Variant
from app.db.session import session_scope
from app.formats import registry
from app.main import app

pytestmark = pytest.mark.integration

_JOB = "phase19job01"
_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def source():
    with session_scope() as s:
        row = Source(
            source_hash="phase19hash",
            source_type="text",
            title="Phase 19",
            chunks=[{"chunk_id": "c1", "text": "one"}],
        )
        s.add(row)
        s.flush()
        sid = row.id
    yield sid
    with session_scope() as s:
        row = s.get(Source, sid)
        if row:
            s.delete(row)


@pytest.fixture
def job_with_variants(source):
    """One artefact carrying three takes, as the comparison view expects."""
    with session_scope() as s:
        job = Job(id=_JOB, status="done", formats=["exec_summary"])
        s.add(job)
        s.flush()
        s.add(JobSource(job_id=_JOB, source_id=source))
        artefact = Artefact(
            job_id=_JOB,
            output_type="exec_summary",
            status="passed",
            content={
                "title": "T",
                "bottom_line": "B",
                "key_points": ["k"],
                "recommended_action": "R",
                "residual_risk": "X",
            },
        )
        s.add(artefact)
        s.flush()
        for label, approach in (("A", "data-led"), ("B", "consequence-led"), ("C", "narrative")):
            s.add(
                Variant(
                    artefact_id=artefact.id,
                    label=label,
                    approach=approach,
                    status="passed",
                    content={
                        "title": f"T{label}",
                        "bottom_line": "B",
                        "key_points": ["k"],
                        "recommended_action": "R",
                        "residual_risk": "X",
                    },
                )
            )
    yield _JOB
    with session_scope() as s:
        row = s.get(Job, _JOB)
        if row:
            s.delete(row)


# --- shape hints come from config, not from code ----------------------------


def test_every_format_describes_its_own_shape():
    """The tile says what you get, and the registry is where that is said."""
    shapes = {f.id: f.shape for f in registry.all_formats()}

    # Each is derived from that format's OWN constraints, so the values differ.
    assert len(set(shapes.values())) > 1
    assert all(shapes.values()), f"a format described nothing: {shapes}"


def test_shape_is_read_off_constraints_not_hardcoded():
    """Change the number in the registry and the tile changes with it.

    This is the test that would fail if `shape` were a lookup table keyed by
    format id - which is the shape Invariant 4 exists to forbid.
    """
    spec = registry.get("presentation")
    lo = spec.constraints["slides_min"]
    hi = spec.constraints["slides_max"]

    assert str(lo) in spec.shape
    assert str(hi) in spec.shape
    assert "slide" in spec.shape


def test_a_format_with_no_countable_constraint_still_answers():
    """`shape` must never raise or return None; a tile renders regardless."""
    for spec in registry.all_formats():
        assert isinstance(spec.shape, str)
        assert isinstance(spec.runtime_hint, str)


def test_only_the_format_that_declares_a_runtime_shows_one():
    hints = {f.id: f.runtime_hint for f in registry.all_formats()}
    with_runtime = {k for k, v in hints.items() if v}

    declared = {f.id for f in registry.all_formats() if f.constraints.get("runtime_target_sec")}
    assert with_runtime == declared


def test_the_registry_module_names_no_format_id():
    """Invariant 4 has teeth only if nothing enumerates the formats.

    `shape` keys off CONSTRAINT names (slides_min, tweets_min), never off
    format ids, so a format added tomorrow describes itself without this file
    being told it exists.
    """
    code = (_ROOT / "app" / "formats" / "registry.py").read_text(encoding="utf-8")
    for format_id in registry.ids():
        assert format_id not in code, f"registry.py names {format_id}"


# --- the tiles --------------------------------------------------------------


def test_every_registered_format_gets_a_tile(client, source):
    html = client.get(f"/?source_id={source}").text

    for spec in registry.all_formats():
        assert f'data-fmt="{spec.id}"' in html, f"{spec.id} has no tile"
        assert spec.label in html


def test_a_tile_shows_what_the_format_produces(client, source):
    html = client.get(f"/?source_id={source}").text

    deck = registry.get("presentation")
    assert deck.shape in html
    # And the file the operator ends up with.
    assert f">.{deck.renderer}<" in html


def test_tiles_carry_the_colour_the_format_keeps(client, source):
    """The same hue on the picker, the status board and the memory tables.

    Choosing here and recognising it later is the whole reason the colour
    exists, so the identity attribute has to be the same one the stylesheet
    keys off.
    """
    html = client.get(f"/?source_id={source}").text
    css = (_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")

    for spec in registry.all_formats():
        assert f'data-fmt="{spec.id}"' in html
        assert f"--fmt-{spec.id}:" in css, f"{spec.id} has no hue"
        assert f'[data-fmt="{spec.id}"]' in css


def test_every_format_hue_is_distinct():
    """Seven formats, seven colours - a shared hue defeats the point."""
    css = (_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")

    hues = {}
    for spec in registry.all_formats():
        marker = f"--fmt-{spec.id}:"
        value = css.split(marker, 1)[1].split(";", 1)[0].strip()
        hues[spec.id] = value

    assert len(set(hues.values())) == len(hues), f"formats share a colour: {hues}"


def test_adding_a_format_needs_no_template_change(client, source):
    """The picker iterates the registry rather than listing formats.

    A `{% if %}` per format would pass every other test in this file and still
    break the invariant, so this asserts the template contains no format id.
    """
    tpl = (_ROOT / "app" / "web" / "templates" / "index.html").read_text(encoding="utf-8")

    # The two pre-ticked defaults are the one deliberate exception.
    body = tpl.replace("('linkedin_post','exec_summary')", "")

    # Matched as a Jinja comparison rather than as a bare substring: the word
    # "advisory" also appears in a placeholder URL, and failing on that would
    # be the test misreading prose as logic.
    for format_id in registry.ids():
        for usage in (f"'{format_id}'", f'"{format_id}"'):
            assert usage not in body, f"index.html branches on {format_id}"


# --- artefact identity ------------------------------------------------------


def test_an_artefact_card_carries_its_format(client, job_with_variants):
    """A rack of seven, and the operator is looking for one of them."""
    html = client.get(f"/ui/jobs/{job_with_variants}/status").text

    assert 'data-fmt="exec_summary"' in html
    assert "fmt-dot" in html


# --- comparing versions -----------------------------------------------------


def test_variants_render_for_comparison(client, job_with_variants):
    html = client.get(f"/ui/jobs/{job_with_variants}/status").text

    assert "variants-compare" in html
    for approach in ("data-led", "consequence-led", "narrative"):
        assert approach in html


def test_each_variant_offers_its_own_choice(client, job_with_variants):
    """Three takes, three buttons - the choice is per version."""
    html = client.get(f"/ui/jobs/{job_with_variants}/status").text

    for label in ("A", "B", "C"):
        assert f'value="{label}"' in html
    assert html.count("/choose") == 3


def test_a_failed_variant_is_shown_but_not_offered(client, source):
    """Hiding it leaves the operator wondering where the third version went.

    Marked, counted and unchoosable - a failed take is information about the
    run, not something to be quietly dropped (the same reasoning as TC-0610).
    """
    with session_scope() as s:
        job = Job(id="phase19job02", status="done", formats=["exec_summary"])
        s.add(job)
        s.flush()
        s.add(JobSource(job_id="phase19job02", source_id=source))
        artefact = Artefact(
            job_id="phase19job02",
            output_type="exec_summary",
            status="passed",
            content={"title": "T"},
        )
        s.add(artefact)
        s.flush()
        s.add(Variant(artefact_id=artefact.id, label="A", approach="data-led", status="passed"))
        s.add(Variant(artefact_id=artefact.id, label="B", approach="narrative", status="blocked"))

    try:
        html = client.get("/ui/jobs/phase19job02/status").text
        assert "did not pass the checks" in html
        # The passing one can be chosen; the blocked one cannot.
        assert html.count("/choose") == 1
        assert "is-out" in html
    finally:
        with session_scope() as s:
            row = s.get(Job, "phase19job02")
            if row:
                s.delete(row)


def test_the_comparison_view_does_not_hardcode_a_format(client, job_with_variants):
    """A variant body reuses the format's own partial.

    Comparing decks and comparing tweets are the same operation on different
    content, and a second rendering path per format is how the two drift.
    """
    tpl = (_ROOT / "app" / "web" / "templates" / "partials" / "status.html").read_text(
        encoding="utf-8"
    )

    for format_id in registry.ids():
        for usage in (f"'{format_id}'", f'"{format_id}"'):
            assert usage not in tpl, f"status.html branches on {format_id}"

    # Rendered by interpolating the output type, not by a branch per format.
    assert "artefact_' ~ a_saved.output_type" in tpl


def test_every_format_has_the_partial_the_view_interpolates():
    """The dynamic include only works if the file is actually there."""
    partials = _ROOT / "app" / "web" / "templates" / "partials"

    for format_id in registry.ids():
        assert (partials / f"artefact_{format_id}.html").exists(), format_id


# --- the registry file itself ----------------------------------------------


def test_the_registry_json_is_the_only_place_formats_are_listed():
    data = json.loads((_ROOT / "app" / "formats" / "registry.json").read_text(encoding="utf-8"))
    declared = {f["id"] for f in data["formats"]}

    assert declared == set(registry.ids())
    assert len(declared) == 7
