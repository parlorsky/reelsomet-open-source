"""Manual photo batch push API.

This path prepares already-ghosted photos for Instagram's gallery picker
without creating queue rows or touching the posting scheduler.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import (
    get_bridge,
    get_config,
    get_db_session,
    get_ws_manager,
    require_auth,
)
from server.models import Device
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

router = APIRouter(prefix="/api/manual-photo-push", tags=["manual_photo_push"])

_TOKEN_TTL_SECONDS = 600
_GHOST_TIMEOUT_SECONDS = 120
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ManualPhotoPushRequest(BaseModel):
    batch_id: str


def _utc_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _validate_component(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field_name} cannot be empty")
    if "/" in normalized or "\\" in normalized or ".." in normalized:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {field_name}")
    return normalized


def _build_public_base_url(config: VPSConfig) -> str:
    if config.domain:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
            return f"http://{config.domain}:8443"
        return f"https://{config.domain}"
    return f"http://{config.host}:{config.port}"


def _compute_asset_token(
    batch_id: str,
    device_key: str,
    filename: str,
    expires: int,
    secret: str,
) -> str:
    message = f"manual-photo:{batch_id}:{device_key}:{filename}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _manual_photo_dir(config: VPSConfig, batch_id: str, device_key: str) -> Path:
    return Path(config.data_dir) / "manual_photo_push" / batch_id / "ghosted" / device_key


def _manual_photo_batch_dir(config: VPSConfig, batch_id: str) -> Path:
    return Path(config.data_dir) / "manual_photo_push" / batch_id


def _manual_photo_raw_dir(config: VPSConfig, batch_id: str) -> Path:
    return _manual_photo_batch_dir(config, batch_id) / "raw"


def _manual_photo_common_ghosted_dir(config: VPSConfig, batch_id: str) -> Path:
    return _manual_photo_batch_dir(config, batch_id) / "ghosted"


def _manifest_path(config: VPSConfig, batch_id: str) -> Path:
    return _manual_photo_batch_dir(config, batch_id) / "manifest.json"


def _safe_upload_filename(filename: str, index: int) -> str:
    original = Path(filename or f"photo_{index}").name
    suffix = Path(original).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported image type: {original}")
    stem = Path(original).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:80] or f"photo_{index}"
    return f"{index:03d}_{stem}{suffix}"


def _ghost_filename(raw_filename: str) -> str:
    stem = Path(raw_filename).stem
    return f"{stem}_ghost.jpg"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value[:80].hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value[:20]]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in list(value.items())[:40]}
    return str(value)


def _extract_image_metadata(path: Path) -> dict[str, Any]:
    stat = path.stat()
    sha256 = _sha256_file(path)
    meta: dict[str, Any] = {
        "filename": path.name,
        "extension": path.suffix.lower(),
        "mime_type": _IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        "size_bytes": stat.st_size,
        "sha256": sha256,
        "sha256_short": sha256[:16],
    }

    try:
        from PIL import ExifTags, Image

        with Image.open(path) as img:
            meta.update({
                "format": img.format,
                "width": img.width,
                "height": img.height,
                "mode": img.mode,
            })
            exif = img.getexif()
            meta["exif_count"] = len(exif) if exif else 0
            if exif:
                tags: dict[str, Any] = {}
                for key, value in list(exif.items())[:80]:
                    tag_name = ExifTags.TAGS.get(key, str(key))
                    tags[tag_name] = _json_safe(value)
                meta["exif"] = tags
    except Exception as exc:
        meta["probe_error"] = str(exc)

    return meta


def _write_manifest(config: VPSConfig, manifest: dict[str, Any]) -> None:
    path = _manifest_path(config, manifest["batch_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_manifest(config: VPSConfig, batch_id: str) -> dict[str, Any]:
    batch_id = _validate_component(batch_id, "batch_id")
    path = _manifest_path(config, batch_id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Photo batch not found: {batch_id}")
    return json.loads(path.read_text(encoding="utf-8"))


async def _ghost_one_photo(config: VPSConfig, raw_path: Path, ghosted_path: Path) -> tuple[bool, str | None]:
    if not config.ghost_enabled:
        return False, "ghost.enabled=false"

    from server.ghost import ghost_media_safe

    threshold = float(getattr(config, "ghost_ssim_threshold", 0.90) or 0.90)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ghost_media_safe, str(raw_path), str(ghosted_path), threshold),
            timeout=_GHOST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return False, f"ghost_timeout_after_{_GHOST_TIMEOUT_SECONDS}s"
    except Exception as exc:
        return False, str(exc)

    if result is None or not ghosted_path.is_file():
        return False, "ghost_failed"
    return True, None


def _copy_common_ghosted_to_device(config: VPSConfig, batch_id: str, device_key: str) -> Path:
    device_dir = _manual_photo_dir(config, batch_id, device_key)
    if device_dir.is_dir() and any(device_dir.iterdir()):
        return device_dir

    common_dir = _manual_photo_common_ghosted_dir(config, batch_id)
    if not common_dir.is_dir():
        return device_dir

    common_files = sorted(
        p for p in common_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
    )
    if not common_files:
        return device_dir

    device_dir.mkdir(parents=True, exist_ok=True)
    for src in common_files:
        shutil.copy2(src, device_dir / src.name)
    return device_dir


def _asset_url(config: VPSConfig, batch_id: str, device_key: str, filename: str) -> str:
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _compute_asset_token(batch_id, device_key, filename, expires, config.jwt_secret)
    base = _build_public_base_url(config)
    return (
        f"{base}/api/manual-photo-push/assets/"
        f"{batch_id}/{device_key}/{filename}?token={token}&expires={expires}"
    )


@router.post("/batches")
async def create_manual_photo_batch(
    files: list[UploadFile] = File(...),
    batch_id: str | None = Form(None),
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Upload photos, run each through ghoster, and store a preview manifest."""
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one photo is required")

    if batch_id and batch_id.strip():
        normalized_batch_id = _validate_component(batch_id, "batch_id")
    else:
        normalized_batch_id = f"ghost_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

    batch_dir = _manual_photo_batch_dir(config, normalized_batch_id)
    if batch_dir.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Photo batch already exists: {normalized_batch_id}")

    raw_dir = _manual_photo_raw_dir(config, normalized_batch_id)
    ghosted_dir = _manual_photo_common_ghosted_dir(config, normalized_batch_id)
    raw_dir.mkdir(parents=True, exist_ok=False)
    ghosted_dir.mkdir(parents=True, exist_ok=False)

    items: list[dict[str, Any]] = []
    try:
        for idx, upload in enumerate(files):
            raw_name = _safe_upload_filename(upload.filename or f"photo_{idx}", idx)
            raw_path = raw_dir / raw_name
            size = 0
            with raw_path.open("wb") as fh:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    fh.write(chunk)
            if size == 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Empty upload: {upload.filename or raw_name}")

            ghosted_name = _ghost_filename(raw_name)
            ghosted_path = ghosted_dir / ghosted_name
            before = _extract_image_metadata(raw_path)
            ghosted, error = await _ghost_one_photo(config, raw_path, ghosted_path)
            after = _extract_image_metadata(ghosted_path) if ghosted else None
            items.append({
                "index": idx,
                "source_filename": upload.filename or raw_name,
                "raw_filename": raw_name,
                "ghosted_filename": ghosted_name if ghosted else None,
                "status": "ghosted" if ghosted else "failed",
                "ghost_applied": ghosted,
                "ghost_error": error,
                "before": before,
                "after": after,
            })
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    ghosted_count = sum(1 for item in items if item["ghost_applied"])
    manifest = {
        "batch_id": normalized_batch_id,
        "created_at": _utc_iso(),
        "updated_at": _utc_iso(),
        "status": "ready" if ghosted_count == len(items) else "partial",
        "item_count": len(items),
        "ghosted_count": ghosted_count,
        "failed_count": len(items) - ghosted_count,
        "items": items,
    }
    _write_manifest(config, manifest)
    return manifest


@router.get("/batches")
async def list_manual_photo_batches(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """List recent ghosted manual photo batches."""
    base = Path(config.data_dir) / "manual_photo_push"
    if not base.is_dir():
        return []
    manifests: list[dict[str, Any]] = []
    for path in sorted(base.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        manifests.append({
            "batch_id": manifest.get("batch_id"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "status": manifest.get("status"),
            "item_count": manifest.get("item_count", 0),
            "ghosted_count": manifest.get("ghosted_count", 0),
            "failed_count": manifest.get("failed_count", 0),
        })
    return manifests


@router.get("/batches/{batch_id}")
async def get_manual_photo_batch(
    batch_id: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Return a manual photo batch manifest with before/after metadata."""
    return _read_manifest(config, batch_id)


@router.post("/devices/{device_pk}")
async def push_manual_photo_batch(
    device_pk: int,
    body: ManualPhotoPushRequest,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Push a ghosted photo batch to one online phone."""
    batch_id = _validate_component(body.batch_id, "batch_id")
    device_key = f"device_{device_pk}"

    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Device '{device.name}' is offline")

    source_dir = _copy_common_ghosted_to_device(config, batch_id, device_key)
    if not source_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Photo batch not found: {batch_id}/{device_key}")

    files = sorted(
        p for p in source_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _ALLOWED_SUFFIXES
    )
    if not files:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Photo batch is empty: {batch_id}/{device_key}")

    assets = [
        {
            "url": _asset_url(config, batch_id, device_key, p.name),
            "filename": p.name,
            "index": idx,
        }
        for idx, p in enumerate(files)
    ]

    result = await bridge.push_image_assets(device_pk, batch_id=batch_id, assets=assets)
    return {
        "status": "ok",
        "device_id": device_pk,
        "device_name": device.name,
        "batch_id": batch_id,
        "asset_count": len(assets),
        "device_result": result,
    }


@router.get("/assets/{batch_id}/{device_key}/{filename}")
async def download_manual_photo_asset(
    batch_id: str,
    device_key: str,
    filename: str,
    token: str,
    expires: int,
    config: VPSConfig = Depends(get_config),
):
    """Serve one signed manual-photo-push asset to a phone."""
    batch_id = _validate_component(batch_id, "batch_id")
    device_key = _validate_component(device_key, "device_key")
    filename = _validate_component(filename, "filename")
    if int(time.time()) > expires:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Token expired")
    expected = _compute_asset_token(batch_id, device_key, filename, expires, config.jwt_secret)
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid token")

    file_path = _manual_photo_dir(config, batch_id, device_key) / filename
    if not file_path.is_file() or file_path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asset not found")

    media_type = _IMAGE_MEDIA_TYPES.get(file_path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type, filename=file_path.name)
