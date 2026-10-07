"""Manual prep API: ghost a vid_bait video on demand and push it to a phone.

Flow:
  1. Operator clicks "Manual prep" on a vid_bait tile in the admin UI,
     picks a scenario shortcode + a target device.
  2. ``POST /api/models/{name}/photos/vid_bait/{filename}/manual-prep`` runs
     the ghost pipeline on the source file with a fresh donor pick (anti-
     collision: every click ghosts independently).
  3. The ghosted result is parked in ``<data_dir>/manual_prep/<uuid>.mp4``
     and a one-time signed URL is minted for it.
  4. ``cmd.video.manual_prep`` is sent over the WebSocket to the chosen
     device.  The phone downloads, copies to MediaStore (``Movies/Reelsomet``),
     and shows a notification whose action copies the scenario caption.
  5. Operator opens Instagram, picks the file from the gallery, and posts
     manually with the pasted caption.

This endpoint deliberately does NOT touch the automated posting pipeline —
no ``Video`` row is created, no scheduler interaction.  The ghosted file is
TTL-cleaned by ``cleanup_manual_prep_dir`` (see :mod:`server.app`).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.scenarios import _load_scenarios
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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["manual_prep"])

_VID_BAIT_FOLDER = "vid_bait"
_MANUAL_PREP_DIRNAME = "manual_prep"
_TOKEN_TTL_SECONDS = 600
_FILE_TTL_SECONDS = 30 * 60  # 30 minutes — operator should post within this
_GHOST_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ManualPrepRequest(BaseModel):
    scenario_shortcode: str
    device_id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid name: path traversal not allowed",
        )


def _models_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / "models"


def _manual_prep_dir(config: VPSConfig) -> Path:
    return Path(config.data_dir) / _MANUAL_PREP_DIRNAME


def _compute_manual_token(token_id: str, expires: int, secret: str) -> str:
    """HMAC-SHA256 over the staged-file id and expiry."""
    message = f"manual:{token_id}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _build_public_base_url(config: VPSConfig) -> str:
    """Mirror of ``server.api.video_download._build_public_base_url``."""
    if config.domain:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
            return f"http://{config.domain}:8443"
        return f"https://{config.domain}"
    return f"http://{config.host}:{config.port}"


def _generate_manual_download_url(token_id: str, config: VPSConfig) -> str:
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _compute_manual_token(token_id, expires, config.jwt_secret)
    base = _build_public_base_url(config)
    return f"{base}/api/manual-prep/download/{token_id}?token={token}&expires={expires}"


# ---------------------------------------------------------------------------
# Background cleanup
# ---------------------------------------------------------------------------

async def cleanup_manual_prep_dir(config: VPSConfig) -> int:
    """Remove ghosted manual-prep files older than ``_FILE_TTL_SECONDS``.

    Called from a periodic task in ``server.app``.  Best-effort: any
    individual unlink that fails (e.g. file currently being streamed)
    is logged and skipped.
    """
    base = _manual_prep_dir(config)
    if not base.is_dir():
        return 0
    cutoff = time.time() - _FILE_TTL_SECONDS
    removed = 0
    try:
        entries = list(base.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            mtime = entry.stat().st_mtime
            if mtime < cutoff:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning("manual_prep cleanup failed for %s: %s", entry, exc)
    if removed:
        logger.info("manual_prep cleanup removed %d stale files", removed)
    return removed


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/models/{name}/photos/vid_bait/{filename}/manual-prep",
    status_code=status.HTTP_202_ACCEPTED,
)
async def manual_prep(
    name: str,
    filename: str,
    body: ManualPrepRequest,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Ghost a vid_bait file and dispatch it to a phone for manual upload."""
    _validate_name(name)
    _validate_name(filename)

    # 1. Source file lookup (path-based, mirrors models_api.py).
    src_path = _models_dir(config) / name / _VID_BAIT_FOLDER / filename
    if not src_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"vid_bait file '{filename}' not found for model '{name}'",
        )

    # 2. Scenario lookup.  Reuse the scenarios loader so any future
    # caching / migration shows up here without a code change.
    scenario_shortcode = body.scenario_shortcode.strip()
    if not scenario_shortcode:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scenario_shortcode cannot be empty",
        )
    scenarios = _load_scenarios(config)
    scenario = next(
        (s for s in scenarios
         if str(s.get("shortcode", "")).strip() == scenario_shortcode),
        None,
    )
    if scenario is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Scenario '{scenario_shortcode}' not found",
        )
    caption = str(scenario.get("caption") or "")

    # 3. Device lookup.  Must be active AND online.  Offline devices
    # are filtered out at the UI layer too; this is the defense-in-
    # depth check.
    device = (await session.execute(
        select(Device).where(
            Device.id == body.device_id,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Device {body.device_id} not found",
        )
    if not ws.is_online(body.device_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device '{device.name}' is offline",
        )

    # 4. Stage paths and kick off the heavy work (ghost + WS dispatch)
    # in a background task so the HTTP request returns 202 immediately.
    # The Caddy/Vite proxy chain enforces a 15s read timeout on the
    # admin client, and ghost commonly takes 30-60s on a fresh donor
    # pick. Operator gets the result via a Telegram notification when
    # the phone reports back over the WS.
    base_dir = _manual_prep_dir(config)
    base_dir.mkdir(parents=True, exist_ok=True)
    token_id = uuid.uuid4().hex
    out_ext = src_path.suffix or ".mp4"
    safe_filename = f"reelsomet_{token_id[:12]}{out_ext}"
    request_id = uuid.uuid4().hex

    asyncio.create_task(_ghost_and_dispatch(
        src_path=src_path,
        base_dir=base_dir,
        token_id=token_id,
        safe_filename=safe_filename,
        caption=caption,
        scenario_shortcode=scenario_shortcode,
        name=name,
        device_id=body.device_id,
        device_name=device.name,
        request_id=request_id,
        config=config,
        bridge=bridge,
    ))

    return {
        "status": "accepted",
        "request_id": request_id,
        "device_id": body.device_id,
        "device_name": device.name,
        "filename": safe_filename,
        "caption": caption,
        "scenario_shortcode": scenario_shortcode,
    }


async def _ghost_and_dispatch(
    *,
    src_path: Path,
    base_dir: Path,
    token_id: str,
    safe_filename: str,
    caption: str,
    scenario_shortcode: str,
    name: str,
    device_id: int,
    device_name: str,
    request_id: str,
    config: VPSConfig,
    bridge: DeviceBridge,
) -> None:
    """Run the slow path: ghost the file, dispatch over WS.

    On any failure we synthesise an event.manual_prep_complete with
    success=False and the broadcaster wired by ``attach_broadcaster``
    so the admin UI / Telegram notifier still hears about it.
    """
    out_ext = src_path.suffix or ".mp4"
    out_path = base_dir / f"{token_id}{out_ext}"
    ghost_used = False
    error: str | None = None

    if config.ghost_enabled:
        from server.ghost import ghost_media_safe
        try:
            ghosted = await asyncio.wait_for(
                asyncio.to_thread(ghost_media_safe, str(src_path), str(out_path)),
                timeout=_GHOST_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            ghosted = None
            logger.warning(
                "[MANUAL_PREP] Ghost timed out after %ds for %s; falling back to copy",
                _GHOST_TIMEOUT_SECONDS, src_path,
            )
        if ghosted is not None:
            ghost_used = True
        else:
            try:
                if out_path.exists():
                    out_path.unlink(missing_ok=True)
            except OSError:
                pass

    if not ghost_used:
        try:
            await asyncio.to_thread(shutil.copy2, str(src_path), str(out_path))
        except OSError as exc:
            error = f"stage copy failed: {exc}"
            logger.error("[MANUAL_PREP] %s", error)

    if error is None:
        download_url = _generate_manual_download_url(token_id, config)
        try:
            await bridge.send_manual_prep_download(
                device_id=device_id,
                url=download_url,
                username=name,
                filename=safe_filename,
                caption=caption,
                request_id=request_id,
            )
        except Exception as exc:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            error = f"WS dispatch failed: {exc}"
            logger.error("[MANUAL_PREP] WS dispatch failed for device %d: %s",
                         device_id, exc)

    if error is not None:
        broadcaster = getattr(_ghost_and_dispatch, "_broadcaster", None)
        if broadcaster is not None:
            try:
                await broadcaster.broadcast(
                    "event.manual_prep_complete",
                    {
                        "requestId": request_id,
                        "success": False,
                        "error": error,
                        "filename": safe_filename,
                        "caption": caption,
                        "device_id": device_id,
                        "device_name": device_name,
                        "model": name,
                        "scenario_shortcode": scenario_shortcode,
                        "ghost_applied": False,
                    },
                )
            except Exception:
                logger.exception("[MANUAL_PREP] failed to broadcast failure event")
        return

    logger.info(
        "[MANUAL_PREP] Sent %s (model=%s, scenario=%s) to device %d (%s); ghost=%s",
        safe_filename, name, scenario_shortcode, device_id, device_name,
        "yes" if ghost_used else "no",
    )


def attach_broadcaster(broadcaster: Any) -> None:
    """Wire the admin-WS broadcaster used to surface async errors."""
    setattr(_ghost_and_dispatch, "_broadcaster", broadcaster)


@router.get("/api/manual-prep/download/{token_id}")
async def manual_prep_download(
    request: Request,
    token_id: str,
    token: str = Query(...),
    expires: int = Query(...),
) -> StreamingResponse:
    """Stream a staged manual-prep file to the phone.

    Auth is via the signed URL token (HMAC over token_id + expires) — no
    JWT required so the phone can use a plain GET.  Mirrors the
    ``/api/videos/download/{video_id}`` flow but keyed by the staged
    token id rather than a Video DB row.
    """
    config: VPSConfig = request.app.state.config

    # Validate token id shape — must be a 32-char hex string from
    # uuid4.hex.  Reject anything else before touching disk.
    if not re.fullmatch(r"[0-9a-f]{32}", token_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token id",
        )
    if int(time.time()) > expires:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Download URL expired",
        )
    expected = _compute_manual_token(token_id, expires, config.jwt_secret)
    if not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid download token",
        )

    base_dir = _manual_prep_dir(config)

    # Find the staged file by token_id prefix (extension was set at
    # stage time; we don't have it on the URL).
    candidates = list(base_dir.glob(f"{token_id}.*")) if base_dir.is_dir() else []
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staged file not found (already cleaned up?)",
        )
    file_path = candidates[0]

    # Defense-in-depth: resolve and confirm the file is inside the
    # manual_prep dir.
    real_base = Path(base_dir.resolve())
    real_file = Path(file_path.resolve())
    try:
        real_file.relative_to(real_base)
    except ValueError:
        logger.error("manual_prep path traversal blocked: %s", file_path)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    def _open_and_stat(path: str) -> tuple[Any, int]:
        fh = open(path, "rb")  # noqa: SIM115 (closed in generator)
        try:
            import os
            sz = os.fstat(fh.fileno()).st_size
        except OSError:
            fh.close()
            raise
        return fh, sz

    try:
        file_handle, file_size = await asyncio.to_thread(_open_and_stat, str(file_path))
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Staged file vanished",
        )

    def _stream() -> Iterator[bytes]:
        try:
            while True:
                chunk = file_handle.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            file_handle.close()

    safe_name = file_path.name.replace('"', "_")
    return StreamingResponse(
        _stream(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(file_size),
        },
    )
