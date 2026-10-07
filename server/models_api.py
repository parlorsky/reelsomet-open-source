"""Models API: CRUD for photo model directories and their images."""
from __future__ import annotations

import base64
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from server.config import VPSConfig
from server.dependencies import get_config, require_auth
from server.llm_client import PROVIDER_DEFAULTS

logger = logging.getLogger(__name__)

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}

router = APIRouter(prefix="/api/models", tags=["models"])

_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm", ".avi", ".mkv"}
_KNOWN_FOLDERS = ("vid_bait",)
_PROFILE_FILENAME = "profile.json"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ProfileUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ModelCreate(BaseModel):
    name: str
    description: str | None = None


class PauseToggle(BaseModel):
    paused: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _models_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "models"


def _validate_name(name: str) -> None:
    """Reject path-traversal attempts in model/folder/file names."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name: path traversal not allowed",
        )


def _validate_model_folder(folder: str) -> None:
    """Allow only product-supported model media folders."""
    _validate_name(folder)
    if folder not in _KNOWN_FOLDERS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model folder '{folder}' is not supported",
        )


def _load_profile(model_dir: Path) -> dict[str, Any]:
    """Load profile.json from a model directory."""
    profile_path = model_dir / _PROFILE_FILENAME
    if not profile_path.exists():
        return {}
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_profile(model_dir: Path, profile: dict[str, Any]) -> None:
    """Persist profile.json to a model directory."""
    profile_path = model_dir / _PROFILE_FILENAME
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def _remove_catalog_entry(catalog_path: Path, folder: str, filename: str) -> None:
    """Remove a file's entry from photo_catalog.json if present."""
    if not catalog_path.is_file():
        return
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, list):
        return

    filtered = [
        entry for entry in raw
        if not (entry.get("folder") == folder and entry.get("filename") == filename)
    ]
    if filtered == raw:
        return
    catalog_path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")


def _count_photos(folder_path: Path) -> int:
    """Count image/video files in a folder (excludes hidden .thumb_* files)."""
    if not folder_path.is_dir():
        return 0
    return sum(
        1 for f in folder_path.iterdir()
        if f.is_file() and f.suffix.lower() in _ALLOWED_EXTENSIONS
        and not f.name.startswith(".thumb_")
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
async def list_models(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """List model directories with folder counts."""
    base = _models_dir(config)
    if not base.is_dir():
        return []

    result = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        profile = _load_profile(entry)
        folders = []
        for folder_name in _KNOWN_FOLDERS:
            folder_path = entry / folder_name
            folders.append({
                "name": folder_name,
                "photo_count": _count_photos(folder_path),
            })
        result.append({
            "name": entry.name,
            "description": profile.get("description", ""),
            "folders": folders,
        })
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelCreate,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Create a new model directory + pre-create the supported media folder.

    A model is just a directory under `data_dir/models/<name>/`. We seed
    the active `_KNOWN_FOLDERS` entry (`vid_bait`) and write a
    `profile.json` with the optional description.

    Returns the freshly-created entry in the same shape `list_models`
    uses, so the frontend can splice it into the model list without a
    full refetch.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model name is required",
        )
    _validate_name(name)
    if not all(c.isalnum() or c in "_-." for c in name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model name must contain only letters, digits, '_', '-', '.'",
        )
    if len(name) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Model name must be ≤ 64 characters",
        )

    base = _models_dir(config)
    base.mkdir(parents=True, exist_ok=True)
    model_dir = base / name
    if model_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model '{name}' already exists",
        )
    try:
        model_dir.mkdir(parents=True, exist_ok=False)
        for folder_name in _KNOWN_FOLDERS:
            (model_dir / folder_name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create model directory: {exc}",
        ) from exc

    profile: dict[str, Any] = {}
    if body.description:
        profile["description"] = body.description
    if profile:
        try:
            _save_profile(model_dir, profile)
        except OSError:
            # Directory was created — model still usable. Profile is best-effort.
            pass

    return {
        "name": name,
        "description": profile.get("description", ""),
        "folders": [
            {"name": f, "photo_count": 0} for f in _KNOWN_FOLDERS
        ],
    }


@router.get("/{name}/photos/{folder}")
async def list_photos(
    name: str,
    folder: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """List photos in a model's folder with URLs."""
    _validate_name(name)
    _validate_model_folder(folder)

    folder_path = _models_dir(config) / name / folder
    if not folder_path.is_dir():
        return []

    # Load photo_catalog.json for metadata
    catalog: dict[str, dict[str, Any]] = {}
    catalog_path = _models_dir(config) / name / "photo_catalog.json"
    if catalog_path.is_file():
        try:
            raw = json.loads(catalog_path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else []
            for entry in entries:
                fn = entry.get("filename", "")
                if entry.get("folder") == folder and fn:
                    catalog[fn] = entry
        except Exception:
            pass

    result = []
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in _ALLOWED_EXTENSIONS:
            meta = catalog.get(f.name, {})
            # Use thumbnail if available, otherwise direct URL
            thumb_name = f".thumb_{f.stem}.jpg"
            thumb_path = folder_path / thumb_name
            thumb_url = f"/api/models/{name}/photos/{folder}/{thumb_name}" if thumb_path.is_file() else None

            # Determine media type from extension (catalog may be stale)
            file_ext = f.suffix.lower()
            detected_type = "video" if file_ext in _VIDEO_EXTENSIONS else "image"
            catalog_type = meta.get("media_type")
            media_type = catalog_type if catalog_type else detected_type

            item: dict[str, Any] = {
                "filename": f.name,
                "url": f"/api/models/{name}/photos/{folder}/{f.name}",
                "thumb_url": thumb_url or f"/api/models/{name}/photos/{folder}/{f.name}",
                "description": meta.get("description", ""),
                "tags": meta.get("tags", []),
                "media_type": media_type,
                "duration_seconds": meta.get("duration_seconds"),
                "has_original_audio": meta.get("has_original_audio"),
                "paused": bool(meta.get("paused", False)),
            }
            result.append(item)
    return result


@router.get("/{name}/photos/{folder}/{filename}")
async def serve_photo(
    name: str,
    folder: str,
    filename: str,
    config: VPSConfig = Depends(get_config),
) -> FileResponse:
    """Serve a photo file directly (no auth for image embedding)."""
    _validate_name(name)
    _validate_model_folder(folder)
    _validate_name(filename)

    file_path = _models_dir(config) / name / folder / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo '{filename}' not found",
        )

    ext = file_path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mkv": "video/x-matroska",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(path=str(file_path), media_type=media_type)


@router.post("/{name}/photos/{folder}", status_code=status.HTTP_201_CREATED)
async def upload_photo(
    name: str,
    folder: str,
    file: UploadFile = File(...),
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Upload a photo to a model's folder."""
    _validate_name(name)
    _validate_model_folder(folder)

    raw_name = file.filename or "photo.jpg"
    safe_name = Path(raw_name).name
    _validate_name(safe_name)

    ext = Path(safe_name).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"File type '{ext}' not allowed. Accepted: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    dest_dir = _models_dir(config) / name / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name

    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    thumb_url = None
    # Generate thumbnail for video files using ffmpeg. Offload the
    # sync subprocess to a worker thread (Codex iter 8 bug hunt
    # 2026-04-14). Blocking the event loop for 15 s per upload
    # freezes every other API request on the same worker.
    if ext in _VIDEO_EXTENSIONS:
        thumb_name = f".thumb_{dest_path.stem}.jpg"
        thumb_path = dest_dir / thumb_name
        try:
            import asyncio as _asyncio
            await _asyncio.to_thread(
                subprocess.run,
                [
                    "ffmpeg", "-y", "-i", str(dest_path),
                    "-ss", "1", "-frames:v", "1",
                    "-vf", "scale=320:-2",
                    str(thumb_path),
                ],
                capture_output=True, timeout=15,
            )
            if thumb_path.is_file():
                thumb_url = f"/api/models/{name}/photos/{folder}/{thumb_name}"
                logger.info("Thumbnail generated for %s", safe_name)
        except Exception as exc:
            logger.warning("Failed to generate thumbnail for %s: %s", safe_name, exc)

    return {
        "filename": safe_name,
        "url": f"/api/models/{name}/photos/{folder}/{safe_name}",
        "thumb_url": thumb_url,
    }


@router.delete("/{name}/photos/{folder}/{filename}")
async def delete_photo(
    name: str,
    folder: str,
    filename: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, str]:
    """Delete a photo from a model's folder."""
    _validate_name(name)
    _validate_model_folder(folder)
    _validate_name(filename)

    file_path = _models_dir(config) / name / folder / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Photo '{filename}' not found",
        )

    # `missing_ok=True` closes the TOCTOU window between the
    # `is_file()` check above and this unlink — a concurrent delete
    # request for the same path would otherwise crash with a 500
    # (Codex iter 14 bug hunt 2026-04-14).
    file_path.unlink(missing_ok=True)
    thumb_path = file_path.parent / f".thumb_{file_path.stem}.jpg"
    thumb_path.unlink(missing_ok=True)

    catalog_path = _models_dir(config) / name / "photo_catalog.json"
    _remove_catalog_entry(catalog_path, folder, filename)
    return {"status": "deleted"}


@router.patch("/{name}/photos/{folder}/{filename}/pause")
async def set_photo_paused(
    name: str,
    folder: str,
    filename: str,
    body: PauseToggle,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Toggle the `paused` flag on a catalog entry.

    Paused items stay on disk but are excluded from the vid_bait pool
    (see scheduler.py `_maybe_generate_vid_bait`). Useful for parking a
    reference clip that's underperforming or temporarily off-brand
    without deleting it.

    Idempotent: setting paused to its current value is a no-op.
    Auto-creates the catalog entry if missing so the operator can pause
    a never-cataloged file (vid_bait files often arrive un-cataloged).
    """
    _validate_name(name)
    _validate_model_folder(folder)
    _validate_name(filename)

    file_path = _models_dir(config) / name / folder / filename
    if not file_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File '{filename}' not found",
        )

    catalog_path = _models_dir(config) / name / "photo_catalog.json"
    entries: list[dict[str, Any]] = []
    if catalog_path.is_file():
        try:
            raw = json.loads(catalog_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                entries = raw
        except Exception:
            entries = []

    found = False
    for entry in entries:
        if entry.get("folder") == folder and entry.get("filename") == filename:
            entry["paused"] = bool(body.paused)
            found = True
            break

    if not found:
        # Synthesize a minimal entry so the pause flag has somewhere to
        # live. media_type is best-effort from extension.
        ext = file_path.suffix.lower()
        media_type = "video" if ext in _VIDEO_EXTENSIONS else "image"
        entries.append({
            "filename": filename,
            "folder": folder,
            "media_type": media_type,
            "description": "",
            "tags": [],
            "paused": bool(body.paused),
        })

    catalog_path.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "set_photo_paused: %s/%s/%s -> paused=%s",
        name, folder, filename, body.paused,
    )
    return {"filename": filename, "folder": folder, "paused": bool(body.paused)}


@router.post("/{name}/recatalog")
async def recatalog_model(
    name: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Scan folders, generate missing thumbnails, rebuild photo counts."""
    _validate_name(name)

    model_dir = _models_dir(config) / name
    if not model_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{name}' not found",
        )

    thumbs_generated = 0
    folders = []
    # Recatalog can process many videos; running ffmpeg in the
    # event loop sequentially would block for minutes. Offload
    # each ffmpeg call to a worker thread (Codex iter 8 bug hunt
    # 2026-04-14).
    import asyncio as _asyncio
    for folder_name in _KNOWN_FOLDERS:
        folder_path = model_dir / folder_name
        if folder_path.is_dir():
            # Generate missing thumbnails for videos
            for f in folder_path.iterdir():
                if f.is_file() and f.suffix.lower() in _VIDEO_EXTENSIONS:
                    thumb = folder_path / f".thumb_{f.stem}.jpg"
                    if not thumb.is_file():
                        try:
                            await _asyncio.to_thread(
                                subprocess.run,
                                ["ffmpeg", "-y", "-i", str(f), "-ss", "1",
                                 "-frames:v", "1", "-vf", "scale=320:-2", str(thumb)],
                                capture_output=True, timeout=15,
                            )
                            if thumb.is_file():
                                thumbs_generated += 1
                        except Exception:
                            logger.exception(
                                "Failed to generate recatalog thumbnail for %s", f,
                            )
        folders.append({
            "name": folder_name,
            "photo_count": _count_photos(folder_path),
        })

    return {"name": name, "folders": folders, "thumbnails_generated": thumbs_generated}


@router.patch("/{name}/profile")
async def update_profile(
    name: str,
    body: ProfileUpdate,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Update the model's profile.json."""
    _validate_name(name)

    model_dir = _models_dir(config) / name
    if not model_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{name}' not found",
        )

    profile = _load_profile(model_dir)
    if body.name is not None:
        profile["name"] = body.name
    if body.description is not None:
        profile["description"] = body.description

    _save_profile(model_dir, profile)
    return profile


# ---------------------------------------------------------------------------
# Per-item catalog (LLM vision)
# ---------------------------------------------------------------------------

def _ffprobe_meta(file_path: Path) -> dict[str, Any]:
    """Extract duration and audio presence via ffprobe."""
    meta: dict[str, Any] = {"duration_seconds": None, "has_original_audio": False}
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(file_path)],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            meta["duration_seconds"] = round(float(dur), 1)
        streams = data.get("streams", [])
        meta["has_original_audio"] = any(s.get("codec_type") == "audio" for s in streams)
    except Exception:
        pass
    return meta


def _ensure_thumbnail(file_path: Path) -> Path | None:
    """Generate thumbnail if missing, return path or None."""
    thumb = file_path.parent / f".thumb_{file_path.stem}.jpg"
    if thumb.is_file():
        return thumb
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(file_path), "-ss", "1",
             "-frames:v", "1", "-vf", "scale=320:-2", str(thumb)],
            capture_output=True, timeout=15,
        )
        return thumb if thumb.is_file() else None
    except Exception:
        return None


_VISION_UNAVAILABLE_RESULT: dict[str, Any] = {
    "description": "(LLM unavailable)",
    "tags": [],
}


async def _vision_describe(config: VPSConfig, image_b64: str) -> dict[str, Any]:
    """Send image to LLM vision API for description + tags.

    Returns a placeholder ``{"description": "(LLM unavailable)", "tags": []}``
    when the LLM is unset or the provider errors out so cataloging continues
    degraded instead of failing the whole catalog endpoint with HTTP 502.
    Non-fallback callers still get the upstream dict on the happy path.
    """
    # Fast path: no provider configured → placeholder so cataloging still
    # produces an entry (description can be filled in manually later).
    if not config.llm_provider or not config.llm_api_key:
        if getattr(config, "farm_llm_required", False):
            logger.warning("Vision LLM unset; returning placeholder catalog entry")
        return dict(_VISION_UNAVAILABLE_RESULT)

    provider = config.llm_provider or "grok"
    api_key = config.llm_api_key or ""
    model = config.llm_model or "grok-4.20-0309-non-reasoning"
    base_url = config.llm_base_url or ""
    if not base_url:
        base_url = PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "https://api.x.ai/v1")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "You are an image cataloging assistant. Analyze the image and return a JSON object with:\n"
                '- "description": a concise 1-2 sentence description of what is shown\n'
                '- "tags": array of 5-8 single-word tags (e.g. indoor, elegant, fashion)\n'
                "Respond ONLY with valid JSON, no markdown, no extra text."
            )},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                {"type": "text", "text": "Describe this image for a media catalog."},
            ]},
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        text = data["choices"][0]["message"]["content"].strip()
        # Parse JSON from response (strip markdown fences if present)
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"vision response is not a dict: {type(parsed).__name__}")
        return parsed
    except (httpx.HTTPError, httpx.TimeoutException, OSError,
            ValueError, KeyError, json.JSONDecodeError) as exc:
        # Catalog endpoint must not return 502 on LLM hiccups — the file
        # still has a thumbnail + ffprobe metadata, a human can fill in
        # the description later. Log WARNING so ops still notice drift.
        logger.warning(
            "Vision LLM failed (%s: %s); returning placeholder catalog entry",
            type(exc).__name__, exc,
        )
        return dict(_VISION_UNAVAILABLE_RESULT)


def _update_catalog(catalog_path: Path, folder: str, filename: str, entry: dict[str, Any]) -> None:
    """Update or insert an entry in photo_catalog.json."""
    entries: list[dict[str, Any]] = []
    if catalog_path.is_file():
        try:
            entries = json.loads(catalog_path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                entries = []
        except Exception:
            entries = []

    # Remove existing entry for this file
    entries = [e for e in entries if not (e.get("filename") == filename and e.get("folder") == folder)]
    entries.append(entry)

    catalog_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


@router.post("/{name}/photos/{folder}/{filename}/catalog")
async def catalog_item(
    name: str,
    folder: str,
    filename: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Catalog a single file: generate thumbnail, extract metadata, describe via LLM vision."""
    _validate_name(name)
    _validate_model_folder(folder)
    _validate_name(filename)

    file_path = _models_dir(config) / name / folder / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    ext = file_path.suffix.lower()
    is_video = ext in _VIDEO_EXTENSIONS

    # 1. Extract metadata (video only) — offload the sync ffprobe
    # subprocess to a worker thread so it doesn't block the event
    # loop for 10 s per request (Codex iter 8 bug hunt 2026-04-14).
    import asyncio as _asyncio
    meta = await _asyncio.to_thread(_ffprobe_meta, file_path) if is_video else {}

    # 2. Get or generate thumbnail — same rationale; ffmpeg run
    # inside _ensure_thumbnail blocks for up to 15 s.
    if is_video:
        thumb_path = await _asyncio.to_thread(_ensure_thumbnail, file_path)
    else:
        thumb_path = file_path  # Use the image itself

    if not thumb_path or not thumb_path.is_file():
        raise HTTPException(status_code=500, detail="Failed to generate thumbnail")

    # 3. Send to LLM vision. _vision_describe never raises on LLM failure —
    # it returns the "(LLM unavailable)" placeholder so cataloging keeps
    # working even when no provider is configured. We keep the try/except
    # as a safety net for unexpected bugs and still return a placeholder.
    image_b64 = base64.b64encode(thumb_path.read_bytes()).decode("ascii")
    try:
        vision_result = await _vision_describe(config, image_b64)
    except Exception as exc:
        logger.warning(
            "LLM vision raised unexpectedly for %s (%s: %s); using placeholder",
            filename, type(exc).__name__, exc,
        )
        vision_result = {"description": "(LLM unavailable)", "tags": []}

    # 4. Build catalog entry
    entry = {
        "filename": filename,
        "folder": folder,
        "media_type": "video" if is_video else "image",
        "description": vision_result.get("description", ""),
        "tags": vision_result.get("tags", []),
        "duration_seconds": meta.get("duration_seconds"),
        "has_original_audio": meta.get("has_original_audio"),
    }

    # 5. Save to photo_catalog.json
    catalog_path = _models_dir(config) / name / "photo_catalog.json"
    _update_catalog(catalog_path, folder, filename, entry)

    logger.info("Cataloged %s/%s/%s: %s", name, folder, filename, entry.get("description", "")[:50])
    return entry
