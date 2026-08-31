"""Render dispatch. Real renderers arrive in Phase 9 (ARCHITECTURE.md sec.9)."""

from __future__ import annotations


def render(artefact: dict, renderer: str, out_dir: str) -> list[str]:
    raise NotImplementedError("renderers land in Phase 9")
