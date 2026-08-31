"""Video transcription via faster-whisper.

The duration gate is checked BEFORE any transcription work (TC-0108): over the
limit must cost nothing and leave no partial source row.
"""

from __future__ import annotations

import json
import logging
import subprocess

from app.config import get_settings
from app.ingest.errors import CorruptSource, NoReadableContent, VideoTooLong

log = logging.getLogger(__name__)


def probe_duration(path: str) -> float:
    """Seconds, via ffprobe. Cheap: reads container metadata, not the stream."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment guard
        raise CorruptSource("ffprobe unavailable; cannot inspect video") from exc
    except subprocess.CalledProcessError as exc:
        raise CorruptSource(f"Could not read video: {exc.stderr.strip()[:200]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CorruptSource("Timed out reading video metadata") from exc

    try:
        return float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise CorruptSource("Video has no readable duration") from exc


def check_duration(path: str) -> float:
    """Enforce the limit. Called before transcription, never after.

    Boundary is inclusive: exactly 10:00 is accepted (TC-0107), 10:01 is not
    (TC-0108).
    """
    limit = get_settings().video_max_seconds
    seconds = probe_duration(path)
    if seconds > limit:
        raise VideoTooLong(seconds, limit)
    return seconds


def extract(path: str) -> tuple[str, str]:
    """Return (transcript, title). Gate first, then transcribe."""
    check_duration(path)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise CorruptSource("Transcription support unavailable") from exc

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(path, beam_size=1)
    transcript = " ".join(s.text.strip() for s in segments).strip()

    if not transcript:
        raise NoReadableContent("No speech detected in the video.")
    return transcript, ""
