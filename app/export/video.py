"""Video PACKAGE rendering. Deterministic: TTS + SRT + title cards + ffmpeg.

Never generative video (Invariant 10). Real implementation in Phase 9.
"""

from __future__ import annotations


def tts(text: str, out_path: str, voice: str = "en-US-AriaNeural") -> str:
    raise NotImplementedError("TTS lands in Phase 9")


def stitch(scenes: list[dict], audio_paths: list[str], out_path: str) -> str:
    raise NotImplementedError("ffmpeg stitching lands in Phase 9")
