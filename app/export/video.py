"""Video PACKAGE rendering. Deterministic throughout.

Invariant 10, ARCHITECTURE.md sec.9. The MODEL produced script, storyboard,
narration, subtitles and visual direction - all text. This module turns that
text into files with no generative step whatsoever:

    narration  -> Edge TTS          -> audio
    subtitles  -> SRT timed from duration_sec
    scenes     -> title cards drawn with PIL
    everything -> stitched with ffmpeg

No video-generation API is called here or anywhere else (TC-0705). Never
describe this as video generation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720


def render_package(artefact: dict, out_dir: str, basename: str = "video") -> list[str]:
    """Render the full package. Returns every path written.

    Each stage degrades independently: no TTS engine still yields SRT and title
    cards, and no ffmpeg still yields audio and stills. A demo machine missing
    one binary loses one artefact, not the package.
    """
    os.makedirs(out_dir, exist_ok=True)
    written: list[str] = []

    # 1. The package itself, always.
    import json

    package_path = os.path.join(out_dir, f"{basename}.json")
    with open(package_path, "w", encoding="utf-8") as fh:
        json.dump(artefact, fh, indent=2, ensure_ascii=False)
    written.append(package_path)

    scenes = artefact.get("scenes") or []

    # 2. Subtitles: timed from duration_sec, deterministic.
    srt_path = os.path.join(out_dir, f"{basename}.srt")
    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write(artefact.get("subtitles_srt") or build_srt(scenes))
    written.append(srt_path)

    # 3. Title cards.
    card_paths: list[str] = []
    for i, scene in enumerate(scenes, start=1):
        try:
            card_paths.append(title_card(scene, os.path.join(out_dir, f"{basename}_scene{i}.png")))
        except Exception as exc:  # noqa: BLE001
            log.warning("title card %d failed: %s", i, exc)
    written.extend(card_paths)

    # 4. Narration audio.
    audio_paths: list[str] = []
    for i, scene in enumerate(scenes, start=1):
        narration = scene.get("narration", "")
        if not narration:
            continue
        try:
            audio_paths.append(tts(narration, os.path.join(out_dir, f"{basename}_scene{i}.mp3")))
        except Exception as exc:  # noqa: BLE001
            log.warning("TTS for scene %d failed: %s", i, exc)
    written.extend(audio_paths)

    # 5. Stitch, only if both halves exist.
    if card_paths and audio_paths and len(card_paths) == len(audio_paths):
        try:
            written.append(
                stitch(scenes, audio_paths, os.path.join(out_dir, f"{basename}.mp4"), card_paths)
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ffmpeg stitch failed: %s", exc)

    return written


def build_srt(scenes: list[dict]) -> str:
    """Build SRT with cue timings summed from duration_sec (TC-0703)."""
    lines: list[str] = []
    cursor = 0.0
    for i, scene in enumerate(scenes, start=1):
        duration = float(scene.get("duration_sec", 0) or 0)
        start, end = cursor, cursor + duration
        cursor = end
        lines += [
            str(i),
            f"{_timestamp(start)} --> {_timestamp(end)}",
            scene.get("narration", "").strip(),
            "",
        ]
    return "\n".join(lines)


def _timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def title_card(scene: dict, path: str) -> str:
    """Draw a title card. PIL only - no stock imagery, no generation."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), "#0f172a")
    draw = ImageDraw.Draw(img)

    text = scene.get("on_screen_text") or scene.get("visual") or ""
    for i, line in enumerate(_wrap(text, 32)[:6]):
        draw.text((80, 220 + i * 48), line, fill="#f8fafc")

    img.save(path)
    return path


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = str(text).split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def tts(text: str, out_path: str, voice: str = "en-US-AriaNeural") -> str:
    """Narration to audio via Edge TTS."""
    import edge_tts

    async def run():
        await edge_tts.Communicate(text, voice).save(out_path)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run())
        return out_path

    # Already inside an event loop: run in a worker thread with its own loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, run()).result(timeout=120)
    return out_path


def _audio_duration(path: str) -> float:
    """Length of a narration clip, so a long take is never cut off."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return 0.0


def stitch(
    scenes: list[dict],
    audio_paths: list[str],
    out_path: str,
    card_paths: list[str] | None = None,
) -> str:
    """Assemble title cards + narration into an mp4 with ffmpeg.

    Deterministic concatenation. No model is involved at any point.
    """
    if not card_paths:
        raise ValueError("stitching needs title cards")

    out_dir = os.path.dirname(out_path) or "."
    segments: list[str] = []

    for i, (card, audio) in enumerate(zip(card_paths, audio_paths, strict=False), start=1):
        segment = os.path.join(out_dir, f"_seg{i}.mp4")

        # Hold each scene for the duration the MODEL specified, not for however
        # long the TTS voice happened to take. The SRT is timed from
        # duration_sec, so timing the video from audio length instead would let
        # subtitles and picture drift apart - which is exactly what TC-0703
        # exists to catch. Narration shorter than the scene is padded with
        # silence; narration longer than the scene extends it rather than being
        # cut off mid-sentence.
        scene = scenes[i - 1] if i - 1 < len(scenes) else {}
        target = float(scene.get("duration_sec", 0) or 0)
        duration = max(target, _audio_duration(audio))

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-loop",
                "1",
                "-i",
                card,
                "-i",
                audio,
                "-f",
                "lavfi",
                "-t",
                str(duration),
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                # Overlay the narration onto silence of the full scene length so
                # the audio stream lasts as long as the picture.
                "-filter_complex",
                "[1:a][2:a]amix=inputs=2:duration=longest:dropout_transition=0[a]",
                "-map",
                "0:v",
                "-map",
                "[a]",
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-tune",
                "stillimage",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "10",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                segment,
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
        segments.append(segment)

    list_file = os.path.join(out_dir, "_concat.txt")
    with open(list_file, "w", encoding="utf-8") as fh:
        for segment in segments:
            fh.write(f"file '{os.path.basename(segment)}'\n")

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file,
            "-c",
            "copy",
            out_path,
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )

    for temp in [*segments, list_file]:
        with contextlib.suppress(OSError):
            os.unlink(temp)

    return out_path
