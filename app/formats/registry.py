"""The output registry. Data, not code.

Adding a format is one config entry + one template + one schema. No code change
(ARCHITECTURE.md sec.7, TC-0302). If you find yourself editing a switch
statement to add a format, something has gone wrong (Invariant 4).
"""

from __future__ import annotations

import functools
import json
import pathlib
from typing import Any

from pydantic import BaseModel, Field

HERE = pathlib.Path(__file__).parent
REGISTRY_PATH = HERE / "registry.json"
SCHEMA_DIR = HERE / "schemas"


class FormatSpec(BaseModel):
    """One registry entry. Supplies everything the pipeline needs per format."""

    model_config = {"frozen": True}

    id: str
    label: str
    description: str = ""
    prompt_template: str
    output_schema: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    model_alias: str
    renderer: str

    def schema(self) -> dict[str, Any]:
        return _load_schema(self.output_schema)

    @property
    def shape(self) -> str:
        """What this format produces, in a phrase, derived from its constraints.

        Read off the registry rather than written per format, because a switch
        on format id here would be exactly what Invariant 4 forbids - a new
        format has to appear in the dashboard from config alone (TC-0302). The
        pairs below are named by CONSTRAINT, so a format declaring
        `slides_min`/`slides_max` describes itself without this file ever
        learning that decks exist.
        """
        c = self.constraints
        for lo, hi, noun in (
            ("scenes_min", "scenes_max", "scene"),
            ("slides_min", "slides_max", "slide"),
            ("panels_min", "panels_max", "panel"),
            ("tweets_min", "tweets_max", "tweet"),
            ("key_points_min", "key_points_max", "key point"),
        ):
            if lo in c and hi in c:
                return f"{c[lo]}–{c[hi]} {noun}s"
            if lo in c:
                return f"{c[lo]}+ {noun}s"

        for key, noun in (("recommendations_min", "recommendation"),):
            if key in c:
                return f"{c[key]}+ {noun}s"

        # Length-bounded prose: the floor is what an operator actually feels,
        # since the ceiling is rarely the binding constraint.
        for key, unit in (("min_chars", ""), ("min_chars_per_tweet", " per tweet")):
            if key in c:
                return f"{c[key]}+ characters{unit}"

        return ""

    @property
    def runtime_hint(self) -> str:
        """A target duration, where the format has one."""
        target = self.constraints.get("runtime_target_sec")
        if not target:
            return ""
        minutes = target / 60
        return f"~{minutes:.0f} min" if minutes >= 1 else f"~{target}s"


class UnknownFormat(ValueError):
    """TC-0301: rejected before any job is created."""

    def __init__(self, given: str, known: list[str]):
        super().__init__(f"Unknown format {given!r}. Available: {', '.join(known)}.")
        self.given = given
        self.known = known


@functools.lru_cache(maxsize=1)
def _load() -> dict[str, FormatSpec]:
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {entry["id"]: FormatSpec(**entry) for entry in raw["formats"]}


@functools.lru_cache(maxsize=32)
def _load_schema(filename: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / filename).read_text(encoding="utf-8"))


def all_formats() -> list[FormatSpec]:
    """Every registered format, for the dashboard multi-select."""
    return list(_load().values())


def ids() -> list[str]:
    return list(_load())


def get(format_id: str) -> FormatSpec:
    specs = _load()
    if format_id not in specs:
        raise UnknownFormat(format_id, list(specs))
    return specs[format_id]


def validate_selection(format_ids: list[str]) -> list[FormatSpec]:
    """Validate every requested format BEFORE a job is created (TC-0301).

    Rejecting the whole selection rather than silently dropping the bad entry
    means the operator never gets six artefacts when they asked for seven.
    """
    if not format_ids:
        raise ValueError("Select at least one output format.")
    return [get(fid) for fid in format_ids]


def reset_cache() -> None:
    """Test hook: pick up registry edits without a restart."""
    _load.cache_clear()
    _load_schema.cache_clear()
