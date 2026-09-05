"""Phase 20: the Grammatizator theme - naming, icons, artefact form, page order.

Requires the compose stack.

Dahl's Great Automatic Grammatizator is a machine that writes to order, and the
theme earns its keep only where it clarifies. So these tests are mostly about
restraint: the character stays in the places the interface was otherwise
silent, the icons sit beside words rather than replacing them, and each format
renders in the geometry of the thing it actually is - which is the check an
operator makes at a glance ("is this the shape I asked for?").
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.db.models import Artefact, Job, JobSource, Source
from app.db.session import session_scope
from app.formats import registry
from app.main import app

pytestmark = pytest.mark.integration

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "app" / "web" / "templates"
_CSS = (_ROOT / "app" / "web" / "static" / "app.css").read_text(encoding="utf-8")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def source():
    with session_scope() as s:
        row = Source(
            source_hash="phase20hash",
            source_type="text",
            title="Phase 20",
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


def _job_with(source_id: str, output_type: str, content: dict) -> str:
    job_id = f"p20{output_type[:8]}"
    with session_scope() as s:
        job = Job(id=job_id, status="done", formats=[output_type])
        s.add(job)
        s.flush()
        s.add(JobSource(job_id=job_id, source_id=source_id))
        s.add(Artefact(job_id=job_id, output_type=output_type, status="passed", content=content))
    return job_id


def _drop(job_id: str) -> None:
    with session_scope() as s:
        row = s.get(Job, job_id)
        if row:
            s.delete(row)


# --- naming -----------------------------------------------------------------


def test_the_machine_has_its_name(client):
    html = client.get("/").text
    assert "The Great Grammatizator" in html


def test_the_old_name_is_gone_everywhere(client):
    """A half-rename is worse than none - it reads as two products."""
    for path in ("/", "/settings"):
        assert "Content Transformation" not in client.get(path).text

    for tpl in _TEMPLATES.rglob("*.html"):
        assert "Content Transformation" not in tpl.read_text(encoding="utf-8"), tpl.name


def test_the_name_is_stamped_once_not_repeated(client):
    """A nameplate, the way a machine carries one.

    Repeating the product name down the page is marketing furniture on a tool
    somebody uses every day, so it appears in the title and the header and
    nowhere else in the body.
    """
    html = client.get("/").text
    body = html.split("</header>", 1)[1]
    assert "The Great Grammatizator" not in body


# --- the machine-room palette ----------------------------------------------


def test_the_palette_is_warm_not_the_old_blue():
    """Brass, not spreadsheet. The neutrals carry the theme, not a wallpaper."""
    accent = _CSS.split("--accent:", 1)[1].split(";", 1)[0]
    # Hue 65 is brass; the old accent sat at 258 (blue).
    assert "258" not in accent
    assert "65" in accent


def test_verdict_colours_did_not_move():
    """The theme may not borrow the colours that carry meaning.

    Green here always means a check passed. A decorative palette shift that
    dragged --pass with it would make the whole QA display ambiguous.
    """
    for token, hue in (("--pass:", "152"), ("--flag:", "75"), ("--fail:", "25")):
        value = _CSS.split(token, 1)[1].split(";", 1)[0]
        assert hue in value, f"{token} moved off its hue"


def test_the_plate_tokens_exist_in_both_themes():
    """A token defined only inside a media query vanishes when it is false."""
    assert _CSS.count("--plate:") >= 2
    assert _CSS.count("--engrave:") >= 2


# --- icons ------------------------------------------------------------------


def test_icons_sit_beside_words_never_instead_of_them(client):
    """An icon-only button is a guessing game.

    Every icon in the interface accompanies a visible label, so the control
    still reads correctly to someone who does not recognise the glyph.
    """
    html = client.get("/settings").text

    for label in ("Save", "Test", "Delete", "Export", "Import"):
        assert label in html


def test_every_icon_is_hidden_from_screen_readers():
    """The label beside it is already announced; the icon would say it twice."""
    icons = (_TEMPLATES / "partials" / "icons.html").read_text(encoding="utf-8")

    assert icons.count("<svg") == icons.count('aria-hidden="true"')


def test_icons_are_defined_in_exactly_one_place():
    """Two copies of an icon drift. The macros are the single source."""
    offenders = []
    for tpl in _TEMPLATES.rglob("*.html"):
        if tpl.name in ("icons.html", "base.html"):
            continue  # the set itself, and the nameplate mark
        if "<svg" in tpl.read_text(encoding="utf-8"):
            offenders.append(tpl.name)

    assert not offenders, f"inline svg outside the icon set: {offenders}"


def test_no_dependency_was_added_for_the_icons():
    """Eight paths do not justify a package."""
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for library in ("feather", "lucide", "heroicons", "fontawesome", "bootstrap-icons"):
        assert library not in pyproject


# --- the first viewport -----------------------------------------------------


def test_the_page_opens_on_the_work_not_on_the_profile(client, source):
    """The operator came to feed the machine a source.

    The learned-preference panel used to fill the first screen before the
    upload box appeared - context ahead of the task. It is now one line, and
    it sits after the source panel rather than before it.
    """
    html = client.get(f"/?source_id={source}").text

    assert html.index("1 &middot; Source") < html.index('class="wholine"')


def test_profile_context_is_one_line_not_a_panel(client):
    html = client.get("/").text

    assert 'class="wholine"' in html
    # The old panel is gone, not merely restyled.
    assert 'id="profile"' not in html


def test_managing_the_profile_still_has_one_home(client):
    """Context here, controls in Settings - never the same job in two places."""
    html = client.get("/").text

    assert "/settings#" in html
    for control in ("/ui/profiles/create", "/ui/profiles/signout"):
        assert control not in html


# --- artefacts that look like what they are ---------------------------------


def test_a_thread_renders_as_a_thread(client, source):
    """Numbered, railed, and each tweet showing its own length.

    The character count is the deterministic checker's constraint made
    visible: an operator can see which tweet is at risk before the checker
    reports it.
    """
    job = _job_with(
        source,
        "twitter_x",
        {"tweets": ["First post in the thread.", "Second post."], "hashtags": ["#Patch"]},
    )
    try:
        html = client.get(f"/ui/jobs/{job}/status").text
        assert 'class="thread"' in html
        assert html.count('class="tweet"') == 2
        assert "/280" in html
    finally:
        _drop(job)


def test_a_tweet_near_the_limit_is_flagged_before_the_checker_says_so(client, source):
    long_tweet = "x" * 265
    job = _job_with(source, "twitter_x", {"tweets": [long_tweet], "hashtags": []})
    try:
        html = client.get(f"/ui/jobs/{job}/status").text
        assert "is-near" in html
    finally:
        _drop(job)


def test_a_deck_renders_as_slides_with_notes_outside_the_plate(client, source):
    """Speaker notes are half the deliverable and belong below the slide."""
    job = _job_with(
        source,
        "presentation",
        {
            "title": "Deck",
            "slides": [
                {"heading": "One", "bullets": ["a", "b"], "speaker_notes": "Say this."},
                {"heading": "Two", "bullets": ["c"], "speaker_notes": "Then this."},
            ],
        },
    )
    try:
        html = client.get(f"/ui/jobs/{job}/status").text
        assert html.count('class="slide-plate"') == 2
        assert "slide-notes" in html
        # The notes sit outside the plate, not inside it.
        assert html.index("slide-notes") > html.index("slide-plate")
    finally:
        _drop(job)


def test_a_video_package_renders_as_a_shot_list_with_proportional_durations(client, source):
    """An unbalanced package is visible without adding the seconds up."""
    job = _job_with(
        source,
        "video_package",
        {
            "title": "Package",
            "scenes": [
                {"duration_sec": 30, "narration": "n1", "visual": "v1", "on_screen_text": "t1"},
                {"duration_sec": 90, "narration": "n2", "visual": "v2", "on_screen_text": "t2"},
            ],
            "visual_recommendations": ["r"],
        },
    )
    try:
        html = client.get(f"/ui/jobs/{job}/status").text
        assert 'class="shots"' in html
        assert "120s" in html  # the total, computed not stated
        # 30 of 120 and 90 of 120 - proportions, not equal bars.
        assert "width: 25%" in html
        assert "width: 75%" in html
    finally:
        _drop(job)


def test_an_infographic_renders_as_panels_with_the_statistic_largest(client, source):
    job = _job_with(
        source,
        "infographic",
        {
            "headline": "H",
            "panels": [
                {"stat": "9.3", "heading": "Severity", "caption": "c1"},
                {"stat": "2.8.4", "heading": "Fix", "caption": "c2"},
            ],
            "layout_recommendation": "stack",
        },
    )
    try:
        html = client.get(f"/ui/jobs/{job}/status").text
        assert html.count('class="ipanel"') == 2
        assert "ipanel-stat" in html
    finally:
        _drop(job)

    # The statistic is the biggest thing in the panel, which is what an
    # infographic panel is for.
    block = _CSS.split(".ipanel-stat", 1)[1].split("}", 1)[0]
    assert "--step-3" in block


def test_every_format_still_has_its_partial():
    """The dynamic include breaks silently if a file goes missing."""
    partials = _TEMPLATES / "partials"
    for format_id in registry.ids():
        assert (partials / f"artefact_{format_id}.html").exists(), format_id


def test_artefact_layouts_use_the_format_hue_not_a_new_palette():
    """Stage 3's identity colours carry through into the content.

    A second colour system inside the artefact bodies would mean a tweet was
    one colour on the tile and another in the card.
    """
    for selector in (".tweet-n", ".shot-n", ".ipanel-stat"):
        block = _CSS.split(selector, 1)[1].split("}", 1)[0]
        # Either weight of the hue counts - the identity for a fill, the
        # readable sibling for text. What matters is that it is the FORMAT's
        # colour rather than a second palette invented for artefact bodies.
        assert "var(--fmt" in block, f"{selector} does not use the format hue"


# --- prose ------------------------------------------------------------------


def test_the_interface_does_not_explain_itself_where_controls_speak(client):
    """Cut the lead paragraphs that restated what the buttons already said.

    Kept: the warning that this dashboard has no login, because that is a real
    constraint an operator cannot infer from a control.
    """
    html = client.get("/settings").text

    assert "anyone using this machine is whoever the cookie says" not in html
    assert "Switching to a new profile starts its learning from nothing" not in html
    # The honest gap stays.
    assert "no login" in html


# --- contrast ---------------------------------------------------------------


def test_a_colour_that_carries_text_has_a_readable_variant():
    """Measured in the browser, then pinned here.

    The format hues sit at 64% lightness, which is right for a border and
    measured 2.87-3.18:1 as text - under WCAG AA. Darkening the identity would
    have cost the thing that makes a rack of seven scannable, so text uses a
    darker sibling at the same hue instead. --flag has the same split, because
    it doubles as a badge fill.
    """
    for spec in registry.all_formats():
        assert f"--fmt-ink-{spec.id}:" in _CSS, f"{spec.id} has no readable text hue"

    assert "--flag-ink:" in _CSS


def test_text_uses_the_readable_variant_not_the_identity_hue():
    """The whole point: a fill may be light, a label may not."""
    for selector in (".shot-n", ".tag-pill", ".ipanel-stat"):
        block = _CSS.split(selector, 1)[1].split("}", 1)[0]
        assert "color: var(--fmt-ink)" in block, f"{selector} paints text with the fill hue"

    for selector in (".fixnote strong", ".tweet-count.is-near"):
        block = _CSS.split(selector, 1)[1].split("}", 1)[0]
        assert "--flag-ink" in block, f"{selector} paints a warning with the fill hue"


def test_the_readable_variants_exist_in_both_themes():
    """A dark ground needs the opposite adjustment, not the same one."""
    # Light plus dark: two definitions minimum for each.
    assert _CSS.count("--fmt-ink-twitter_x:") >= 2
    assert _CSS.count("--flag-ink:") >= 2
