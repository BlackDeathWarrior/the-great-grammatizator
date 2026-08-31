"""Render dispatch. Renderer comes from the registry entry, never a switch on id.

ARCHITECTURE.md sec.9: structured JSON to files. pdf/docx/md, pptx + notes,
mp4/srt/json.

The mapping below is renderer -> function, not format -> function. Seven formats
share five renderers, and a new format naming an existing renderer needs no
change here (Invariant 4).
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)


def render(artefact: dict, renderer: str, out_dir: str, *, basename: str = "artefact") -> list[str]:
    """Render one artefact. Returns the paths written."""
    os.makedirs(out_dir, exist_ok=True)

    handlers = {
        "markdown": _markdown,
        "json": _json,
        "docx": _docx,
        "pptx": _pptx,
        "video": _video,
    }
    handler = handlers.get(renderer)
    if handler is None:
        raise ValueError(f"Unknown renderer {renderer!r}. Known: {', '.join(sorted(handlers))}")
    return handler(artefact, out_dir, basename)


def _json(artefact: dict, out_dir: str, basename: str) -> list[str]:
    path = os.path.join(out_dir, f"{basename}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artefact, fh, indent=2, ensure_ascii=False)
    return [path]


def _markdown(artefact: dict, out_dir: str, basename: str) -> list[str]:
    from app.export import markdown

    path = os.path.join(out_dir, f"{basename}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(markdown.to_markdown(artefact))
    return [path, *_json(artefact, out_dir, basename)]


def _docx(artefact: dict, out_dir: str, basename: str) -> list[str]:
    from app.export import documents

    return [
        documents.to_docx(artefact, os.path.join(out_dir, f"{basename}.docx")),
        documents.to_pdf(artefact, os.path.join(out_dir, f"{basename}.pdf")),
    ]


def _pptx(artefact: dict, out_dir: str, basename: str) -> list[str]:
    from app.export import documents

    return [documents.to_pptx(artefact, os.path.join(out_dir, f"{basename}.pptx"))]


def _video(artefact: dict, out_dir: str, basename: str) -> list[str]:
    from app.export import video

    return video.render_package(artefact, out_dir, basename)
