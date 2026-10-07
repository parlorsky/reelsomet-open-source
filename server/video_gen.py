"""FFmpeg-only video generation for VPS (no numpy/Pillow/moviepy).

Generates slideshow videos using only the ffmpeg CLI. Designed for
resource-constrained environments (1-core VPS, 2 GB RAM).

All public functions are async and shell out to ffmpeg/ffprobe via
asyncio.create_subprocess_exec.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
_SIZE = f"{OUTPUT_WIDTH}x{OUTPUT_HEIGHT}"

# Maximum characters per text line before wrapping
_MAX_LINE_CHARS = 28

# Vertical gap (pixels) between multi-line drawtext entries
_LINE_SPACING = 12

# Heavy vid_bait sources can exceed 120s on 1-core VPS instances.
_VID_BAIT_PRESET = "veryfast"
_VID_BAIT_RENDER_TIMEOUT = 300.0
_MIN_VID_BAIT_FONT_SIZE = 28


@dataclass(frozen=True)
class _TextOverlayLayout:
    font_size: int
    text_y: int
    max_line_chars: int


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ffmpeg_bin() -> str:
    """Return path to ffmpeg binary (expects it on PATH)."""
    path = shutil.which("ffmpeg")
    if path is None:
        raise FileNotFoundError("ffmpeg not found on PATH")
    return path


def _ffprobe_bin() -> str:
    """Return path to ffprobe binary (expects it on PATH)."""
    path = shutil.which("ffprobe")
    if path is None:
        raise FileNotFoundError("ffprobe not found on PATH")
    return path


def _escape_text(text: str) -> str:
    """Escape text for ffmpeg drawtext filter.

    Handles backslashes, single quotes, colons, semicolons, and other
    special characters that the drawtext filter interprets.  Preserves
    Cyrillic and other Unicode characters as-is.
    """
    # Order matters: backslash first so we don't double-escape later subs.
    text = text.replace("\\", "\\\\\\\\")
    text = text.replace("'", "\u2019")          # typographic apostrophe
    text = text.replace(":", "\\:")
    text = text.replace(";", "\\;")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace(",", "\\,")
    text = text.replace("%", "%%")
    # Newlines -> space (drawtext cannot render literal newlines)
    text = text.replace("\n", " ")
    return text


def _wrap_lines(text_lines: list[str], max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """Word-wrap each input line so no rendered line exceeds *max_chars*.

    Returns a flat list of wrapped lines ready for individual drawtext
    filters.
    """
    result: list[str] = []
    for line in text_lines:
        wrapped = textwrap.wrap(line.strip(), width=max_chars, break_long_words=True, break_on_hyphens=False)
        result.extend(wrapped if wrapped else [""])
    return result


def _max_line_chars_for_frame(width: int, font_size: int) -> int:
    """Estimate a safe wrap width for centred drawtext on a specific frame."""
    usable_width = max(1, int(width * 0.92))
    average_char_width = max(1.0, font_size * 0.58)
    estimated = int(usable_width / average_char_width)
    return max(12, min(_MAX_LINE_CHARS, estimated))


def _resolve_vid_bait_text_layout(
    *,
    width: int,
    height: int,
    requested_font_size: int,
    requested_text_y: int | None,
) -> _TextOverlayLayout:
    """Resolve vid_bait subtitle layout against the actual source frame size."""
    if width <= 0 or height <= 0:
        raise ValueError(f"Invalid video dimensions: {width}x{height}")

    scale = min(width / OUTPUT_WIDTH, height / OUTPUT_HEIGHT)
    font_size = max(
        _MIN_VID_BAIT_FONT_SIZE,
        min(requested_font_size, int(round(requested_font_size * scale))),
    )

    if requested_text_y is None:
        text_y = int(height * 0.65)
    elif requested_text_y > height:
        text_y = int(height * (requested_text_y / OUTPUT_HEIGHT))
    else:
        text_y = requested_text_y

    return _TextOverlayLayout(
        font_size=font_size,
        text_y=text_y,
        max_line_chars=_max_line_chars_for_frame(width, font_size),
    )


def _build_drawtext_chain(
    lines: list[str],
    font_path: str | None,
    font_size: int,
    text_color: str,
    outline_width: int,
    outline_color: str,
    text_y: int,
    frame_height: int | None = None,
) -> str:
    """Build a comma-separated chain of drawtext filters for *lines*.

    Text is centred horizontally.  *text_y* is the vertical centre of the
    entire text block; individual lines are spread above and below it.
    """
    if not lines:
        return ""

    line_height = font_size + _LINE_SPACING
    total_block = line_height * len(lines) - _LINE_SPACING
    top_y = text_y - total_block // 2
    if frame_height is not None and frame_height > 0:
        margin = max(24, int(frame_height * 0.04))
        max_top_y = frame_height - total_block - margin
        if max_top_y < margin:
            top_y = margin
        else:
            top_y = min(max(top_y, margin), max_top_y)

    parts: list[str] = []
    for idx, raw_line in enumerate(lines):
        escaped = _escape_text(raw_line)
        y = top_y + idx * line_height

        font_clause = f":fontfile='{font_path}'" if font_path else ""
        dt = (
            f"drawtext=text='{escaped}'"
            f"{font_clause}"
            f":fontsize={font_size}"
            f":fontcolor={text_color}"
            f":borderw={outline_width}"
            f":bordercolor={outline_color}"
            f":x=(w-text_w)/2"
            f":y={y}"
        )
        parts.append(dt)

    return ",".join(parts)


async def _run_ffmpeg(
    args: list[str],
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """Run an ffmpeg/ffprobe command asynchronously.

    Returns (returncode, stdout, stderr).  Raises ``TimeoutError`` if the
    process does not finish within *timeout* seconds.
    """
    cmd_str = " ".join(args)
    logger.debug("Running: %s", cmd_str)

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"ffmpeg command timed out after {timeout}s: {cmd_str}")

    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

    if proc.returncode != 0:
        logger.error(
            "ffmpeg failed (rc=%d): %s\nstderr: %s",
            proc.returncode,
            cmd_str,
            stderr[-2000:],
        )

    return proc.returncode, stdout, stderr


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_media_duration(path: Path) -> float:
    """Return the duration (seconds) of a media file via ffprobe.

    Raises ``FileNotFoundError`` if the file does not exist and
    ``RuntimeError`` if ffprobe cannot determine the duration.
    """
    if not path.exists():
        raise FileNotFoundError(f"Media file not found: {path}")

    rc, stdout, stderr = await _run_ffmpeg(
        [
            _ffprobe_bin(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=30.0,
    )

    if rc != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {stderr[:500]}")

    try:
        return float(stdout.strip())
    except ValueError:
        raise RuntimeError(f"ffprobe returned non-numeric duration for {path}: {stdout!r}")


async def get_media_dimensions(path: Path) -> tuple[int, int]:
    """Return the first video stream dimensions as ``(width, height)``."""
    if not path.exists():
        raise FileNotFoundError(f"Media file not found: {path}")

    rc, stdout, stderr = await _run_ffmpeg(
        [
            _ffprobe_bin(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0",
            str(path),
        ],
        timeout=30.0,
    )

    if rc != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {stderr[:500]}")

    raw = stdout.strip()
    try:
        width_raw, height_raw = raw.split("x", 1)
        width = int(width_raw)
        height = int(height_raw)
    except ValueError:
        raise RuntimeError(f"ffprobe returned invalid dimensions for {path}: {raw!r}")

    if width <= 0 or height <= 0:
        raise RuntimeError(f"ffprobe returned invalid dimensions for {path}: {raw!r}")

    return width, height


async def generate_simple_video(
    photo_path: Path,
    audio_path: Path,
    output_path: Path,
    text_lines: list[str],
    duration: float = 10.0,
    fps: int = 30,
    crf: int = 18,
    font_path: str | None = None,
    font_size: int = 56,
    text_color: str = "white",
    outline_width: int = 3,
    outline_color: str = "black",
    text_y: int = 800,
    zoom_speed: float = 0.001,
    max_zoom: float = 1.08,
) -> Path:
    """Create a Ken-Burns slideshow video with text overlay and music.

    Scales the photo to cover 1080x1920, applies a slow zoom (Ken Burns),
    draws centred text, and mixes in the audio track.

    Returns *output_path* on success.
    """
    if not photo_path.exists():
        raise FileNotFoundError(f"Photo not found: {photo_path}")
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_frames = int(duration * fps)

    # Build filter graph
    wrapped = _wrap_lines(text_lines)
    dt_chain = _build_drawtext_chain(
        wrapped, font_path, font_size, text_color, outline_width, outline_color, text_y,
        frame_height=OUTPUT_HEIGHT,
    )

    # Video filter: scale -> crop -> zoompan [-> drawtext*]
    vf_parts = [
        f"scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:force_original_aspect_ratio=increase",
        f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}",
        (
            f"zoompan=z='min(zoom+{zoom_speed},{max_zoom})'"
            f":d={total_frames}:s={_SIZE}:fps={fps}"
        ),
    ]
    if dt_chain:
        vf_parts.append(dt_chain)

    filter_complex = f"[0:v]{','.join(vf_parts)}[v]"

    cmd = [
        _ffmpeg_bin(), "-y",
        "-loop", "1", "-i", str(photo_path),
        "-i", str(audio_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-t", str(duration),
        "-c:v", "libx264", "-crf", str(crf),
        "-c:a", "aac", "-b:a", "256k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(output_path),
    ]

    rc, _, stderr = await _run_ffmpeg(cmd, timeout=max(300.0, duration * 10))
    if rc != 0:
        raise RuntimeError(f"generate_simple_video failed: {stderr[-1000:]}")

    return output_path


async def generate_text_on_video(
    video_path: Path,
    output_path: Path,
    text_lines: list[str],
    font_path: str | None = None,
    font_size: int = 56,
    text_color: str = "white",
    outline_width: int = 3,
    outline_color: str = "black",
    text_y: int = 800,
) -> Path:
    """Overlay centred text on an existing video.

    Returns *output_path* on success.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = await get_media_dimensions(video_path)
    wrapped = _wrap_lines(text_lines, max_chars=_max_line_chars_for_frame(width, font_size))
    dt_chain = _build_drawtext_chain(
        wrapped, font_path, font_size, text_color, outline_width, outline_color, text_y,
        frame_height=height,
    )

    if not dt_chain:
        # Nothing to draw -- just copy
        cmd = [_ffmpeg_bin(), "-y", "-i", str(video_path), "-c", "copy", str(output_path)]
    else:
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-vf", dt_chain,
            "-c:v", "libx264", "-crf", "18",
            "-c:a", "copy",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]

    rc, _, stderr = await _run_ffmpeg(cmd)
    if rc != 0:
        raise RuntimeError(f"generate_text_on_video failed: {stderr[-1000:]}")

    return output_path


async def generate_countdown(
    output_path: Path,
    duration: float = 3.0,
    fps: int = 30,
    bg_color: str = "#1a1a2e",
    text_color: str = "white",
    font_path: str | None = None,
    font_size: int = 200,
) -> Path:
    """Generate a 3-2-1 countdown video segment.

    Each number is displayed for ``duration / 3`` seconds using drawtext
    ``enable`` expressions over a solid colour background.

    Returns *output_path* on success.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    segment = duration / 3.0
    t1 = segment
    t2 = segment * 2

    font_clause = f":fontfile='{font_path}'" if font_path else ""

    # Three drawtext filters, each enabled for its third of the duration
    dt3 = (
        f"drawtext=text='3'"
        f"{font_clause}"
        f":fontsize={font_size}:fontcolor={text_color}"
        f":x=(w-text_w)/2:y=(h-text_h)/2"
        f":enable='between(t,0,{t1})'"
    )
    dt2 = (
        f"drawtext=text='2'"
        f"{font_clause}"
        f":fontsize={font_size}:fontcolor={text_color}"
        f":x=(w-text_w)/2:y=(h-text_h)/2"
        f":enable='between(t,{t1},{t2})'"
    )
    dt1 = (
        f"drawtext=text='1'"
        f"{font_clause}"
        f":fontsize={font_size}:fontcolor={text_color}"
        f":x=(w-text_w)/2:y=(h-text_h)/2"
        f":enable='between(t,{t2},{duration})'"
    )

    vf = f"{dt3},{dt2},{dt1}"

    cmd = [
        _ffmpeg_bin(), "-y",
        "-f", "lavfi",
        "-i", f"color=c={bg_color}:s={_SIZE}:d={duration}:r={fps}",
        "-f", "lavfi",
        "-i", f"sine=frequency=440:duration={duration}:sample_rate=44100",
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        str(output_path),
    ]

    rc, _, stderr = await _run_ffmpeg(cmd, timeout=60.0)
    if rc != 0:
        raise RuntimeError(f"generate_countdown failed: {stderr[-1000:]}")

    return output_path


async def concat_videos(
    segments: list[Path],
    output_path: Path,
) -> Path:
    """Concatenate video segments using the ffmpeg concat demuxer.

    All segments must share the same codec, resolution, and timebase.
    Returns *output_path* on success.
    """
    if not segments:
        raise ValueError("No segments provided for concatenation")

    for seg in segments:
        if not seg.exists():
            raise FileNotFoundError(f"Segment not found: {seg}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a temporary concat list file
    tmp_dir = output_path.parent
    list_path = tmp_dir / f"_concat_{output_path.stem}.txt"
    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for seg in segments:
                # Paths must use forward slashes and be single-quoted for
                # the concat demuxer to handle spaces / special chars.
                safe = str(seg.resolve()).replace("\\", "/")
                f.write(f"file '{safe}'\n")

        cmd = [
            _ffmpeg_bin(), "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c", "copy",
            str(output_path),
        ]

        rc, _, stderr = await _run_ffmpeg(cmd, timeout=120.0)
        if rc != 0:
            raise RuntimeError(f"concat_videos failed: {stderr[-1000:]}")
    finally:
        list_path.unlink(missing_ok=True)

    return output_path


# ---------------------------------------------------------------------------
# vid_bait pipeline: existing video clip + subtitle overlay
# ---------------------------------------------------------------------------

async def generate_vid_bait(
    video_path: Path,
    output_path: Path,
    text_lines: list[str],
    font_path: str | None = None,
    font_size: int = 52,
    text_color: str = "white",
    outline_width: int = 3,
    outline_color: str = "black",
    text_y: int | None = None,
) -> Path:
    """Take a vid_bait clip with original audio and overlay subtitle text.

    This is the primary video generation method. The vid_bait clips are
    pre-made short videos (5-15s) with music/effects baked in. We only
    add centred text as a subtitle overlay.

    Parameters
    ----------
    video_path : Path
        Source vid_bait clip (must have audio).
    output_path : Path
        Where to write the output video.
    text_lines : list[str]
        Text to overlay (each string is a separate line).
    text_y : int | None
        Vertical position. If None, auto-centres at 65% of frame height.
    """
    if not video_path.exists():
        raise FileNotFoundError(f"vid_bait clip not found: {video_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    width, height = await get_media_dimensions(video_path)
    layout = _resolve_vid_bait_text_layout(
        width=width,
        height=height,
        requested_font_size=font_size,
        requested_text_y=text_y,
    )
    wrapped = _wrap_lines(text_lines, max_chars=layout.max_line_chars)
    dt_chain = _build_drawtext_chain(
        wrapped, font_path, layout.font_size, text_color, outline_width, outline_color, layout.text_y,
        frame_height=height,
    )

    if not dt_chain:
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-c:v", "libx264", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]
    else:
        cmd = [
            _ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-vf", dt_chain,
            "-c:v", "libx264", "-crf", "20", "-preset", _VID_BAIT_PRESET,
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            str(output_path),
        ]

    rc, _, stderr = await _run_ffmpeg(cmd, timeout=_VID_BAIT_RENDER_TIMEOUT)
    if rc != 0:
        raise RuntimeError(f"vid_bait render failed: {stderr[-1000:]}")

    logger.info("vid_bait rendered: %s → %s", video_path.name, output_path.name)
    return output_path
