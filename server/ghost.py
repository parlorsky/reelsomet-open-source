"""Ghost pipeline wrapper — runs ghostcli on media before dispatch.

Every media file MUST be processed through ghostcli before being sent to
the phone. This produces unique pixel/hash fingerprints per posting, preventing
Instagram from detecting duplicate content across accounts.

Key rule: ghost PER POSTING, not per file. The same source photo used in
two carousels gets ghosted separately each time.

Note on compare_video hang safety: bounded sampling (max frames + wall-clock
budget) lives upstream in ghostcli.similarity.compare_video. The outer
scheduler additionally wraps every ghost call in asyncio.wait_for so a
genuinely stuck cv2.VideoCapture.read (which no Python-level timeout can
interrupt) can be abandoned — the worker thread leaks but the scheduler
stays healthy.
"""
from __future__ import annotations

import logging
import os
import random
import shutil
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def _atomic_replace(src: Path, dst: Path) -> None:
    """Move src into dst, atomic when possible.

    1. Try os.replace() — atomic on same filesystem.
    2. On EXDEV (cross-device), stage a sibling on the destination filesystem
       and finish with os.replace() so the visible swap stays atomic.
    3. Never overwrite the live destination file with a partial copy: if both
       attempts above fail, raise — callers can then decide whether to skip
       ghosting (safer) or surface the error.

    Refusing to do a non-atomic copy here is intentional: the live file may be
    served by the download API while we're writing, and a torn read on the
    phone would dispatch a corrupt asset.
    """
    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(src), str(dst))
        return
    except OSError:
        pass  # cross-device or other — fall through

    staging = dst.parent / f".{dst.name}.ghost-{uuid.uuid4().hex[:12]}"
    try:
        shutil.copy2(str(src), str(staging))
        os.replace(str(staging), str(dst))
        try:
            Path(src).unlink()
        except Exception:
            pass
        return
    except Exception:
        if staging.exists():
            try:
                staging.unlink()
            except Exception:
                pass
        # Re-raise so ghost_media_safe returns None and the dispatcher
        # logs the failure instead of silently corrupting the live file.
        raise


def ghost_media(
    input_path: str | Path,
    output_path: str | Path | None = None,
    ssim_threshold: float = 0.90,
    shared_meta_seed: int | None = None,
) -> Path:
    """Ghost a single media file. Returns path to ghosted output.

    If output_path is None, replaces input file in-place (via temp dir + atomic rename).
    Raises RuntimeError on failure.

    `shared_meta_seed` forces the metadata stage (iPhone donor row pick) to
    use a caller-supplied root seed so multiple files in the same logical
    post (carousel) all get the same iPhone donor — real iPhone carousels
    are always one device, not four.
    """
    from ghostcli.pipeline import ghost_file

    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Ghost input not found: {input_path}")

    seed = random.randint(0, 2**31)

    if output_path is None:
        # Ghost in-place: ghost to temp, then atomic-replace the original so
        # concurrent readers (the download API) cannot observe a torn state.
        with tempfile.TemporaryDirectory(prefix="ghost_") as tmp:
            result = ghost_file(
                input_path, Path(tmp), seed, ssim_threshold,
                shared_meta_seed=shared_meta_seed,
            )
            _atomic_replace(Path(result), input_path)
            logger.info("Ghosted in-place: %s (seed=%d)", input_path.name, seed)
            return input_path
    else:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = ghost_file(
            input_path, output_path.parent, seed, ssim_threshold,
            shared_meta_seed=shared_meta_seed,
        )
        # Rename to requested output name if different — atomic
        if result != output_path:
            _atomic_replace(Path(result), output_path)
        logger.info(
            "Ghosted: %s -> %s (seed=%d)",
            input_path.name, output_path.name, seed,
        )
        return output_path


def ghost_media_safe(
    input_path: str | Path,
    output_path: str | Path | None = None,
    ssim_threshold: float = 0.90,
    shared_meta_seed: int | None = None,
) -> Path | None:
    """Like ghost_media but returns None on failure instead of raising."""
    try:
        return ghost_media(input_path, output_path, ssim_threshold, shared_meta_seed)
    except Exception:
        logger.exception("Ghost failed for %s", input_path)
        return None


def minimal_carousel_reencode(
    input_path: str | Path,
    output_path: str | Path,
    seed: int,
) -> Path | None:
    """Last-ditch fallback for when ghost_media_safe times out or fails on
    a carousel photo.

    Runs a seeded Pillow JPEG re-encode with a per-account quality jitter so
    two different accounts that both hit a ghost timeout don't end up
    dispatching the same byte-identical master file from carousel_sets/.
    The output has no forged iPhone metadata (that requires the full ghost
    pipeline), but its binary hash, JPEG DCT coefficients, and file size
    will differ from both the master and from any other account's fallback.

    Returns the output Path on success, None on failure.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not available for minimal re-encode fallback")
        return None

    input_path = Path(input_path)
    output_path = Path(output_path)

    try:
        with Image.open(input_path) as img:
            # Strip alpha / handle PNG/WebP sources — carousel dispatch is JPEG-only.
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")

            rng = random.Random(seed)
            # Narrow quality band — imperceptible visually, but large enough
            # that two different seeds produce different byte streams from
            # the same source pixels.
            quality = rng.randint(85, 92)
            subsampling = rng.choice([0, 1, 2])  # 4:4:4, 4:2:2, 4:2:0

            output_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(
                output_path,
                format="JPEG",
                quality=quality,
                subsampling=subsampling,
                optimize=True,
            )
        logger.info(
            "Minimal re-encode fallback: %s -> %s (quality=%d, subsample=%d)",
            input_path.name, output_path.name, quality, subsampling,
        )
        return output_path
    except Exception:
        logger.exception("Minimal re-encode failed for %s", input_path)
        return None
