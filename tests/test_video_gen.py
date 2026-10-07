"""Tests for server.video_gen — FFmpeg-only video generation.

All tests that invoke ffmpeg are guarded by ``HAS_FFMPEG`` and will be
skipped automatically on machines where ffmpeg is not on PATH.  Test
inputs (colour frames, sine audio) are generated with ffmpeg itself so
no external media files are required.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from server import video_gen
from server.video_gen import (
    _escape_text,
    _run_ffmpeg,
    _wrap_lines,
    concat_videos,
    generate_countdown,
    generate_simple_video,
    generate_text_on_video,
    get_media_duration,
)

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not found on PATH")

# ---------------------------------------------------------------------------
# Fixtures — generate test media with ffmpeg
# ---------------------------------------------------------------------------


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Dedicated working directory for a single test."""
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def test_photo(work_dir: Path) -> Path:
    """Generate a 1080x1920 solid-colour JPEG for test input."""
    photo = work_dir / "test_photo.jpg"
    if not HAS_FFMPEG:
        return photo  # will never be used (tests skipped)
    asyncio.run(
        _run_ffmpeg([
            shutil.which("ffmpeg"), "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=1080x1920:d=1",
            "-frames:v", "1",
            str(photo),
        ], timeout=30.0)
    )
    return photo


@pytest.fixture
def test_audio(work_dir: Path) -> Path:
    """Generate a 5-second sine-wave WAV for test input."""
    audio = work_dir / "test_audio.wav"
    if not HAS_FFMPEG:
        return audio
    asyncio.run(
        _run_ffmpeg([
            shutil.which("ffmpeg"), "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5:sample_rate=44100",
            str(audio),
        ], timeout=30.0)
    )
    return audio


@pytest.fixture
def test_video(work_dir: Path) -> Path:
    """Generate a 3-second 1080x1920 MP4 with colour + silent audio."""
    video = work_dir / "test_video.mp4"
    if not HAS_FFMPEG:
        return video
    asyncio.run(
        _run_ffmpeg([
            shutil.which("ffmpeg"), "-y",
            "-f", "lavfi", "-i", "color=c=red:s=1080x1920:d=3:r=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3:sample_rate=44100",
            "-c:v", "libx264", "-crf", "28",
            "-c:a", "aac", "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-t", "3",
            str(video),
        ], timeout=30.0)
    )
    return video


# ---------------------------------------------------------------------------
# Pure-function tests (no ffmpeg needed)
# ---------------------------------------------------------------------------


class TestEscapeText:
    def test_plain_ascii(self) -> None:
        assert _escape_text("hello world") == "hello world"

    def test_colon(self) -> None:
        assert _escape_text("time: 12:30") == "time\\: 12\\:30"

    def test_semicolon(self) -> None:
        assert _escape_text("a;b") == "a\\;b"

    def test_single_quote_replaced(self) -> None:
        result = _escape_text("it's fine")
        assert "'" not in result
        assert "\u2019" in result  # typographic apostrophe

    def test_brackets(self) -> None:
        result = _escape_text("[tag]")
        assert result == "\\[tag\\]"

    def test_percent(self) -> None:
        assert _escape_text("50%") == "50%%"

    def test_backslash(self) -> None:
        result = _escape_text("a\\b")
        assert result.startswith("a")
        assert "b" in result

    def test_newline_to_space(self) -> None:
        assert _escape_text("line1\nline2") == "line1 line2"

    def test_cyrillic_preserved(self) -> None:
        text = "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440"  # "Привет мир"
        assert _escape_text(text) == text

    def test_mixed_cyrillic_special(self) -> None:
        result = _escape_text("\u041c\u0438\u0440: \u043b\u044e\u0431\u043e\u0432\u044c")
        assert "\\:" in result
        assert "\u041c\u0438\u0440" in result

    def test_comma(self) -> None:
        assert _escape_text("a,b") == "a\\,b"

    def test_empty(self) -> None:
        assert _escape_text("") == ""


class TestWrapLines:
    def test_short_line(self) -> None:
        assert _wrap_lines(["hi"]) == ["hi"]

    def test_long_line_wraps(self) -> None:
        long = "This is a very long sentence that should wrap"
        result = _wrap_lines([long], max_chars=20)
        assert len(result) > 1
        for line in result:
            assert len(line) <= 20

    def test_multiple_input_lines(self) -> None:
        result = _wrap_lines(["short", "also short"])
        assert result == ["short", "also short"]

    def test_empty_line(self) -> None:
        result = _wrap_lines([""])
        assert result == [""]

    def test_preserves_order(self) -> None:
        result = _wrap_lines(["AAA", "BBB", "CCC"])
        assert result == ["AAA", "BBB", "CCC"]


class TestVidBaitLayout:
    def test_layout_uses_actual_source_frame_for_720p_vertical_clip(self) -> None:
        """A 720x1280 source must not position captions from a 1920px frame."""
        layout = video_gen._resolve_vid_bait_text_layout(
            width=720,
            height=1280,
            requested_font_size=52,
            requested_text_y=None,
        )

        assert layout.font_size == 35
        assert layout.text_y == 832
        assert layout.max_line_chars >= 28

    def test_drawtext_block_is_clamped_inside_actual_frame(self) -> None:
        chain = video_gen._build_drawtext_chain(
            ["POV: Sunday morning. We", "didn't get out of bed until"],
            font_path=None,
            font_size=52,
            text_color="white",
            outline_width=3,
            outline_color="black",
            text_y=1248,
            frame_height=1280,
        )

        assert ":y=1248" not in chain
        assert ":y=1280" not in chain


# ---------------------------------------------------------------------------
# ffmpeg integration tests
# ---------------------------------------------------------------------------


@needs_ffmpeg
class TestGetMediaDuration:
    async def test_audio_duration(self, test_audio: Path) -> None:
        dur = await get_media_duration(test_audio)
        assert 4.5 < dur < 5.5  # ~5 seconds

    async def test_video_duration(self, test_video: Path) -> None:
        dur = await get_media_duration(test_video)
        assert 2.5 < dur < 3.5  # ~3 seconds

    async def test_missing_file(self, work_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            await get_media_duration(work_dir / "nonexistent.mp4")


@needs_ffmpeg
class TestGenerateSimpleVideo:
    async def test_basic(
        self, test_photo: Path, test_audio: Path, work_dir: Path,
    ) -> None:
        out = work_dir / "simple.mp4"
        result = await generate_simple_video(
            photo_path=test_photo,
            audio_path=test_audio,
            output_path=out,
            text_lines=["Hello World", "Line Two"],
            duration=3.0,
            fps=15,
            crf=28,
        )
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1000

        dur = await get_media_duration(out)
        assert 2.5 < dur < 3.5

    async def test_no_font(
        self, test_photo: Path, test_audio: Path, work_dir: Path,
    ) -> None:
        """Works without an explicit font path (uses ffmpeg default)."""
        out = work_dir / "no_font.mp4"
        result = await generate_simple_video(
            photo_path=test_photo,
            audio_path=test_audio,
            output_path=out,
            text_lines=["No font specified"],
            duration=2.0,
            fps=15,
            crf=28,
        )
        assert result == out
        assert out.exists()

    async def test_empty_text(
        self, test_photo: Path, test_audio: Path, work_dir: Path,
    ) -> None:
        """Empty text lines should still produce a video (no overlay)."""
        out = work_dir / "empty_text.mp4"
        result = await generate_simple_video(
            photo_path=test_photo,
            audio_path=test_audio,
            output_path=out,
            text_lines=[],
            duration=2.0,
            fps=15,
            crf=28,
        )
        assert out.exists()

    async def test_missing_photo(
        self, test_audio: Path, work_dir: Path,
    ) -> None:
        with pytest.raises(FileNotFoundError, match="Photo not found"):
            await generate_simple_video(
                photo_path=work_dir / "nope.jpg",
                audio_path=test_audio,
                output_path=work_dir / "out.mp4",
                text_lines=["test"],
            )

    async def test_missing_audio(
        self, test_photo: Path, work_dir: Path,
    ) -> None:
        with pytest.raises(FileNotFoundError, match="Audio not found"):
            await generate_simple_video(
                photo_path=test_photo,
                audio_path=work_dir / "nope.wav",
                output_path=work_dir / "out.mp4",
                text_lines=["test"],
            )

    async def test_multiline_wrapping(
        self, test_photo: Path, test_audio: Path, work_dir: Path,
    ) -> None:
        """Long text that must be word-wrapped still produces valid output."""
        out = work_dir / "multiline.mp4"
        result = await generate_simple_video(
            photo_path=test_photo,
            audio_path=test_audio,
            output_path=out,
            text_lines=[
                "This is a really long line of text that will need wrapping",
                "Second long line that also should be wrapped properly",
            ],
            duration=2.0,
            fps=15,
            crf=28,
        )
        assert out.exists()
        assert out.stat().st_size > 1000

    async def test_cyrillic_text(
        self, test_photo: Path, test_audio: Path, work_dir: Path,
    ) -> None:
        out = work_dir / "cyrillic.mp4"
        result = await generate_simple_video(
            photo_path=test_photo,
            audio_path=test_audio,
            output_path=out,
            text_lines=["\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440", "\u041b\u044e\u0431\u043e\u0432\u044c: \u0432\u0435\u0447\u043d\u0430"],
            duration=2.0,
            fps=15,
            crf=28,
        )
        assert out.exists()


@needs_ffmpeg
class TestGenerateTextOnVideo:
    async def test_basic(self, test_video: Path, work_dir: Path) -> None:
        out = work_dir / "text_overlay.mp4"
        result = await generate_text_on_video(
            video_path=test_video,
            output_path=out,
            text_lines=["Overlay Text"],
        )
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1000

    async def test_empty_text_copies(self, test_video: Path, work_dir: Path) -> None:
        """Empty text should just copy the video."""
        out = work_dir / "copy.mp4"
        await generate_text_on_video(
            video_path=test_video,
            output_path=out,
            text_lines=[],
        )
        assert out.exists()

    async def test_missing_video(self, work_dir: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Video not found"):
            await generate_text_on_video(
                video_path=work_dir / "missing.mp4",
                output_path=work_dir / "out.mp4",
                text_lines=["text"],
            )

    async def test_multiline(self, test_video: Path, work_dir: Path) -> None:
        out = work_dir / "multi.mp4"
        await generate_text_on_video(
            video_path=test_video,
            output_path=out,
            text_lines=[
                "Line A is quite long and should wrap",
                "Line B",
                "Line C",
            ],
        )
        assert out.exists()
        assert out.stat().st_size > 1000


@needs_ffmpeg
class TestGenerateCountdown:
    async def test_basic(self, work_dir: Path) -> None:
        out = work_dir / "countdown.mp4"
        result = await generate_countdown(output_path=out, duration=3.0, fps=15)
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1000

        dur = await get_media_duration(out)
        assert 2.5 < dur < 3.5

    async def test_custom_colours(self, work_dir: Path) -> None:
        out = work_dir / "countdown_custom.mp4"
        await generate_countdown(
            output_path=out,
            duration=3.0,
            fps=15,
            bg_color="#ff0000",
            text_color="yellow",
            font_size=150,
        )
        assert out.exists()


@needs_ffmpeg
class TestConcatVideos:
    async def test_two_segments(self, work_dir: Path) -> None:
        # Create two short countdown segments
        seg1 = work_dir / "seg1.mp4"
        seg2 = work_dir / "seg2.mp4"
        await generate_countdown(output_path=seg1, duration=2.0, fps=15)
        await generate_countdown(output_path=seg2, duration=2.0, fps=15)

        out = work_dir / "concat.mp4"
        result = await concat_videos([seg1, seg2], output_path=out)
        assert result == out
        assert out.exists()

        dur = await get_media_duration(out)
        assert 3.5 < dur < 4.5  # ~4 seconds total

    async def test_single_segment(self, work_dir: Path) -> None:
        seg = work_dir / "only.mp4"
        await generate_countdown(output_path=seg, duration=2.0, fps=15)

        out = work_dir / "single.mp4"
        await concat_videos([seg], output_path=out)
        assert out.exists()

    async def test_empty_list(self, work_dir: Path) -> None:
        with pytest.raises(ValueError, match="No segments"):
            await concat_videos([], output_path=work_dir / "out.mp4")

    async def test_missing_segment(self, work_dir: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Segment not found"):
            await concat_videos(
                [work_dir / "nonexistent.mp4"],
                output_path=work_dir / "out.mp4",
            )

    async def test_cleanup_list_file(self, work_dir: Path) -> None:
        """The temporary concat list file is cleaned up after use."""
        seg = work_dir / "seg.mp4"
        await generate_countdown(output_path=seg, duration=1.0, fps=15)

        out = work_dir / "concat_cleanup.mp4"
        await concat_videos([seg], output_path=out)

        # The list file should have been deleted
        list_files = list(work_dir.glob("_concat_*.txt"))
        assert len(list_files) == 0


@needs_ffmpeg
class TestRunFfmpeg:
    async def test_bad_command(self) -> None:
        """Invalid ffmpeg arguments return a non-zero exit code."""
        rc, stdout, stderr = await _run_ffmpeg(
            [shutil.which("ffmpeg"), "-v", "error", "-i", "nonexistent_input.xyz", "-f", "null", "-"],
            timeout=10.0,
        )
        assert rc != 0

    async def test_timeout(self, work_dir: Path) -> None:
        """A command that exceeds the timeout raises TimeoutError."""
        # Generate a very long colour source but with an impossibly short timeout
        with pytest.raises(TimeoutError):
            await _run_ffmpeg(
                [
                    shutil.which("ffmpeg"), "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=999:r=30",
                    "-c:v", "libx264",
                    "-pix_fmt", "yuv420p",
                    str(work_dir / "never_finishes.mp4"),
                ],
                timeout=0.5,
            )

    async def test_success(self, work_dir: Path) -> None:
        """A valid simple command returns rc=0."""
        out = work_dir / "tiny.mp4"
        rc, stdout, stderr = await _run_ffmpeg(
            [
                shutil.which("ffmpeg"), "-y",
                "-f", "lavfi", "-i", "color=c=green:s=64x64:d=0.1:r=10",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                str(out),
            ],
            timeout=15.0,
        )
        assert rc == 0
        assert out.exists()
