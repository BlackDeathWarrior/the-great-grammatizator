"""Phase 21: the console, and the contract that keeps it alive across a poll.

The status region replaces itself wholesale every two seconds, and the same
partial comes back from all five action POSTs. Everything in this file guards
one fact: nothing in that region survives a swap unless it was designed to.

These are source-level assertions rather than browser tests, because the ways
this breaks are all removals - an id dropped, a global scoped, an attribute
lost in a rewrite - and each one fails silently in a way that looks like the
feature was never built. A test that reads the template catches the removal;
only a browser catches the behaviour, and there is no browser in CI.
"""

from __future__ import annotations

import json
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "app" / "web" / "templates"
_STATIC = _ROOT / "app" / "web" / "static"
_STATUS = (_TEMPLATES / "partials" / "status.html").read_text(encoding="utf-8")
_BASE = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
_CONSOLE = (_STATIC / "console.js").read_text(encoding="utf-8")


# --- what the operator typed -------------------------------------------------


def test_the_two_text_fields_survive_a_poll():
    """A sentence half-typed when the poll lands must still be there after it.

    hx-preserve moves the original node into the new content instead of
    rendering a fresh one, which is the only thing keeping a rating reason or
    a regeneration instruction from being wiped every two seconds.
    """
    assert 'name="reason"' in _STATUS
    assert 'name="instructions"' in _STATUS
    # Both fields, and nothing else: preserving a whole lane would freeze the
    # checker lamps inside it, which is the opposite of what this view is for.
    assert _STATUS.count('hx-preserve="true"') == 2


def test_a_preserved_field_carries_an_id_that_is_unique_per_artefact():
    """hx-preserve is a silent no-op without a stable id.

    Seven artefacts render seven copies of each field, so a literal id would
    collide and htmx would keep the wrong one - or none. output_type is unique
    within a job by construction (one artefact row per selected format).
    """
    assert 'id="reason-{{ a.output_type }}"' in _STATUS
    assert 'id="instructions-{{ a.output_type }}"' in _STATUS


# --- what the operator opened ------------------------------------------------


def test_every_lane_is_a_disclosure_with_a_stable_id():
    """The store keys on these ids, so they may not become positional."""
    assert 'id="lane-{{ a.output_type }}"' in _STATUS
    assert 'class="lane"' in _STATUS


def test_a_lane_in_trouble_opens_itself():
    """The failure is the interesting part (PRODUCT.md, principle 3).

    An operator scanning seven collapsed lanes should not have to click to
    discover which one stopped.
    """
    assert "{% if a.status in ('blocked', 'failed') %}open{% endif %}" in _STATUS


def test_the_client_restores_what_the_server_cannot_know():
    """The server renders a default; the operator's own choice must win."""
    assert "htmx:afterSwap" in _CONSOLE
    assert "applyLaneState" in _CONSOLE
    assert "sessionStorage" in _CONSOLE


def test_the_toggle_listener_is_delegated_not_per_element():
    """Thirty swaps a minute against seven lanes leaks listeners fast.

    One listener on the document, in the capture phase because `toggle` does
    not bubble.
    """
    assert re.search(r"document\.addEventListener\(\s*'toggle'", _CONSOLE)
    assert _CONSOLE.count("addEventListener('toggle'") == 1


# --- motion and sound that mean something ------------------------------------


def test_a_lane_carries_a_token_of_what_it_is_showing():
    """Motion keyed to an element being new fires on every poll forever.

    The token changes only when something actually moved, which is what makes
    the flash - and the tick - report work rather than report a redraw.
    """
    assert 'data-state-token="{{ a.state_token }}"' in _STATUS
    jobs_api = (_ROOT / "app" / "api" / "jobs.py").read_text(encoding="utf-8")
    assert "state_token" in jobs_api


def test_sound_is_off_until_asked_for_and_silent_under_reduced_motion():
    """Unrequested noise in a work tool is a bug, not a feature."""
    assert "on: false" in _CONSOLE
    assert "prefers-reduced-motion" in _CONSOLE
    # enabled() gates every blip on both conditions at once.
    assert "return this.on && !reduced.matches;" in _CONSOLE


# --- the wiring that is easy to break ----------------------------------------


def test_the_brief_mode_switch_is_reachable_from_swapped_in_markup():
    """interview.html calls showLists() from an inline onclick, and that markup
    arrives from htmx long after console.js has run. A module scope, or a
    function left inside the IIFE, puts it out of reach and the button dies
    silently.
    """
    assert "window.showLists" in _CONSOLE
    assert "window.showInterview" in _CONSOLE
    interview = (_TEMPLATES / "partials" / "interview.html").read_text(encoding="utf-8")
    assert "showLists()" in interview


def test_the_console_is_loaded_and_not_a_module():
    assert "/static/console.js" in _BASE
    assert "defer" in _BASE
    assert 'type="module"' not in _BASE


def test_both_static_files_are_fingerprinted():
    """Stat only the stylesheet and an edited console.js ships invisibly.

    The CSS fingerprint exists precisely because a cached asset surviving a
    rebuild is a long way to chase; JS has the same problem and hides better.
    """
    routes = (_ROOT / "app" / "web" / "routes.py").read_text(encoding="utf-8")
    assert "console.js" in routes
    assert "app.css" in routes
    assert _BASE.count("asset_version") >= 2


# --- things the rewrite must not have quietly dropped ------------------------


def test_the_status_partial_still_stops_its_own_polling():
    assert "{% if not settled %}" in _STATUS
    assert 'hx-trigger="every 2s"' in _STATUS
    assert 'hx-swap="outerHTML"' in _STATUS


def test_the_rack_does_not_shout_every_two_seconds():
    """aria-live on the whole region reads seven lanes aloud on every poll.

    One line carries the announcement instead, which is the difference between
    a status region and a reason to leave the page.
    """
    assert 'aria-live="polite"' in _STATUS
    before = _STATUS.split('aria-live="polite"', 1)[0]
    # The live region is the small paragraph, not the container that holds the
    # pipeline and every lane.
    assert before.rstrip().endswith('<p class="sr-only" role="status"')
    assert "aria-busy=" in _STATUS


def test_no_format_id_is_written_into_the_status_partial():
    """A new format must appear from config alone (Invariant 4).

    The lane rewrite is exactly where somebody reaches for
    `{% if a.output_type == 'presentation' %}`.
    """
    registry = json.loads((_ROOT / "app" / "formats" / "registry.json").read_text(encoding="utf-8"))
    for fmt_id in (f["id"] for f in registry["formats"]):
        assert f"'{fmt_id}'" not in _STATUS, fmt_id
        assert f'"{fmt_id}"' not in _STATUS, fmt_id
