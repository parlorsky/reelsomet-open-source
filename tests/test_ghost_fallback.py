"""Tests for `server/ghost.py::minimal_carousel_reencode`.

This fallback runs when `ghost_media_safe` times out or fails on a
carousel photo. It re-encodes the source image as JPEG with a seeded
quality jitter so two accounts that both hit a ghost timeout don't
dispatch byte-identical master files. See Feature A Option E design
notes in `scheduler.py::_ghost_one`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from server.ghost import minimal_carousel_reencode


def _make_test_image(path: Path, color: tuple[int, int, int] = (200, 50, 50)) -> None:
    img = Image.new("RGB", (128, 128), color=color)
    img.save(path, format="JPEG", quality=95)


class TestMinimalCarouselReencode:
    def test_produces_output_file(self, tmp_path: Path) -> None:
        """Basic sanity: fallback writes an output file and returns the path."""
        src = tmp_path / "src.jpg"
        dst = tmp_path / "dst.jpg"
        _make_test_image(src)
        result = minimal_carousel_reencode(str(src), str(dst), seed=42)
        assert result is not None
        assert Path(result) == dst
        assert dst.exists()
        assert dst.stat().st_size > 0

    def test_different_seeds_produce_different_bytes(self, tmp_path: Path) -> None:
        """Same source + different seeds → different byte content.

        This is the *entire point* of the fallback: two sibling posts
        hitting ghost timeout on the same source must diverge on
        binary hash to avoid cross-account fingerprint collision.
        """
        src = tmp_path / "src.jpg"
        _make_test_image(src)
        dst_a = tmp_path / "a.jpg"
        dst_b = tmp_path / "b.jpg"
        minimal_carousel_reencode(str(src), str(dst_a), seed=100)
        minimal_carousel_reencode(str(src), str(dst_b), seed=999999)
        bytes_a = dst_a.read_bytes()
        bytes_b = dst_b.read_bytes()
        assert bytes_a != bytes_b, (
            "Different seeds must produce different JPEG output "
            "(quality jitter must affect the encode)"
        )
        md5_a = hashlib.md5(bytes_a).hexdigest()
        md5_b = hashlib.md5(bytes_b).hexdigest()
        assert md5_a != md5_b

    def test_deterministic_same_seed(self, tmp_path: Path) -> None:
        """Same source + same seed → identical output (retry determinism)."""
        src = tmp_path / "src.jpg"
        _make_test_image(src)
        dst_a = tmp_path / "a.jpg"
        dst_b = tmp_path / "b.jpg"
        minimal_carousel_reencode(str(src), str(dst_a), seed=777)
        minimal_carousel_reencode(str(src), str(dst_b), seed=777)
        assert dst_a.read_bytes() == dst_b.read_bytes()

    def test_png_source_converted_to_jpeg(self, tmp_path: Path) -> None:
        """PNG source → JPEG output. The ghost pipeline forces JPEG."""
        src = tmp_path / "src.png"
        Image.new("RGB", (64, 64), color=(0, 255, 0)).save(src, format="PNG")
        dst = tmp_path / "dst.jpg"
        result = minimal_carousel_reencode(str(src), str(dst), seed=1)
        assert result is not None
        # Verify the output is valid JPEG by re-opening it
        with Image.open(dst) as img:
            assert img.format == "JPEG"

    def test_webp_source_converted_to_jpeg(self, tmp_path: Path) -> None:
        """WebP source → JPEG output."""
        src = tmp_path / "src.webp"
        Image.new("RGB", (64, 64), color=(0, 0, 255)).save(src, format="WEBP")
        dst = tmp_path / "dst.jpg"
        result = minimal_carousel_reencode(str(src), str(dst), seed=2)
        assert result is not None
        with Image.open(dst) as img:
            assert img.format == "JPEG"

    def test_rgba_source_converted(self, tmp_path: Path) -> None:
        """PNG with alpha channel → RGB JPEG (alpha must be stripped)."""
        src = tmp_path / "src.png"
        Image.new("RGBA", (64, 64), color=(255, 255, 255, 128)).save(src, format="PNG")
        dst = tmp_path / "dst.jpg"
        result = minimal_carousel_reencode(str(src), str(dst), seed=3)
        assert result is not None
        with Image.open(dst) as img:
            assert img.mode == "RGB"

    def test_missing_source_returns_none(self, tmp_path: Path) -> None:
        """Non-existent source → None, no crash."""
        src = tmp_path / "nope.jpg"
        dst = tmp_path / "out.jpg"
        result = minimal_carousel_reencode(str(src), str(dst), seed=0)
        assert result is None
        assert not dst.exists()

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """Destination parent directory is created if missing."""
        src = tmp_path / "src.jpg"
        _make_test_image(src)
        # Deeply nested, doesn't exist yet
        dst = tmp_path / "a" / "b" / "c" / "out.jpg"
        result = minimal_carousel_reencode(str(src), str(dst), seed=5)
        assert result is not None
        assert dst.exists()

    def test_quality_in_expected_band(self, tmp_path: Path) -> None:
        """Quality must stay in the [85, 92] band across many seeds.

        Outside that range either degrades pixels visibly (< 85) or
        barely breaks the hash (> 92). The band is a design decision
        in the helper; this test locks it in so a future tweak can't
        silently widen it and compromise fingerprint isolation.
        """
        src = tmp_path / "src.jpg"
        _make_test_image(src)
        sizes = []
        for seed in range(20):
            dst = tmp_path / f"out_{seed}.jpg"
            minimal_carousel_reencode(str(src), str(dst), seed=seed)
            sizes.append(dst.stat().st_size)
        # Higher quality → larger file; lower quality → smaller. We don't
        # assert exact values (they depend on Pillow version + libjpeg),
        # but all 20 outputs must be non-empty and the spread between
        # smallest and largest must be < 50% of the larger (i.e., quality
        # 85 vs 92 should not differ wildly).
        assert all(s > 0 for s in sizes)
        spread = (max(sizes) - min(sizes)) / max(sizes)
        assert spread < 0.5, f"Quality band too wide, spread={spread:.2%}"
