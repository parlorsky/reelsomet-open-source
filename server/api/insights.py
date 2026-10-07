"""Insights API: account insights snapshots, CSV export, collection trigger."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import io
import logging
import re
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.dependencies import get_bridge, get_db_session, get_ws_manager, require_auth
from server.insights_watermark import strip_watermark
from server.models import (
    Account,
    AccountDevice,
    Device,
    InsightsCollectionPlan,
    InsightsSnapshot,
    Video,
)
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/insights", tags=["insights"])


async def _resolve_account_username(session: AsyncSession, account_ref: str) -> str | None:
    """Resolve either a numeric account ID or a username to a username."""
    if account_ref.isdigit():
        acct = (await session.execute(
            select(Account).where(Account.id == int(account_ref)),
        )).scalar_one_or_none()
        return acct.username if acct is not None else None
    return account_ref


def _serialize_datetime(value: datetime | None) -> str | None:
    """Serialize datetimes consistently for the insights API."""
    return value.isoformat() if value else None


def _snapshot_timestamp(snapshot: InsightsSnapshot) -> datetime | None:
    """Return the best timestamp available for ordering a snapshot."""
    return snapshot.collected_at or snapshot.created_at


def _snapshot_sort_key(snapshot: InsightsSnapshot) -> tuple[datetime, int]:
    """Return a stable chronological key for snapshot history."""
    return (_snapshot_timestamp(snapshot) or datetime.min, snapshot.id or 0)


def _closest_snapshot(
    snapshots: list[InsightsSnapshot],
    target: datetime,
) -> InsightsSnapshot | None:
    """Return the snapshot closest to the target time."""
    candidates = [snapshot for snapshot in snapshots if _snapshot_timestamp(snapshot) is not None]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda snapshot: abs((_snapshot_timestamp(snapshot) - target).total_seconds()),
    )


def _non_negative_delta(current: int | None, previous: int | None) -> int:
    """Return a counter delta clamped at zero."""
    return max((current or 0) - (previous or 0), 0)


def _parse_iso_datetime_param(value: str | None, field_name: str) -> datetime | None:
    """Parse an ISO query param into the app's naive UTC storage format."""
    if value is None:
        return None
    raw_value = value.strip()
    if not raw_value:
        raise HTTPException(status_code=400, detail=f"Invalid ISO datetime for '{field_name}'")
    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ISO datetime for '{field_name}'") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _serialize_snapshot(snapshot: InsightsSnapshot) -> dict[str, Any]:
    """Return the frontend payload shape for a single insights snapshot."""
    collected_at_iso = _serialize_datetime(snapshot.collected_at)
    return {
        "id": snapshot.id,
        "account_username": snapshot.account_username,
        "video_id": snapshot.video_id,
        "caption_snippet": snapshot.caption_snippet,
        "reel_position": snapshot.reel_position,
        "plays": snapshot.plays,
        "likes": snapshot.likes,
        "comments": snapshot.comments,
        "shares": snapshot.shares,
        "saves": snapshot.saves,
        "reposts": snapshot.reposts,
        "reach": snapshot.reach,
        "engaged": snapshot.engaged,
        "profile_visits": snapshot.profile_visits,
        "follows": snapshot.follows,
        "watch_time_seconds": snapshot.watch_time_seconds,
        "avg_watch_time_seconds": snapshot.avg_watch_time_seconds,
        "skip_rate_percent": snapshot.skip_rate_percent,
        "followers_percent": snapshot.followers_percent,
        "non_followers_percent": snapshot.non_followers_percent,
        "collected_at": collected_at_iso,
        "timestamp": collected_at_iso,
        "created_at": _serialize_datetime(snapshot.created_at),
        "followers": 0,
        "following": 0,
        "posts": 0,
    }


def _trend_bucket_frame(
    window: Literal["24h", "7d", "30d"],
    now: datetime,
) -> tuple[list[dict[str, Any]], datetime, float]:
    """Build empty trend buckets and return their frame metadata."""
    if window == "24h":
        bucket_span = timedelta(hours=1)
        bucket_count = 24
        end_anchor = now.replace(minute=0, second=0, microsecond=0) + bucket_span
    else:
        bucket_span = timedelta(days=1)
        bucket_count = 7 if window == "7d" else 30
        end_anchor = now.replace(hour=0, minute=0, second=0, microsecond=0) + bucket_span

    start_anchor = end_anchor - (bucket_span * bucket_count)
    buckets: list[dict[str, Any]] = []
    for index in range(bucket_count):
        bucket_start = start_anchor + (bucket_span * index)
        bucket_end = bucket_start + bucket_span
        buckets.append({
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "plays_delta": 0,
            "likes_delta": 0,
            "comments_delta": 0,
            "shares_delta": 0,
            "saves_delta": 0,
            "reach_delta": 0,
        })
    return buckets, start_anchor, bucket_span.total_seconds()


def _bucket_index_for_timestamp(
    timestamp: datetime,
    start_anchor: datetime,
    bucket_seconds: float,
    bucket_count: int,
) -> int | None:
    """Map a timestamp into the bucket frame."""
    delta_seconds = (timestamp - start_anchor).total_seconds()
    if delta_seconds < 0:
        return None
    bucket_index = int(delta_seconds // bucket_seconds)
    if bucket_index < 0 or bucket_index >= bucket_count:
        return None
    return bucket_index


def _serialize_trend_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    """Serialize an internal trend bucket."""
    return {
        "bucket_start": _serialize_datetime(bucket["bucket_start"]),
        "bucket_end": _serialize_datetime(bucket["bucket_end"]),
        "plays_delta": bucket["plays_delta"],
        "likes_delta": bucket["likes_delta"],
        "comments_delta": bucket["comments_delta"],
        "shares_delta": bucket["shares_delta"],
        "saves_delta": bucket["saves_delta"],
        "reach_delta": bucket["reach_delta"],
    }


@router.get("/accounts")
async def insights_accounts(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return accounts with their snapshot counts."""
    result = await session.execute(
        select(Account).where(Account.is_active == True).order_by(Account.username),  # noqa: E712
    )
    accounts = result.scalars().all()
    out = []
    for account in accounts:
        snap_count = (await session.execute(
            select(func.count(InsightsSnapshot.id)).where(
                InsightsSnapshot.account_username == account.username,
            ),
        )).scalar_one()
        out.append({
            "id": account.id,
            "username": account.username,
            "insights_enabled": account.insights_enabled,
            "snapshot_count": snap_count,
            "last_insights_at": _serialize_datetime(account.last_insights_at),
        })
    return out


@router.get("/summary")
async def insights_summary(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return aggregate insights summary stats across all accounts."""
    totals = (await session.execute(
        select(
            func.sum(InsightsSnapshot.plays),
            func.sum(InsightsSnapshot.likes),
            func.sum(InsightsSnapshot.comments),
            func.count(InsightsSnapshot.id),
        )
    )).one()
    total_plays = totals[0] or 0
    total_likes = totals[1] or 0
    total_comments = totals[2] or 0
    reels_tracked = totals[3] or 0

    avg_er = 0.0
    if total_plays > 0:
        avg_er = round((total_likes + total_comments) / total_plays * 100, 2)

    last_collected = (await session.execute(
        select(InsightsSnapshot.collected_at)
        .order_by(InsightsSnapshot.collected_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    enabled_count = (await session.execute(
        select(func.count(Account.id)).where(
            Account.is_active == True,  # noqa: E712
            Account.insights_enabled == True,  # noqa: E712
        )
    )).scalar_one()

    return {
        "total_plays": total_plays,
        "total_likes": total_likes,
        "total_comments": total_comments,
        "reels_tracked": reels_tracked,
        "avg_er_percent": avg_er,
        "last_collected_at": _serialize_datetime(last_collected),
        "accounts_enabled": enabled_count,
    }


@router.get("/{account_username}/videos")
async def insights_videos_for_account(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    include_retired: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Return paginated video insights for an account."""
    filters = [Account.username == account_username, Video.account_username == account_username]
    if not include_retired:
        filters.append(Video.insights_retired == False)  # noqa: E712

    total = (await session.execute(
        select(func.count(Video.id))
        .select_from(Video)
        .join(Account, Account.username == Video.account_username)
        .where(*filters),
    )).scalar_one()

    result = await session.execute(
        select(Video, InsightsCollectionPlan)
        .join(Account, Account.username == Video.account_username)
        .outerjoin(InsightsCollectionPlan, InsightsCollectionPlan.video_id == Video.id)
        .where(*filters)
        .order_by(Video.posted_at.is_(None), Video.posted_at.desc(), Video.id.desc())
        .limit(limit)
        .offset(offset),
    )
    rows = result.all()
    if not rows:
        return {"account_username": account_username, "total": total, "videos": []}

    video_ids = [video.id for video, _plan in rows]
    snapshots_result = await session.execute(
        select(InsightsSnapshot)
        .where(InsightsSnapshot.video_id.in_(video_ids))
        .order_by(
            InsightsSnapshot.video_id,
            InsightsSnapshot.collected_at.asc(),
            InsightsSnapshot.created_at.asc(),
            InsightsSnapshot.id.asc(),
        ),
    )
    snapshots_by_video_id: dict[int, list[InsightsSnapshot]] = defaultdict(list)
    for snapshot in snapshots_result.scalars():
        if snapshot.video_id is not None:
            snapshots_by_video_id[snapshot.video_id].append(snapshot)

    now = datetime.utcnow()
    videos: list[dict[str, Any]] = []
    for video, plan in rows:
        history = sorted(snapshots_by_video_id.get(video.id, []), key=_snapshot_sort_key)
        latest_snapshot = history[-1] if history else None
        baseline_24h = _closest_snapshot(history, now - timedelta(hours=24))
        baseline_7d = _closest_snapshot(history, now - timedelta(days=7))
        latest_plays = latest_snapshot.plays if latest_snapshot and latest_snapshot.plays is not None else None
        plays_delta_24h = 0
        plays_delta_7d = 0
        if latest_plays is not None:
            plays_delta_24h = _non_negative_delta(
                latest_plays,
                baseline_24h.plays if baseline_24h else latest_plays,
            )
            plays_delta_7d = _non_negative_delta(
                latest_plays,
                baseline_7d.plays if baseline_7d else latest_plays,
            )

        videos.append({
            "video_id": video.id,
            "filename": video.filename,
            "caption": strip_watermark(video.caption),
            "content_type": video.content_type,
            "posted_at": _serialize_datetime(video.posted_at),
            "insights_retired": video.insights_retired,
            "insights_last_collected_at": _serialize_datetime(video.insights_last_collected_at),
            "insights_collection_count": video.insights_collection_count,
            "next_due_at": _serialize_datetime(plan.next_due_at if plan else None),
            "priority_score": plan.priority_score if plan else None,
            "metrics": {
                "plays": latest_snapshot.plays if latest_snapshot else None,
                "likes": latest_snapshot.likes if latest_snapshot else None,
                "comments": latest_snapshot.comments if latest_snapshot else None,
                "shares": latest_snapshot.shares if latest_snapshot else None,
                "saves": latest_snapshot.saves if latest_snapshot else None,
                "reach": latest_snapshot.reach if latest_snapshot else None,
                "skip_rate_percent": latest_snapshot.skip_rate_percent if latest_snapshot else None,
                "avg_watch_time_seconds": (
                    latest_snapshot.avg_watch_time_seconds if latest_snapshot else None
                ),
            },
            "plays_delta_24h": plays_delta_24h,
            "plays_delta_7d": plays_delta_7d,
            "sparkline": [
                {
                    "t": _serialize_datetime(_snapshot_timestamp(snapshot)),
                    "plays": snapshot.plays,
                }
                for snapshot in sorted(history, key=_snapshot_sort_key, reverse=True)[:20]
            ],
        })

    return {"account_username": account_username, "total": total, "videos": videos}


@router.get("/{account_username}/trends")
async def account_insights_trends(
    account_username: str,
    window: Literal["24h", "7d", "30d"] = Query(...),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return account-level insights deltas bucketed over a time window."""
    now = datetime.utcnow()
    buckets, start_anchor, bucket_seconds = _trend_bucket_frame(window, now)

    result = await session.execute(
        select(InsightsSnapshot)
        .join(Video, Video.id == InsightsSnapshot.video_id)
        .join(Account, Account.username == Video.account_username)
        .where(Account.username == account_username)
        .order_by(
            InsightsSnapshot.video_id,
            InsightsSnapshot.collected_at.asc(),
            InsightsSnapshot.created_at.asc(),
            InsightsSnapshot.id.asc(),
        ),
    )
    snapshots_by_video_id: dict[int, list[InsightsSnapshot]] = defaultdict(list)
    for snapshot in result.scalars():
        if snapshot.video_id is not None:
            snapshots_by_video_id[snapshot.video_id].append(snapshot)

    for history in snapshots_by_video_id.values():
        ordered = sorted(history, key=_snapshot_sort_key)
        for previous, current in zip(ordered, ordered[1:]):
            later_timestamp = _snapshot_timestamp(current)
            if later_timestamp is None:
                continue
            bucket_index = _bucket_index_for_timestamp(
                later_timestamp,
                start_anchor,
                bucket_seconds,
                len(buckets),
            )
            if bucket_index is None:
                continue
            bucket = buckets[bucket_index]
            bucket["plays_delta"] += _non_negative_delta(current.plays, previous.plays)
            bucket["likes_delta"] += _non_negative_delta(current.likes, previous.likes)
            bucket["comments_delta"] += _non_negative_delta(current.comments, previous.comments)
            bucket["shares_delta"] += _non_negative_delta(current.shares, previous.shares)
            bucket["saves_delta"] += _non_negative_delta(current.saves, previous.saves)
            bucket["reach_delta"] += _non_negative_delta(current.reach, previous.reach)

    return {
        "account_username": account_username,
        "window": window,
        "buckets": [_serialize_trend_bucket(bucket) for bucket in buckets],
    }


@router.get("/{account_ref}/snapshots")
@router.get("/{account_ref}")
async def insights_for_account(
    account_ref: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=500),
    from_dt: str | None = Query(None, alias="from"),
    to_dt: str | None = Query(None, alias="to"),
) -> list[dict[str, Any]]:
    """Return insights snapshots for a specific account (by username or ID)."""
    username = await _resolve_account_username(session, account_ref)
    if username is None:
        return []

    parsed_from = _parse_iso_datetime_param(from_dt, "from")
    parsed_to = _parse_iso_datetime_param(to_dt, "to")
    filters = [InsightsSnapshot.account_username == username]
    if parsed_from is not None:
        filters.append(InsightsSnapshot.collected_at >= parsed_from)
    if parsed_to is not None:
        filters.append(InsightsSnapshot.collected_at <= parsed_to)

    result = await session.execute(
        select(InsightsSnapshot)
        .where(*filters)
        .order_by(InsightsSnapshot.created_at.desc())
        .limit(limit),
    )
    snapshots = result.scalars().all()
    return [_serialize_snapshot(snapshot) for snapshot in snapshots]


@router.get("/{account_ref}/latest")
async def latest_snapshot_for_account(
    account_ref: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return the latest insights snapshot for a specific account (by username or ID)."""
    username = await _resolve_account_username(session, account_ref)
    if username is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    snapshot = (await session.execute(
        select(InsightsSnapshot)
        .where(InsightsSnapshot.account_username == username)
        .order_by(InsightsSnapshot.created_at.desc())
        .limit(1),
    )).scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No insights snapshots found")

    return _serialize_snapshot(snapshot)


@router.get("/{account_username}/export")
async def insights_export_csv(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """Export insights snapshots for an account as CSV."""
    result = await session.execute(
        select(InsightsSnapshot)
        .where(InsightsSnapshot.account_username == account_username)
        .order_by(InsightsSnapshot.created_at.desc()),
    )
    snapshots = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "account", "video_id", "caption", "reel_position",
        "plays", "likes", "comments", "shares", "saves", "reposts",
        "reach", "engaged", "profile_visits", "follows",
        "watch_time_s", "avg_watch_time_s", "skip_rate_pct",
        "followers_pct", "non_followers_pct", "collected_at", "created_at",
    ])
    for snapshot in snapshots:
        writer.writerow([
            snapshot.id,
            snapshot.account_username,
            snapshot.video_id,
            snapshot.caption_snippet,
            snapshot.reel_position,
            snapshot.plays,
            snapshot.likes,
            snapshot.comments,
            snapshot.shares,
            snapshot.saves,
            snapshot.reposts,
            snapshot.reach,
            snapshot.engaged,
            snapshot.profile_visits,
            snapshot.follows,
            snapshot.watch_time_seconds,
            snapshot.avg_watch_time_seconds,
            snapshot.skip_rate_percent,
            snapshot.followers_percent,
            snapshot.non_followers_percent,
            _serialize_datetime(snapshot.collected_at) or "",
            _serialize_datetime(snapshot.created_at) or "",
        ])
    output.seek(0)
    safe_username = re.sub(r"[^\w.\-]", "_", account_username)
    filename = f"insights_{safe_username}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
        },
    )


class InsightsToggle(BaseModel):
    enabled: bool


@router.post("/toggle")
async def toggle_insights(
    body: InsightsToggle,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Enable or disable insights globally (updates all active accounts)."""
    result = await session.execute(
        select(Account).where(Account.is_active == True),  # noqa: E712
    )
    accounts = result.scalars().all()
    count = 0
    for account in accounts:
        account.insights_enabled = body.enabled
        count += 1
    await session.flush()
    return {"enabled": body.enabled, "accounts_updated": count}


@router.post("/{account_username}/collect")
async def collect_insights(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Trigger insights collection on the phone for an account.

    The device must be online; returns 409 if it is not.
    """
    account = (await session.execute(
        select(Account).where(Account.username == account_username),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if not account.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account is inactive")

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
    if link is None or not ws.is_online(link.device_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Device offline",
        )

    payload = {
        "accounts": [
            {
                "username": account_username,
                "knownVideoIds": [],
                "skipReels": 0,
            },
        ],
        "maxReelsPerAccount": account.insights_max_reels or 10,
    }
    await bridge.start_insights(link.device_id, payload)
    logger.info("Started insights collection for %s on device %d", account_username, link.device_id)
    return {"message": "Insights collection started", "device_id": link.device_id}
