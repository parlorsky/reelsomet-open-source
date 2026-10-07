"""Tracks API: CRUD for music tracks (catalog.json + audio files)."""
from __future__ import annotations

import json
import logging
import asyncio
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.config import VPSConfig
from server.dependencies import get_config, require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tracks", tags=["tracks"])

_CATALOG_FILENAME = "catalog.json"
_ALLOWED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".ogg"}

# Per-process lock for the load-modify-save catalog cycle (Codex
# iter 9 Q2 2026-04-14). Without it, two concurrent uploads or
# patches both load the same state, both modify their copy, and
# the second `_save_catalog_async` call clobbers the first
# write — losing one update silently. The lock serialises just
# the mutation window, leaving reads (which don't write) free
# to run concurrently.
_catalog_mutate_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TrackMetadataUpdate(BaseModel):
    type: str | None = None
    intro_duration: float | None = None
    beat_interval: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tracks_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "tracks"


def _catalog_path(config: VPSConfig) -> Path:
    return _tracks_dir(config) / _CATALOG_FILENAME


def _load_catalog(config: VPSConfig) -> dict[str, dict[str, Any]]:
    """Load track catalog from disk. Returns empty dict if file missing."""
    path = _catalog_path(config)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _save_catalog(config: VPSConfig, catalog: dict[str, dict[str, Any]]) -> None:
    """Persist track catalog to disk atomically.

    Write-then-rename pattern with a UNIQUE per-call tmp path so
    two concurrent saves can't trample the same staging file
    (Codex iter 9 Q2 2026-04-14). Combined with the upstream
    `_catalog_mutate_lock`, this gives both atomic visibility AND
    last-write-wins consistency without requiring a global file
    lock.
    """
    import os as _os
    import uuid as _uuid
    path = _catalog_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique suffix per save → no shared tmp filename collisions
    tmp_path = path.with_suffix(f"{path.suffix}.tmp-{_uuid.uuid4().hex[:8]}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(catalog, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                _os.fsync(f.fileno())
            except OSError:
                pass
        _os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup on failure so we don't leave orphan
        # tmp files in the data dir.
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise


async def _save_catalog_async(
    config: VPSConfig, catalog: dict[str, dict[str, Any]],
) -> None:
    """Async wrapper for _save_catalog — offloads the JSON dump +
    fsync to a worker thread so async upload/delete handlers don't
    block the event loop on disk I/O.
    """
    await asyncio.to_thread(_save_catalog, config, catalog)


def _validate_filename(filename: str) -> None:
    """Reject path-traversal attempts."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename",
        )


def _track_to_response(
    filename: str, meta: dict[str, Any], *, exists: bool = True,
) -> dict[str, Any]:
    """Build a track response dict matching the frontend Track type."""
    return {
        "filename": filename,
        "type": meta.get("type", "simple"),
        "duration": meta.get("duration", 0),
        "used_count": meta.get("used_count", 0),
        "intro_duration": meta.get("intro_duration"),
        "beat_interval": meta.get("beat_interval"),
        "exists": exists,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_tracks(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """List all tracks from catalog.json, enriched with file existence."""
    catalog = _load_catalog(config)
    tracks_path = _tracks_dir(config)
    result = []
    for filename, meta in catalog.items():
        file_exists = (tracks_path / filename).is_file()
        result.append(_track_to_response(filename, meta, exists=file_exists))
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_track(
    file: UploadFile = File(...),
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Upload an audio file and add it to the catalog."""
    raw_name = file.filename or "track.wav"
    safe_name = Path(raw_name).name
    _validate_filename(safe_name)

    ext = Path(safe_name).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type '{ext}' not allowed. Accepted: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    # Save file to disk. Wrap the blocking shutil.copyfileobj in
    # `asyncio.to_thread` so large uploads don't freeze the event
    # loop while bytes stream from network buffer to disk (Codex
    # iter 8 bug hunt 2026-04-14).
    dest_dir = _tracks_dir(config)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    def _write_blob(src, dst_path):
        with open(dst_path, "wb") as f:
            shutil.copyfileobj(src, f)

    await asyncio.to_thread(_write_blob, file.file, dest_path)

    # Probe duration via ffprobe (non-fatal)
    duration = 0.0
    try:
        from server.video_gen import get_media_duration
        duration = await get_media_duration(dest_path)
    except Exception as exc:
        logger.warning("Failed to probe duration for %s: %s", safe_name, exc)

    # Update catalog under a per-process lock so concurrent uploads
    # don't load-modify-save with stale state and lose entries
    # (Codex iter 9 Q2 2026-04-14).
    async with _catalog_mutate_lock:
        catalog = _load_catalog(config)
        catalog[safe_name] = {
            "type": "simple",
            "duration": duration,
            "intro_duration": None,
            "beat_interval": None,
            "used_count": 0,
        }
        await _save_catalog_async(config, catalog)

    return _track_to_response(safe_name, catalog[safe_name])


@router.delete("/{filename}")
async def delete_track(
    filename: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, str]:
    """Remove a track file and its catalog entry."""
    _validate_filename(filename)

    async with _catalog_mutate_lock:
        catalog = _load_catalog(config)
        if filename not in catalog:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Track '{filename}' not found in catalog",
            )

        # Remove from catalog
        del catalog[filename]
        await _save_catalog_async(config, catalog)

    # Remove file (best-effort) — outside the lock since it doesn't
    # touch the catalog.
    file_path = _tracks_dir(config) / filename
    if file_path.is_file():
        file_path.unlink()

    return {"status": "deleted"}


@router.patch("/{filename}")
async def update_track_metadata(
    filename: str,
    body: TrackMetadataUpdate,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Update metadata fields for an existing track."""
    _validate_filename(filename)

    async with _catalog_mutate_lock:
        catalog = _load_catalog(config)
        if filename not in catalog:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Track '{filename}' not found in catalog",
            )

        meta = catalog[filename]
        fields_set = body.model_fields_set
        if "type" in fields_set and body.type is not None:
            meta["type"] = body.type
        if "intro_duration" in fields_set:
            meta["intro_duration"] = body.intro_duration
        if "beat_interval" in fields_set:
            meta["beat_interval"] = body.beat_interval

        await _save_catalog_async(config, catalog)
    return _track_to_response(filename, meta)


@router.get("/{filename}/audio")
async def stream_audio(
    filename: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> FileResponse:
    """Stream an audio file for preview."""
    _validate_filename(filename)

    file_path = _tracks_dir(config) / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audio file '{filename}' not found",
        )

    # Determine media type from extension
    ext = file_path.suffix.lower()
    media_types = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".ogg": "audio/ogg",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        filename=filename,
    )
