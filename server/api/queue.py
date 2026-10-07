"""Queue API: video queue management, upload, cancel, retry, bulk ops."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile, File, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import (
    get_admin_broadcaster,
    get_bridge,
    get_config,
    get_db_session,
    get_ws_manager,
    require_auth,
)
from server.device_actions import abort_engagement_on_device, cancel_video_on_device
from server import farm_time
from server.models import Account, AccountDevice, Device, EngagementSession, PhotoSetUsage, Video
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

_ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
# Stories can be either video or a still image, so we union the video
# set with the common image formats Instagram accepts. The phone's
# StoryPostingFSM detects video vs image from the file extension and
# routes to the right upload handler on the Instagram side.
#
# .heic is intentionally NOT on the allow-list (Codex Q3 2026-04-13):
# Instagram's story picker has inconsistent HEIC handling across
# Android API 26-35 and MediaStoreHelper does not transcode. Add HEIC
# back only after confirming on-device or after adding an
# upload-time HEIC→JPEG transcode step.
_ALLOWED_STORY_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_ALLOWED_STORY_EXTENSIONS = _ALLOWED_VIDEO_EXTENSIONS | _ALLOWED_STORY_IMAGE_EXTENSIONS
_ALLOWED_CONTENT_TYPES = {"reel", "story"}
_LOCAL_CANCELABLE_VIDEO_STATUSES = {"pending", "failed"}
_REMOTE_CANCELABLE_VIDEO_STATUSES = {"queued", "scheduled", "posting"}
_CLEAR_ALL_VIDEO_STATUSES = (
    _LOCAL_CANCELABLE_VIDEO_STATUSES
    | _REMOTE_CANCELABLE_VIDEO_STATUSES
    | {"cancelled"}
)
_DELETABLE_VIDEO_STATUSES = {"posted", "failed", "cancelled"}
_POST_NOWABLE_VIDEO_STATUSES = {"pending"}
_VISIBLE_ERROR_STATUSES = {"failed", "cancelled"}
_QUEUE_BROWSER_RENDER_LIMIT = 50

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


class BulkIds(BaseModel):
    ids: list[int]


def _utcnow() -> datetime:
    """Return a naive UTC timestamp for SQLite-backed models."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _broadcast_queue_update(
    broadcaster: AdminBroadcaster,
    *,
    video_id: int,
    status_value: str,
    action: str,
) -> None:
    """Notify admin dashboards about a queue mutation."""
    await broadcaster.broadcast("queue:update", {
        "video_id": video_id,
        "status": status_value,
        "action": action,
    })


def _queue_error_message(video: Video) -> str | None:
    """Return the error message that should be exposed in the queue UI."""
    if video.status not in _VISIBLE_ERROR_STATUSES:
        return None
    return video.post_error or video.upload_error


def _serialize_queue_row(video: Video, device_name: str | None) -> dict[str, Any]:
    """Return the queue row shape consumed by the admin UI."""
    return {
        "id": video.id,
        "filename": video.filename,
        "account_username": video.account_username,
        "device_name": device_name or "-",
        "status": video.status,
        "caption": video.caption,
        "error_message": _queue_error_message(video),
        "scheduled_at": farm_time.isoformat_utc(video.scheduled_time),
        "content_type": video.content_type or "reel",
        "images": json.loads(video.image_filenames) if video.image_filenames else [],
        "retry_count": video.retry_count,
        "created_at": farm_time.isoformat_utc(video.created_at),
        "posted_at": farm_time.isoformat_utc(video.posted_at),
    }


def _reset_retryable_video_state(video: Video) -> None:
    """Clear stale failure metadata before re-queueing a video."""
    video.status = "pending"
    video.post_error = None
    video.upload_error = None
    video.post_result = None
    video.post_duration_ms = None
    video.posted_at = None


async def _release_carousel_usage(
    video: Video, session: AsyncSession,
) -> None:
    """Delete any PhotoSetUsage rows tied to a cancelled/deleted carousel Video.

    Without this cleanup, a carousel that was generated but never
    successfully posted permanently burns one use of its photo set
    against the `max_uses_per_account` cap. Calling this on a
    non-carousel Video is a no-op.

    Orphan-safe: if the dedup bookkeeping was already out of sync for
    any reason, this also drops any PhotoSetUsage rows whose
    `video_id` references a Video that has just been deleted (the FK
    column will soon be dangling on the SQLite side since the project
    does not enforce FK by default).
    """
    if video.content_type != "carousel":
        return
    from sqlalchemy import delete as _sa_delete
    await session.execute(
        _sa_delete(PhotoSetUsage).where(PhotoSetUsage.video_id == video.id)
    )


async def _cancel_video_record(
    video: Video,
    *,
    session: AsyncSession,
    bridge: DeviceBridge,
    ws: DeviceConnectionManager,
    force: bool = False,
) -> None:
    """Cancel a queued video without lying about device state.

    `force=True` overrides the "uploading in progress" guard. Use it
    when the upload has clearly hung — the safety check exists to
    avoid mid-flight desync, but in real life a phone WS can drop
    while a chunked upload is half-delivered and the row stays in
    `uploading` forever. The `recover_stale_videos` job will
    eventually clean it up after `farm_upload_window_minutes * 3`,
    but that's slow; the admin UI needs a fast escape hatch.
    """
    if video.status in _LOCAL_CANCELABLE_VIDEO_STATUSES:
        video.status = "cancelled"
        video.post_error = video.post_error or "Cancelled by admin"
        await _release_carousel_usage(video, session)
        return

    if video.status == "uploading":
        if force:
            video.status = "cancelled"
            video.post_error = "Cancelled by admin (forced — upload was hung)"
            video.uploaded_to_phone = False
            await _release_carousel_usage(video, session)
            return
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Video upload is already in progress on the device and cannot be cancelled safely yet.",
        )

    if video.status in _REMOTE_CANCELABLE_VIDEO_STATUSES:
        _device_id, error = await cancel_video_on_device(
            video=video,
            session=session,
            bridge=bridge,
            ws=ws,
        )
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error,
            )
        video.status = "cancelled"
        video.post_error = "Cancelled by admin"
        await _release_carousel_usage(video, session)
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Cannot cancel video with status '{video.status}'",
    )


def _delete_video_file(original_path: str | None, *, config: VPSConfig) -> None:
    """Remove a stored queue file when it still lives under the VPS data dir."""
    if not original_path:
        return

    data_dir = Path(config.data_dir).resolve()
    file_path = Path(original_path).resolve()
    try:
        file_path.relative_to(data_dir)
    except ValueError:
        logger.warning("Skipping queue file delete outside data dir: %s", original_path)
        return

    if file_path.is_file():
        file_path.unlink()


@router.get("")
async def list_queue(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    status_filter: str | None = Query(None, alias="status"),
    account_username: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    paginated: bool = Query(False),
) -> Any:
    """Return the video queue with optional filtering."""
    device_name_subquery = (
        select(Device.name)
        .join(AccountDevice, Device.id == AccountDevice.device_id)
        .where(
            AccountDevice.account_username == Video.account_username,
            Device.is_active.is_(True),
        )
        .order_by(AccountDevice.is_primary.desc(), AccountDevice.id.asc(), Device.id.asc())
        .limit(1)
        .scalar_subquery()
    )

    filters = []
    if status_filter:
        filters.append(Video.status == status_filter)
    if account_username:
        filters.append(Video.account_username == account_username)
    if search:
        pattern = f"%{search}%"
        filters.append(
            or_(
                Video.filename.ilike(pattern),
                Video.caption.ilike(pattern),
                Video.account_username.ilike(pattern),
            ),
        )

    query = (
        select(Video, device_name_subquery.label("device_name"))
        .where(*filters)
        .order_by(Video.created_at.desc())
    )

    effective_limit = limit if paginated else min(limit, _QUEUE_BROWSER_RENDER_LIMIT)
    result = await session.execute(query.offset(offset).limit(effective_limit))
    rows = result.all()
    items = [_serialize_queue_row(v, device_name) for v, device_name in rows]

    if not paginated:
        return items

    total = (await session.execute(
        select(func.count(Video.id)).where(*filters),
    )).scalar_one()
    return {
        "items": items,
        "total": total,
        "limit": effective_limit,
        "offset": offset,
        "has_more": offset + len(items) < total,
    }


@router.get("/stats")
async def queue_stats(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """Return per-status counts for the video queue."""
    counts: dict[str, int] = {}
    for s in ("pending", "scheduled", "queued", "uploading", "posting", "posted", "failed", "cancelled"):
        cnt = (await session.execute(
            select(func.count(Video.id)).where(Video.status == s),
        )).scalar_one()
        counts[s] = cnt
    counts["total"] = sum(counts.values())
    return counts


@router.get("/schedule")
async def upcoming_schedule(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
) -> list[dict[str, Any]]:
    """Return a flat timeline of all upcoming/running actions across all accounts."""
    now = farm_time.utcnow_naive()
    default_times = config.farm_default_posting_times or []
    actions: list[dict[str, Any]] = []

    # All active accounts
    accts = (await session.execute(
        select(Account).where(Account.is_active.is_(True))
    )).scalars().all()

    # Build device lookup
    dev_map: dict[str, tuple[int, str]] = {}  # username -> (device_id, device_name)
    for acct in accts:
        row = (await session.execute(
            select(AccountDevice.device_id, Device.name)
            .join(Device, Device.id == AccountDevice.device_id)
            .where(
                AccountDevice.account_username == acct.username,
                Device.is_active == True,  # noqa: E712
            )
            .order_by(AccountDevice.is_primary.desc(), AccountDevice.id)
            .limit(1)
        )).first()
        if row:
            dev_map[acct.username] = (row[0], row[1] or str(row[0]))

    for acct in accts:
        dev_id, dev_name = dev_map.get(acct.username, (0, "-"))

        # --- Posting: next slot ---
        if acct.posting_enabled:
            raw = acct.posting_times
            if raw and raw.strip().startswith("["):
                try:
                    times = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    times = default_times
            elif raw:
                times = [t.strip().strip('"') for t in raw.split(",") if t.strip()]
            else:
                times = default_times

            slots = farm_time.compute_future_slots_utc(
                times_str=times,
                now_utc=now,
                timezone_name=config.farm_timezone,
                jitter_std=0,
            )
            if slots:
                actions.append({
                    "time": farm_time.isoformat_utc(slots[0]),
                    "type": "post",
                    "account": acct.username,
                    "device": dev_name,
                    "device_id": dev_id,
                    "status": "upcoming",
                    "detail": "Reel post",
                })

        # --- Engagement ---
        if acct.engagement_enabled:
            active_eng = (await session.execute(
                select(EngagementSession).where(
                    EngagementSession.account_username == acct.username,
                    EngagementSession.status == "running",
                )
            )).scalar_one_or_none()
            if active_eng:
                actions.append({
                    "time": farm_time.isoformat_utc(active_eng.started_at or now),
                    "type": "engagement",
                    "account": acct.username,
                    "device": dev_name,
                    "device_id": dev_id,
                    "status": "running",
                    "detail": f"Likes: {active_eng.total_likes}, Comments: {active_eng.total_comments}",
                    "session_id": active_eng.id,
                })
            else:
                spd = acct.engagement_sessions_day or 1
                interval_h = 24.0 / spd
                if acct.last_engagement_at:
                    nxt = acct.last_engagement_at + timedelta(hours=interval_h)
                else:
                    nxt = now
                actions.append({
                    "time": farm_time.isoformat_utc(max(nxt, now)),
                    "type": "engagement",
                    "account": acct.username,
                    "device": dev_name,
                    "device_id": dev_id,
                    "status": "due" if nxt <= now else "upcoming",
                    "detail": f"Session ({spd}/day)",
                })

        # --- Insights ---
        if acct.insights_enabled:
            interval = acct.insights_interval_hours or 6.0
            if acct.last_insights_at:
                nxt = acct.last_insights_at + timedelta(hours=interval)
            else:
                nxt = now
            actions.append({
                "time": farm_time.isoformat_utc(max(nxt, now)),
                "type": "insights",
                "account": acct.username,
                "device": dev_name,
                "device_id": dev_id,
                "status": "due" if nxt <= now else "upcoming",
                "detail": f"Collect reels data",
            })

    # --- Videos currently in pipeline ---
    active_vids = (await session.execute(
        select(Video).where(Video.status.in_(["uploading", "posting", "scheduled"]))
    )).scalars().all()
    for v in active_vids:
        d = dev_map.get(v.account_username, (0, "-"))
        actions.append({
            "time": farm_time.isoformat_utc(v.scheduled_time or v.created_at or now),
            "type": "post",
            "account": v.account_username,
            "device": d[1],
            "device_id": d[0],
            "status": v.status,
            "detail": v.filename,
            "video_id": v.id,
        })

    actions.sort(key=lambda a: ("running" not in a["status"], a["time"]))
    return actions


class CancelActionBody(BaseModel):
    action_id: str


@router.post("/cancel-action")
async def cancel_action(
    body: CancelActionBody,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    bridge: DeviceBridge = Depends(get_bridge),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
) -> dict[str, str]:
    """Cancel a running or upcoming action. Aborts on device if running."""
    parts = body.action_id.split(":")
    if len(parts) != 2:
        raise HTTPException(400, "Invalid action_id format (type:id)")

    atype, aid = parts[0], int(parts[1])

    if atype == "engagement":
        eng = await session.get(EngagementSession, aid)
        if eng and eng.status == "running":
            _device_id, error = await abort_engagement_on_device(
                account_username=eng.account_username,
                session=session,
                bridge=bridge,
                ws=ws,
                device_id=eng.device_id,
            )
            if error is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=error,
                )
            eng.status = "cancelled"
            eng.finished_at = _utcnow()
            await session.commit()
        return {"status": "cancelled"}

    if atype == "video":
        video = await session.get(Video, aid)
        if video is not None:
            await _cancel_video_record(
                video,
                session=session,
                bridge=bridge,
                ws=ws,
            )
            await session.commit()
        return {"status": "cancelled"}

    raise HTTPException(400, f"Unknown action type: {atype}")


@router.post("/reschedule")
async def reschedule(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, int]:
    """Reset pending schedules so the scheduler recalculates future slots."""
    result = await session.execute(
        select(Video).where(
            Video.status == "pending",
            Video.scheduled_time.isnot(None),
        )
    )
    videos = result.scalars().all()
    count = len(videos)
    for v in videos:
        v.scheduled_time = None
    await session.flush()
    logger.info("Rescheduled %d pending videos by clearing scheduled_time", count)
    return {"rescheduled": count}


@router.post("/clear-all")
async def clear_all(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    bridge: DeviceBridge = Depends(get_bridge),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
) -> dict[str, Any]:
    """Clear queue items after cancelling any phone-side scheduled tasks."""
    from sqlalchemy import delete as _sa_delete
    result = await session.execute(
        select(Video)
        .where(Video.status.in_(_CLEAR_ALL_VIDEO_STATUSES))
        .order_by(Video.id)
    )
    videos = result.scalars().all()
    deletable: list[Video] = []
    errors: list[dict[str, Any]] = []

    for video in videos:
        try:
            if video.status == "cancelled":
                await _release_carousel_usage(video, session)
            elif (
                video.status in _REMOTE_CANCELABLE_VIDEO_STATUSES
                and not video.uploaded_to_phone
                and video.phone_video_id is None
            ):
                video.status = "cancelled"
                video.post_error = video.post_error or "Cleared by admin"
                await _release_carousel_usage(video, session)
            else:
                await _cancel_video_record(
                    video,
                    session=session,
                    bridge=bridge,
                    ws=ws,
                )
            deletable.append(video)
        except HTTPException as exc:
            errors.append({
                "id": video.id,
                "status": video.status,
                "error": str(exc.detail),
            })

    if deletable:
        video_ids = [v.id for v in deletable]
        # Release carousel photo-set tickets in bulk before deleting Videos —
        # PhotoSetUsage.video_id FK has no ON DELETE CASCADE so an unguarded
        # session.delete would trip a SQLite IntegrityError.
        await session.execute(
            _sa_delete(PhotoSetUsage).where(PhotoSetUsage.video_id.in_(video_ids))
        )
    for v in deletable:
        await session.delete(v)
    await session.flush()
    logger.info(
        "Cleared %d videos from queue; skipped %d",
        len(deletable),
        len(errors),
    )
    return {"deleted": len(deletable), "errors": errors}


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_video(
    file: UploadFile = File(...),
    caption: str = Form(""),
    account_username: str | None = Form(None),
    account_id: int | None = Form(None),
    content_type: str = Form("reel"),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Upload a video file and add it to the queue.

    Accepts either ``account_username`` or ``account_id`` to identify the account.
    If the account's device is online, the schedule is automatically pushed
    to the phone via WebSocket.
    """
    # Resolve account: by username or by ID
    account: Account | None = None
    if account_username:
        if ".." in account_username or "/" in account_username or "\\" in account_username:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid username")
        account = (await session.execute(
            select(Account).where(Account.username == account_username),
        )).scalar_one_or_none()
    elif account_id:
        account = (await session.execute(
            select(Account).where(Account.id == account_id),
        )).scalar_one_or_none()
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="account_username or account_id required")

    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if not account.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account is inactive")
    account_username = account.username

    link = (await session.execute(
        select(AccountDevice)
        .join(Device, AccountDevice.device_id == Device.id)
        .where(
            AccountDevice.account_username == account_username,
            Device.is_active == True,  # noqa: E712
        )
        .order_by(AccountDevice.is_primary.desc())
        .limit(1),
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account has no linked device",
        )

    # Validate content_type
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"content_type '{content_type}' not allowed. "
                f"Accepted: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}"
            ),
        )

    # Sanitize filename: strip directory components, reject traversal attempts.
    # Default extension depends on content_type — stories often ship as jpg.
    default_ext = ".jpg" if content_type == "story" else ".mp4"
    raw_name = file.filename or f"upload_{int(time.time())}{default_ext}"
    filename = Path(raw_name).name  # strip directory components
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename",
        )

    # Validate file extension — stories accept both video and image formats,
    # reels accept video only.
    suffix = Path(filename).suffix
    ext = suffix.lower()
    allowed_exts = (
        _ALLOWED_STORY_EXTENSIONS if content_type == "story" else _ALLOWED_VIDEO_EXTENSIONS
    )
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File type '{ext}' not allowed for {content_type}. "
                f"Accepted: {', '.join(sorted(allowed_exts))}"
            ),
        )

    # Save file to data dir. Stories go to data/stories/<user>/ so the
    # existing video_download.py fallback resolver and the scheduler's
    # dispatch path both find the file without a content-type lookup.
    # The phone-side local store (videos/<user>/<filename>) is content-
    # type-agnostic — Android keys on contentType from the schedule payload
    # to route to the right FSM.
    storage_subdir = "stories" if content_type == "story" else "videos"
    upload_dir = Path(config.data_dir) / storage_subdir / account_username
    upload_dir.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid.uuid4().hex}{suffix}"
    filepath = upload_dir / storage_name

    with open(filepath, "wb") as f:
        shutil.copyfileobj(file.file, f)

    video = Video(
        filename=filename,
        original_path=str(filepath),
        account_username=account_username,
        caption=caption,
        content_type=content_type,
        status="pending",
    )
    session.add(video)
    await session.flush()

    # LIFO bump: clear scheduled_time on all OTHER pending videos for this
    # account+content_type so the next auto_schedule tick assigns slots
    # newest-first (per scheduler._schedule_for_account order_by=desc). Without
    # this, the freshly uploaded video lands behind any already-scheduled
    # backlog and the user sees "I just uploaded new content but old posts
    # are still going out". Only pending rows are affected; scheduled /
    # posting / posted rows stay locked in — no in-flight dispatch is
    # disturbed.
    bump_res = await session.execute(
        update(Video)
        .where(
            Video.account_username == account_username,
            Video.content_type == content_type,
            Video.status == "pending",
            Video.id != video.id,
            Video.scheduled_time.is_not(None),
        )
        .values(scheduled_time=None)
        .execution_options(synchronize_session=False)
    )
    if bump_res.rowcount:
        logger.info(
            "[QUEUE] Bumped %d pending %s(s) for @%s — auto_schedule will "
            "reassign slots newest-first",
            bump_res.rowcount, content_type, account_username,
        )

    # Commit BEFORE dispatching to the phone so a late client abort or
    # request cancellation can NOT leave the phone with a
    # carousel_download / video_download command pointing at an
    # uncommitted Video row (Codex Q6 2026-04-13). Once the phone has
    # received the command, the DB row must exist.
    await session.commit()

    # T8 (Codex flag 3): ghost the uploaded file before the phone ever
    # sees it. The scheduler's _dispatch_video runs ghost_media_safe on
    # reels/carousels but this upload path bypasses _dispatch_video
    # entirely (it calls bridge.send_schedule directly below), which
    # meant queue-API uploads previously shipped with the user's
    # original file metadata intact — no fingerprint randomization,
    # visibly different from auto-generated reels on the phone.
    #
    # Run it in a worker thread with a hard wall-clock timeout mirroring
    # the scheduler's 180s budget; failures (ffmpeg missing, bad media,
    # timeout) downgrade gracefully to "dispatch original" the same way
    # the scheduler path does — we never block the upload on a busted
    # ghost pipeline.
    if config.ghost_enabled:
        from server.ghost import ghost_media_safe
        try:
            file_path = Path(video.original_path)
            if file_path.exists():
                await asyncio.wait_for(
                    asyncio.to_thread(ghost_media_safe, str(file_path)),
                    timeout=180,
                )
                logger.info("[QUEUE] Ghosted uploaded video %s", video.filename)
        except asyncio.TimeoutError:
            logger.warning("[QUEUE] Ghost timed out for uploaded video %s", video.filename)
        except Exception:
            logger.exception("[QUEUE] Ghost failed for uploaded video %s", video.filename)

    # Auto-push schedule to phone if device is online
    scheduled = False
    if ws.is_online(link.device_id):
        try:
            # Build nested accounts[].videos[] format (Android contract).
            # Always include contentType so the phone routes to the right
            # FSM (PostingForegroundService switches on it at line 165).
            schedule_payload = {
                "accounts": [
                    {
                        "username": account_username,
                        "videos": [{
                            "filename": video.filename,
                            "scheduledTimeMs": 0,
                            "caption": video.caption or "",
                            "firstComment": "",
                            "contentType": content_type,
                        }],
                    },
                ],
            }
            await bridge.send_schedule(link.device_id, schedule_payload)
            scheduled = True
            logger.info(
                "Auto-pushed schedule to device %d for %s (content_type=%s)",
                link.device_id, account_username, content_type,
            )
        except Exception as exc:
            logger.warning("Failed to auto-push schedule to device %d: %s", link.device_id, exc)

    await _broadcast_queue_update(
        broadcaster,
        video_id=video.id,
        status_value=video.status,
        action="added",
    )

    return {
        "id": video.id,
        "filename": video.filename,
        "account_username": video.account_username,
        "status": video.status,
        "scheduled": scheduled,
    }


@router.post("/{video_id}/cancel")
async def cancel_video(
    video_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    bridge: DeviceBridge = Depends(get_bridge),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
    force: bool = Query(
        False,
        description=(
            "Force-cancel a video stuck in 'uploading' state. The "
            "default safety check refuses to cancel mid-upload to "
            "avoid desync; use force=true when an upload has clearly "
            "hung (phone WS dropped mid-transfer, 409 returned)."
        ),
    ),
) -> dict[str, Any]:
    """Cancel a queued video without desynchronizing device state."""
    video = (await session.execute(
        select(Video).where(Video.id == video_id),
    )).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    await _cancel_video_record(
        video,
        session=session,
        bridge=bridge,
        ws=ws,
        force=force,
    )
    await session.flush()
    # Commit before broadcast so admin WS clients never see a
    # "cancelled" event for state that gets rolled back on a later
    # exception (Codex iter 12 bug hunt 2026-04-14).
    await session.commit()
    await _broadcast_queue_update(
        broadcaster,
        video_id=video.id,
        status_value="cancelled",
        action="cancelled",
    )
    return {"id": video.id, "status": "cancelled"}


@router.post("/{video_id}/post-now")
async def post_now_video(
    video_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Move a pending video to the front of the posting queue."""
    video = (await session.execute(
        select(Video).where(Video.id == video_id),
    )).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if video.status not in _POST_NOWABLE_VIDEO_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot post now video with status '{video.status}'",
        )

    video.scheduled_time = _utcnow()
    await session.flush()
    await session.commit()  # Commit before broadcast (Codex iter 12)
    await _broadcast_queue_update(
        broadcaster,
        video_id=video.id,
        status_value=video.status,
        action="post_now",
    )
    return {
        "id": video.id,
        "status": video.status,
        "scheduled_at": farm_time.isoformat_utc(video.scheduled_time),
    }


@router.post("/{video_id}/retry")
async def retry_video(
    video_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Reset a failed video back to pending."""
    video = (await session.execute(
        select(Video).where(Video.id == video_id),
    )).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if video.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot retry video with status '{video.status}'",
        )
    _reset_retryable_video_state(video)
    video.retry_count += 1
    await session.flush()
    await session.commit()  # Commit before broadcast (Codex iter 12)
    await _broadcast_queue_update(
        broadcaster,
        video_id=video.id,
        status_value="pending",
        action="retried",
    )
    return {"id": video.id, "status": "pending", "retry_count": video.retry_count}


@router.delete("/{video_id}")
async def delete_video(
    video_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Delete a terminal queue item and its stored media file."""
    video = (await session.execute(
        select(Video).where(Video.id == video_id),
    )).scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video not found")
    if video.status not in _DELETABLE_VIDEO_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete video with status '{video.status}'",
        )

    _delete_video_file(video.original_path, config=config)
    # Release the photo-set usage ticket BEFORE deleting the Video
    # row so the WHERE clause can still reference `video.id`.
    await _release_carousel_usage(video, session)
    await session.delete(video)
    await session.flush()
    await _broadcast_queue_update(
        broadcaster,
        video_id=video_id,
        status_value="deleted",
        action="removed",
    )
    return {"id": video.id, "status": "deleted"}


@router.post("/bulk-cancel")
async def bulk_cancel(
    body: BulkIds,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Cancel multiple videos at once."""
    result = await session.execute(
        select(Video).where(
            Video.id.in_(body.ids),
            Video.status.in_(["pending", "failed"]),
        ),
    )
    videos = result.scalars().all()
    cancelled = []
    carousel_ids: list[int] = []
    for v in videos:
        v.status = "cancelled"
        if v.content_type == "carousel":
            carousel_ids.append(v.id)
        cancelled.append(v.id)
    # Single bulk DELETE for all carousel usage tickets at once (Codex
    # Q2 Feature-A review): one round-trip to SQLite regardless of how
    # many videos are in the batch, vs. N DELETE statements in a loop.
    if carousel_ids:
        from sqlalchemy import delete as _sa_delete
        await session.execute(
            _sa_delete(PhotoSetUsage)
            .where(PhotoSetUsage.video_id.in_(carousel_ids))
            # Skip the per-object Python-level identity-map walk
            # (Codex Q5 2026-04-14). The PhotoSetUsage rows being
            # deleted are not referenced anywhere downstream in this
            # session — the bulk-cancel response only reports Video
            # ids — so session coherence is not a concern.
            .execution_options(synchronize_session=False)
        )
    await session.flush()
    return {"cancelled": cancelled, "count": len(cancelled)}


@router.post("/bulk-retry")
async def bulk_retry(
    body: BulkIds,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Retry multiple failed videos at once."""
    result = await session.execute(
        select(Video).where(
            Video.id.in_(body.ids),
            Video.status == "failed",
        ),
    )
    videos = result.scalars().all()
    retried = []
    for v in videos:
        _reset_retryable_video_state(v)
        v.retry_count += 1
        retried.append(v.id)
    await session.flush()
    return {"retried": retried, "count": len(retried)}
