"""Video download API: authenticated, time-limited file download for phone devices.

The phone downloads video files over HTTPS using a signed URL (HMAC token),
avoiding the overhead and blocking of base64 over WebSocket.

Flow:
1. Scheduler generates a signed download URL for a video file.
2. VPS sends ``video.download`` WS message with the URL to the phone.
3. Phone downloads via HTTPS (OkHttp), then sends ``video.download_complete``.
4. Scheduler receives confirmation and dispatches ``cmd.send_schedule``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select

from server.config import VPSConfig
from server.models import Video

logger = logging.getLogger(__name__)

router = APIRouter(tags=["video_download"])

# Token validity: 10 minutes
_TOKEN_TTL_SECONDS = 600


# ---------------------------------------------------------------------------
# Helpers: signed URL generation / verification
# ---------------------------------------------------------------------------

def _compute_hmac(video_id: int, expires: int, secret: str) -> str:
    """Compute HMAC-SHA256 for (video_id, expires) using the JWT secret."""
    message = f"{video_id}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _compute_asset_hmac(
    video_id: int,
    index: int,
    expires: int,
    secret: str,
) -> str:
    """Compute HMAC-SHA256 for (video_id, index, expires) — carousel asset token.

    Binding the index into the HMAC prevents a leaked token for image 0 from
    being replayed to fetch image 1 of the same carousel.
    """
    message = f"asset:{video_id}:{index}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _build_public_base_url(config: VPSConfig) -> str:
    """Build the public base URL for download endpoints.

    IP → ``http://{ip}:8443`` (Caddy), domain → ``https://{domain}``,
    fallback → ``http://{host}:{port}``.
    """
    import re as _re
    if config.domain:
        if _re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
            return f"http://{config.domain}:8443"
        else:
            return f"https://{config.domain}"
    return f"http://{config.host}:{config.port}"


def generate_download_url(video_id: int, config: VPSConfig) -> str:
    """Generate a time-limited signed download URL for a video.

    The URL includes an HMAC token and expiration timestamp as query params.
    Valid for 10 minutes.
    """
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _compute_hmac(video_id, expires, config.jwt_secret)
    base = _build_public_base_url(config)
    return f"{base}/api/videos/download/{video_id}?token={token}&expires={expires}"


def generate_carousel_asset_url(video_id: int, index: int, config: VPSConfig) -> str:
    """Generate a time-limited signed URL for a single carousel photo.

    The HMAC binds video_id, index, and expires together so a token for image 0
    cannot be replayed against image 1.
    """
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    token = _compute_asset_hmac(video_id, index, expires, config.jwt_secret)
    base = _build_public_base_url(config)
    return f"{base}/api/photo-sets/asset/{video_id}/{index}?token={token}&expires={expires}"


def verify_asset_token(
    video_id: int,
    index: int,
    token: str,
    expires: int,
    config: VPSConfig,
) -> bool:
    """Verify a carousel asset URL token (video_id + index bound).

    Use this for ``/api/photo-sets/asset/{video_id}/{index}`` — not ``verify_download_token``.
    """
    if int(time.time()) > expires:
        return False
    expected = _compute_asset_hmac(video_id, index, expires, config.jwt_secret)
    return hmac.compare_digest(token, expected)


def verify_download_token(
    video_id: int,
    token: str,
    expires: int,
    config: VPSConfig,
) -> bool:
    """Verify the HMAC signature and check expiration.

    Parameters
    ----------
    video_id : int
        The video ID from the URL path.
    token : str
        The HMAC token from the query string.
    expires : int
        The expiration epoch timestamp from the query string.
    config : VPSConfig
        Server config (provides jwt_secret).

    Returns
    -------
    bool
        True if the token is valid and not expired.
    """
    # Check expiration first (cheaper than HMAC)
    if int(time.time()) > expires:
        return False

    expected = _compute_hmac(video_id, expires, config.jwt_secret)
    return hmac.compare_digest(token, expected)


# ---------------------------------------------------------------------------
# Download endpoint
# ---------------------------------------------------------------------------

@router.get("/api/videos/download/{video_id}")
async def download_video(
    request: Request,
    video_id: int,
    token: str = Query(...),
    expires: int = Query(...),
) -> FileResponse:
    """Serve a video file for phone download.

    Authentication is via the signed URL token (HMAC), not JWT.
    This allows the phone to download via a simple GET request.

    Parameters
    ----------
    video_id : int
        Database ID of the video (from URL path).
    token : str
        HMAC signature (from query string).
    expires : int
        Expiration epoch timestamp (from query string).
    """
    config: VPSConfig = request.app.state.config

    if not verify_download_token(video_id, token, expires, config):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired download token",
        )

    # Look up the video in the DB to find the file path
    session_factory = request.app.state.db_session_factory
    async with session_factory() as session:
        result = await session.execute(
            select(Video).where(Video.id == video_id),
        )
        video = result.scalar_one_or_none()

    if video is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video not found",
        )

    # Resolve file path: prefer original_path, fallback to data_dir convention.
    # For stories, media lives in data_dir/stories/{user}/, not data_dir/videos/.
    if video.original_path and os.path.isfile(video.original_path):
        file_path = video.original_path
    elif video.content_type == "story":
        story_path = os.path.join(
            config.data_dir, "stories", video.account_username, video.filename,
        )
        if os.path.isfile(story_path):
            file_path = story_path
        else:
            file_path = os.path.join(
                config.data_dir, "videos", video.account_username, video.filename,
            )
    else:
        file_path = os.path.join(
            config.data_dir, "videos", video.account_username, video.filename,
        )

    # Validate resolved path is within data_dir (defense against DB tampering)
    data_dir_real = os.path.realpath(config.data_dir)
    file_path_real = os.path.realpath(file_path)
    if not file_path_real.startswith(data_dir_real + os.sep):
        logger.error("Path traversal blocked: video_id=%d, path=%s", video_id, file_path)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    # Single-step TOCTOU-safe open (Codex Q1/Q5 iteration 3):
    #
    #   - `open()` itself is the only existence check we need — it
    #     raises FileNotFoundError atomically, no separate `isfile()`
    #     guard. Removing the guard eliminates the race window
    #     entirely (isfile true → open fails is now impossible).
    #
    #   - Sync `open()` + `os.fstat()` would block the event loop on
    #     a slow filesystem, so both are wrapped in
    #     `asyncio.to_thread(...)`. Once the fd is held, a subsequent
    #     atomic rename on Linux does NOT invalidate it — the fd
    #     still points at the original inode, so the streaming
    #     response reads the bytes we promised the phone even if the
    #     name now points at a new ghosted file.
    def _open_and_stat(path: str) -> tuple[Any, int]:
        fh = open(path, "rb")  # noqa: SIM115 (closed in generator)
        try:
            sz = os.fstat(fh.fileno()).st_size
        except OSError:
            fh.close()
            raise
        return fh, sz

    try:
        file_handle, file_size = await asyncio.to_thread(_open_and_stat, file_path)
    except FileNotFoundError:
        logger.warning(
            "Video file missing on disk: video_id=%d path=%s",
            video_id, file_path,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video file not found on disk",
        )
    except OSError as exc:
        logger.error("fstat failed for video %d: %s", video_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to stat video file",
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

    safe_filename = video.filename.replace('"', "_")
    return StreamingResponse(
        _stream(),
        media_type="video/mp4",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Length": str(file_size),
        },
    )
