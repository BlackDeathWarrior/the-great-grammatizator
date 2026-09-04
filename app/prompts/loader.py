"""Prompt loading. Prompts live in versioned FILES, never string literals.

Invariant 9. Versioning is what lets an eval run compare v1 against v2 on the
same fixed test set (ARCHITECTURE.md sec.10); without it every prompt change is
a guess.

Naming: <name>@v<N>.jinja for the user prompt, <name>@v<N>.system.txt for the
system prompt. `latest_version` picks the highest N unless one is pinned.
"""

from __future__ import annotations

import functools
import logging
import pathlib
import re

log = logging.getLogger(__name__)

PROMPTS_DIR = pathlib.Path(__file__).parent / "templates"
_VERSION_RE = re.compile(r"^(?P<name>.+)@v(?P<version>\d+)\.jinja$")


# How many chunks a generation prompt may list. Until real top-k selection
# lands (docs/v2/ARCHITECTURE.md §13.2) the generator sees chunks in document
# order, so this is a context-window guard rather than a relevance decision.
MAX_PROMPT_CHUNKS = 60


@functools.lru_cache(maxsize=1)
def _env():
    from jinja2 import Environment, FileSystemLoader, StrictUndefined

    env = Environment(
        loader=FileSystemLoader(str(PROMPTS_DIR)),
        # StrictUndefined: a typo in a template name must fail loudly at render
        # time rather than silently producing a prompt with an empty slot.
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # A global so every template gets it without each caller remembering to
    # pass it - and StrictUndefined would make forgetting a hard error.
    # _shared.jinja listed every chunk at 400 chars with no cap, so a long
    # source produced a prompt the provider rejects on length, on every retry.
    env.globals["max_chunks"] = MAX_PROMPT_CHUNKS
    # Optional blocks. StrictUndefined is deliberate - a typo must fail loudly -
    # so anything a caller MAY omit needs a default here rather than at every
    # call site.
    env.globals["style_notes"] = []
    env.globals["approach"] = ""
    return env


def versions(name: str) -> list[int]:
    out = []
    for path in PROMPTS_DIR.glob(f"{name}@v*.jinja"):
        m = _VERSION_RE.match(path.name)
        if m:
            out.append(int(m.group("version")))
    return sorted(out)


def latest_version(name: str) -> int:
    found = versions(name)
    if not found:
        raise FileNotFoundError(f"No prompt template named {name!r} in {PROMPTS_DIR}")
    return found[-1]


def resolve(spec: str) -> tuple[str, int]:
    """Accept 'linkedin_post' or a pinned 'linkedin_post@v3'."""
    if "@v" in spec:
        name, _, version = spec.partition("@v")
        return name, int(version)
    return spec, latest_version(spec)


def version_tag(spec: str) -> str:
    """Canonical 'name@vN', for the cache key and the trace."""
    name, version = resolve(spec)
    return f"{name}@v{version}"


def render(spec: str, **context) -> str:
    """Render a prompt template."""
    name, version = resolve(spec)
    return _env().get_template(f"{name}@v{version}.jinja").render(**context).strip()


def system(spec: str) -> str:
    """The system prompt beside a template, if one exists."""
    name, version = resolve(spec)
    path = PROMPTS_DIR / f"{name}@v{version}.system.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""
