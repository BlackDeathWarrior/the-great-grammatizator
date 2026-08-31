"""Render and media tools. Export only.

Registered here so the allowlist is complete and testable; the actual renderers
arrive in Phase 9 (ARCHITECTURE.md sec.9).
"""

from __future__ import annotations

from app.tools.registry import ToolGroup, tool


@tool("render_document", ToolGroup.RENDER, "Render an artefact to md/pdf/docx/pptx.")
def render_document(artefact: dict, renderer: str, out_dir: str) -> list[str]:
    from app.export import dispatch

    return dispatch.render(artefact, renderer, out_dir)


@tool("synthesise_speech", ToolGroup.MEDIA, "Narration text to audio via TTS.")
def synthesise_speech(text: str, out_path: str, voice: str = "en-US-AriaNeural") -> str:
    from app.export import video

    return video.tts(text, out_path, voice=voice)


@tool("stitch_video", ToolGroup.MEDIA, "Assemble title cards + audio into an mp4.")
def stitch_video(scenes: list[dict], audio_paths: list[str], out_path: str) -> str:
    from app.export import video

    return video.stitch(scenes, audio_paths, out_path)
