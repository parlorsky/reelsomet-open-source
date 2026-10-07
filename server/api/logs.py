"""Logs API: fetch recent activity/post logs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.activity_utils import engagement_event_time, error_suffix
from server import farm_time
from server.dependencies import get_db_session, require_auth
from server.models import EngagementSession, PostLog

router = APIRouter(prefix="/api/logs", tags=["logs"])


def _search_pattern(search: str | None) -> str | None:
    if not search:
        return None
    text = search.strip()
    return f"%{text}%" if text else None


@router.get("")
@router.get("/recent")
@router.get("/stream")
async def recent_logs(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    level: str | None = Query(None, description="Filter by result (e.g. 'success', 'failed')"),
    activity: str | None = Query(None, description="Activity type: 'post' or 'engagement'"),
    search: str | None = Query(None, description="Filter by message or device id"),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Return recent log entries (post logs + engagement sessions)."""
    items: list[dict[str, Any]] = []
    search_pattern = _search_pattern(search)

    if activity is None or activity == "post":
        stmt = select(PostLog).order_by(PostLog.timestamp.desc())
        if level:
            stmt = stmt.where(PostLog.result == level)
        if search_pattern:
            stmt = stmt.where(or_(
                PostLog.account_username.ilike(search_pattern),
                PostLog.result.ilike(search_pattern),
                PostLog.error_message.ilike(search_pattern),
                cast(PostLog.device_id, String).ilike(search_pattern),
            ))
        rows = (await session.execute(stmt.limit(limit))).scalars().all()
        for post_log in rows:
            message = f"@{post_log.account_username}: {post_log.result}{error_suffix(post_log.error_message)}"
            if post_log.duration_ms:
                message += f" ({post_log.duration_ms}ms)"
            items.append({
                "timestamp": farm_time.isoformat_utc(post_log.timestamp),
                "level": "ERROR" if post_log.result in ("failed", "FAILED") else "INFO",
                "activity": "post",
                "message": message,
                "device": str(post_log.device_id) if post_log.device_id else None,
            })

    if activity is None or activity == "engagement":
        stmt = select(EngagementSession).order_by(EngagementSession.id.desc())
        if level:
            stmt = stmt.where(EngagementSession.status == level)
        if search_pattern:
            stmt = stmt.where(or_(
                EngagementSession.account_username.ilike(search_pattern),
                EngagementSession.status.ilike(search_pattern),
                EngagementSession.error.ilike(search_pattern),
                cast(EngagementSession.device_id, String).ilike(search_pattern),
            ))
        rows = (await session.execute(stmt.limit(limit))).scalars().all()
        for engagement_session in rows:
            event_time = engagement_event_time(engagement_session)
            message = (
                f"@{engagement_session.account_username}: "
                f"{engagement_session.status}{error_suffix(engagement_session.error)}"
            )
            if engagement_session.duration_ms:
                message += f" ({engagement_session.duration_ms}ms)"
            items.append({
                "timestamp": farm_time.isoformat_utc(event_time),
                "level": "ERROR" if engagement_session.status in ("failed", "aborted") else "INFO",
                "activity": "engagement",
                "message": message,
                "device": str(engagement_session.device_id) if engagement_session.device_id else None,
            })

    items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    return items[:limit]


# ---------------------------------------------------------------------------
# Advanced logging (Codex roadmap step 1+5): query farm_logs table
# ---------------------------------------------------------------------------


@router.get("/structured")
async def structured_logs(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    level: str | None = Query(None),
    activity: str | None = Query(None),
    device_id: int | None = Query(None),
    account: str | None = Query(None),
    trace_id: str | None = Query(None),
    since_ms: int | None = Query(None, description="Epoch ms — only return entries after this ts"),
    search: str | None = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """Filtered query of the canonical farm_logs table.

    Distinct from /api/logs/recent which serves the legacy PostLog +
    EngagementSession union for the existing dashboard. This endpoint is
    used by the upgraded LogsPage to drive the trace-centric UI.
    """
    from server.log_store import query_logs
    return await query_logs(
        session,
        limit=limit, offset=offset,
        level=level, activity=activity,
        device_id=device_id, account=account,
        trace_id=trace_id, since_ms=since_ms, search=search,
    )


@router.get("/trace/{trace_id}")
async def trace_logs(
    trace_id: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(1000, ge=1, le=5000),
) -> list[dict[str, Any]]:
    """All farm_logs entries for a single trace, chronological order."""
    from server.log_store import query_trace
    return await query_trace(session, trace_id, limit=limit)
