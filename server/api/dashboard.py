"""Dashboard API: overview stats, recent activity."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.activity_utils import engagement_event_time, error_suffix
from server import farm_time
from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, get_ws_manager, require_auth
from server.models import Account, Device, EngagementSession, PostLog, Video
from server.ws.manager import DeviceConnectionManager


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
async def dashboard_stats(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Return aggregate counts for the dashboard overview."""
    device_count = (await session.execute(
        select(func.count(Device.id)).where(Device.is_active == True),  # noqa: E712
    )).scalar_one()

    account_count = (await session.execute(
        select(func.count(Account.id)).where(Account.is_active == True),  # noqa: E712
    )).scalar_one()

    video_counts: dict[str, int] = {}
    for s in ("pending", "posted", "failed", "cancelled"):
        cnt = (await session.execute(
            select(func.count(Video.id)).where(Video.status == s),
        )).scalar_one()
        video_counts[s] = cnt

    total_videos = (await session.execute(select(func.count(Video.id)))).scalar_one()

    # Today's stats — anchored on the farm's local midnight, not UTC,
    # so dashboard counters align with the operator's sense of "today"
    # on UTC+N deployments (Codex iter 4 bug hunt 2026-04-14).
    today_start, _today_end = farm_time.local_day_bounds_utc(
        _utcnow(), config.farm_timezone,
    )
    posts_today = (await session.execute(
        select(func.count(Video.id)).where(
            Video.status == "posted",
            Video.posted_at >= today_start,
        ),
    )).scalar_one()
    errors_today = (await session.execute(
        select(func.count(Video.id)).where(
            Video.status == "failed",
            Video.updated_at >= today_start,
        ),
    )).scalar_one()
    engagement_today = (await session.execute(
        select(func.count(EngagementSession.id)).where(
            EngagementSession.started_at >= today_start,
        ),
    )).scalar_one()

    return {
        "devices_total": device_count,
        "devices_online": len(ws.get_online_device_ids()),
        "accounts_active": account_count,
        "accounts_total": (await session.execute(select(func.count(Account.id)))).scalar_one(),
        "posts_today": posts_today,
        "posts_pending": video_counts.get("pending", 0),
        "engagement_sessions_today": engagement_today,
        "errors_today": errors_today,
    }


@router.get("/activity")
async def dashboard_activity(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Return recent post logs and engagement sessions sorted by event time."""
    post_result = await session.execute(
        select(PostLog)
        .order_by(PostLog.timestamp.desc())
        .limit(limit),
    )
    logs = post_result.scalars().all()

    eng_result = await session.execute(
        select(EngagementSession)
        .order_by(EngagementSession.started_at.desc())
        .limit(limit),
    )
    eng_sessions = eng_result.scalars().all()

    items: list[dict[str, Any]] = []

    for log in logs:
        items.append({
            "timestamp": farm_time.isoformat_utc(log.timestamp),
            "activity": "POSTING",
            "message": f"@{log.account_username}: {log.result}{error_suffix(log.error_message)}",
            "device": str(log.device_id) if log.device_id else None,
            "level": "ERROR" if log.result in ("failed", "FAILED") else "INFO",
        })

    for session_row in eng_sessions:
        event_time = engagement_event_time(session_row)
        actions_parts: list[str] = []
        if session_row.total_likes:
            actions_parts.append(f"{session_row.total_likes} likes")
        if session_row.total_comments:
            actions_parts.append(f"{session_row.total_comments} comments")
        if session_row.total_replies:
            actions_parts.append(f"{session_row.total_replies} replies")
        if session_row.total_shares:
            actions_parts.append(f"{session_row.total_shares} shares")
        if session_row.total_follows:
            actions_parts.append(f"{session_row.total_follows} follows")
        actions_str = ", ".join(actions_parts) if actions_parts else "no actions"

        if session_row.status == "running":
            message = f"@{session_row.account_username}: engagement running ({actions_str})"
            level = "INFO"
        elif session_row.status == "completed":
            message = f"@{session_row.account_username}: engagement completed ({actions_str})"
            level = "INFO"
        elif session_row.status in ("failed", "aborted"):
            message = f"@{session_row.account_username}: engagement {session_row.status} ({actions_str})"
            level = "WARNING" if session_row.status == "aborted" else "ERROR"
        else:
            message = f"@{session_row.account_username}: engagement {session_row.status}"
            level = "INFO"

        items.append({
            "timestamp": farm_time.isoformat_utc(event_time),
            "activity": "ENGAGEMENT",
            "message": message,
            "device": str(session_row.device_id) if session_row.device_id else None,
            "level": level,
        })

    items.sort(key=lambda item: item["timestamp"] or "", reverse=True)
    return items[:limit]
