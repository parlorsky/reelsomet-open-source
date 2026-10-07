"""Engagement API: status, targets, sessions, actions."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_bridge, get_config, get_db_session, get_ws_manager, require_auth
from server.device_actions import abort_engagement_on_device
from server.models import (
    Account,
    AccountDevice,
    Device,
    EngagementAction,
    EngagementSession,
    EngagementTarget,
)
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


def _build_public_url(config: VPSConfig) -> str:
    """Build the public URL that phones can reach for API calls.

    IP addresses use ``http://<ip>:8443`` (Caddy HTTP on alt port).
    Real domains use ``https://<domain>`` (Caddy auto-TLS on 443).
    """
    if not config.domain:
        return ""
    import re
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
        return f"http://{config.domain}:8443"
    return f"https://{config.domain}"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engagement", tags=["engagement"])


_INSTAGRAM_RESERVED_SEGMENTS = {"p", "reel", "reels", "stories", "explore", "tv"}


def _active_engagement_targets_query() -> Any:
    """Return engagement targets whose backing account is still active."""
    return (
        select(EngagementTarget)
        .join(Account, Account.username == EngagementTarget.account_username)
        .where(Account.is_active.is_(True))
    )


def _target_probabilities(account: Account | None) -> dict[str, float]:
    """Return the effective account-level engagement probabilities."""
    return {
        "like_probability": account.engagement_like_prob if account and account.engagement_like_prob is not None else 0.7,
        "comment_probability": (
            account.engagement_comment_prob
            if account and account.engagement_comment_prob is not None
            else 0.3
        ),
        "reply_probability": account.engagement_reply_prob if account and account.engagement_reply_prob is not None else 0.1,
        "share_probability": account.engagement_share_prob if account and account.engagement_share_prob is not None else 0.05,
    }


def _daily_budget_minutes(account: Account) -> int:
    """Return the effective engagement daily budget for an account."""
    return account.engagement_daily_budget if account.engagement_daily_budget is not None else 30


def _normalize_target_username(value: str) -> str:
    """Normalize target input from raw usernames or Instagram profile URLs."""
    normalized = value.strip()
    if not normalized:
        raise HTTPException(400, "channel_url or target_username required")

    candidate = normalized
    lower_value = normalized.lower()
    if "instagram.com/" in lower_value or lower_value.startswith("instagram.com") or lower_value.startswith("www.instagram.com"):
        parsed = urlparse(normalized if "://" in normalized else f"https://{normalized.lstrip('/')}")
        host = parsed.netloc.lower()
        if host == "instagram.com" or host.endswith(".instagram.com"):
            segments = [segment for segment in parsed.path.split("/") if segment]
            if not segments or segments[0].lower() in _INSTAGRAM_RESERVED_SEGMENTS:
                raise HTTPException(400, "Instagram profile URL required")
            candidate = segments[0]

    candidate = candidate.lstrip("@").strip()
    if not candidate or candidate.lower() in _INSTAGRAM_RESERVED_SEGMENTS:
        raise HTTPException(400, "channel_url or target_username required")
    return candidate


def _canonical_target_username(value: str) -> str:
    """Best-effort canonical form for comparing against stored legacy target values."""
    try:
        return _normalize_target_username(value)
    except HTTPException:
        return value.strip().lstrip("@").strip()


async def _ensure_unique_target(
    session: AsyncSession,
    *,
    account_username: str,
    target_username: str,
) -> None:
    """Reject duplicate active engagement targets per account after normalization."""
    existing_usernames = (await session.execute(
        select(EngagementTarget.target_username).where(
            EngagementTarget.account_username == account_username,
            EngagementTarget.is_active == True,  # noqa: E712
        ),
    )).scalars().all()
    for existing_username in existing_usernames:
        if _canonical_target_username(existing_username).lower() == target_username.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Engagement target '{target_username}' already exists for @{account_username}",
            )


async def _resolve_flat_target_account(session: AsyncSession, account_username: str) -> Account:
    """Resolve the account for the flat target flow with stable defaults."""
    normalized_username = account_username.strip()
    if normalized_username:
        account = (await session.execute(
            select(Account).where(Account.username == normalized_username),
        )).scalar_one_or_none()
        if account is None:
            raise HTTPException(404, "Account not found")
        if not account.is_active:
            raise HTTPException(409, "Account is inactive")
        return account

    account = (await session.execute(
        select(Account)
        .join(AccountDevice, AccountDevice.account_username == Account.username)
        .join(Device, Device.id == AccountDevice.device_id)
        .where(Account.is_active.is_(True))
        .where(Device.is_active.is_(True))
        .order_by(Account.username, AccountDevice.is_primary.desc(), AccountDevice.id)
        .limit(1),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(400, "No active accounts with linked devices - specify account_username")
    return account


class TargetCreate(BaseModel):
    target_username: str = ""
    channel_url: str = ""  # alias for frontend
    account_username: str = ""
    max_reels: int = 5
    should_follow: bool = True
    like_probability: float = 0.7
    comment_probability: float = 0.3
    reply_probability: float = 0.1
    share_probability: float = 0.05


class TargetUpdate(BaseModel):
    enabled: bool | None = None
    max_reels: int | None = None
    should_follow: bool | None = None


@router.get("/status")
async def engagement_status(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Return global engagement status across all accounts."""
    enabled_count = (await session.execute(
        select(func.count(Account.id)).where(
            Account.engagement_enabled == True,  # noqa: E712
            Account.is_active == True,  # noqa: E712
        ),
    )).scalar_one()

    running_sessions = (await session.execute(
        select(func.count(EngagementSession.id)).where(
            EngagementSession.status == "running",
        ),
    )).scalar_one()

    total_actions = (await session.execute(
        select(func.count(EngagementAction.id)),
    )).scalar_one()

    # Find current running session account
    running_session = (await session.execute(
        select(EngagementSession).where(EngagementSession.status == "running").limit(1),
    )).scalar_one_or_none()

    # Operator-timezone aware day boundary for dashboard counters.
    from server import farm_time
    today_start, _today_end = farm_time.local_day_bounds_utc(
        farm_time.utcnow_naive(), getattr(config, "farm_timezone", "UTC"),
    )
    sessions_today = (await session.execute(
        select(func.count(EngagementSession.id)).where(
            EngagementSession.started_at >= today_start,
        ),
    )).scalar_one()

    return {
        "accounts_enabled": enabled_count,
        "running": running_sessions > 0,
        "running_sessions": running_sessions,
        "current_account": running_session.account_username if running_session else None,
        "sessions_today": sessions_today,
        "total_actions": total_actions,
    }


@router.get("/targets")
async def list_all_targets(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return all engagement targets across all accounts."""
    result = await session.execute(
        _active_engagement_targets_query().order_by(EngagementTarget.id),
    )
    targets = result.scalars().all()
    account_probs: dict[str, dict[str, float]] = {}
    acct_rows = (await session.execute(select(Account))).scalars().all()
    for a in acct_rows:
        account_probs[a.username] = _target_probabilities(a)

    return [
        {
            "id": t.id,
            "account_username": t.account_username,
            "channel_url": t.target_username,
            "target_username": t.target_username,
            "enabled": t.is_active,
            "max_reels": t.max_reels,
            "should_follow": t.should_follow,
            **account_probs.get(t.account_username, _target_probabilities(None)),
        }
        for t in targets
    ]


@router.get("/sessions")
async def list_all_sessions(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return recent engagement sessions across all accounts."""
    result = await session.execute(
        select(EngagementSession).order_by(EngagementSession.id.desc()).limit(limit),
    )
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "account_username": s.account_username,
            "device_id": s.device_id,
            "status": s.status,
            "target_channel": f"{s.channels_visited or 0} channels",
            "channels_visited": s.channels_visited,
            "reels_watched": s.reels_watched,
            "likes_given": s.total_likes,
            "comments_given": s.total_comments,
            "replies_given": s.total_replies,
            "shares_given": s.total_shares,
            "total_likes": s.total_likes,
            "total_comments": s.total_comments,
            "total_follows": s.total_follows,
            "duration_ms": s.duration_ms,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "ended_at": s.finished_at.isoformat() if s.finished_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in sessions
    ]


@router.post("/targets", status_code=status.HTTP_201_CREATED)
async def create_target_flat(
    body: TargetCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create an engagement target (flat endpoint for frontend)."""
    username = _normalize_target_username(body.channel_url or body.target_username)
    account = await _resolve_flat_target_account(session, body.account_username)
    await _ensure_unique_target(
        session,
        account_username=account.username,
        target_username=username,
    )

    target = EngagementTarget(
        target_username=username,
        account_username=account.username,
        max_reels=body.max_reels,
        should_follow=body.should_follow,
        is_active=True,
    )
    session.add(target)
    await session.flush()
    return {
        "id": target.id,
        "channel_url": target.target_username,
        "target_username": target.target_username,
        "account_username": target.account_username,
        "enabled": target.is_active,
        "max_reels": target.max_reels,
        "should_follow": target.should_follow,
        **_target_probabilities(account),
    }


@router.patch("/targets/{target_id}")
async def update_target_flat(
    target_id: int,
    body: TargetUpdate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Update an engagement target by ID."""
    target = (await session.execute(
        select(EngagementTarget).where(EngagementTarget.id == target_id),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Target not found")
    if body.enabled is not None:
        target.is_active = body.enabled
    if body.max_reels is not None:
        target.max_reels = body.max_reels
    if body.should_follow is not None:
        target.should_follow = body.should_follow
    await session.commit()
    return {"status": "ok"}


@router.delete("/targets/{target_id}")
async def delete_target_flat(
    target_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    """Delete an engagement target by ID."""
    target = (await session.execute(
        select(EngagementTarget).where(EngagementTarget.id == target_id),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Target not found")
    await session.delete(target)
    await session.commit()
    return {"status": "ok"}


@router.post("/abort-all")
async def abort_all_engagement(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Abort ALL running engagement sessions and send abort to ALL online devices."""
    running = (await session.execute(
        select(EngagementSession).where(EngagementSession.status == "running"),
    )).scalars().all()

    aborted = 0
    aborted_devices: set[int] = set()
    skipped: list[dict[str, Any]] = []
    for s in running:
        device_id, error = await abort_engagement_on_device(
            account_username=s.account_username,
            session=session,
            bridge=bridge,
            ws=ws,
            device_id=s.device_id,
        )
        if error is not None:
            skipped.append({
                "session_id": s.id,
                "account_username": s.account_username,
                "reason": error,
            })
            continue
        s.status = "aborted"
        s.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        aborted += 1
        if device_id is not None:
            aborted_devices.add(device_id)

    await session.commit()
    return {
        "sessions_aborted": aborted,
        "devices_aborted": len(aborted_devices),
        "sessions_skipped": skipped,
    }


class EngagementToggle(BaseModel):
    enabled: bool


@router.post("/toggle")
async def toggle_engagement(
    body: EngagementToggle,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Enable or disable engagement globally (updates all active accounts)."""
    result = await session.execute(
        select(Account).where(Account.is_active == True),  # noqa: E712
    )
    accounts = result.scalars().all()
    count = 0
    for acc in accounts:
        acc.engagement_enabled = body.enabled
        count += 1
    await session.flush()
    return {"enabled": body.enabled, "accounts_updated": count}


@router.get("/accounts-overview")
async def accounts_overview(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return per-account engagement aggregates: target counts, recent session
    activity, cumulative action totals for 24h and 7d windows.

    One row per active account. Rows include accounts with zero targets so
    the UI can prompt the user to add some.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    # Active accounts — base rows
    accounts = (await session.execute(
        select(Account).where(Account.is_active == True).order_by(Account.username),  # noqa: E712
    )).scalars().all()

    # Target counts per account (total vs active)
    target_counts_rows = (await session.execute(
        select(
            EngagementTarget.account_username,
            func.count(EngagementTarget.id).label("total"),
            func.sum(
                case((EngagementTarget.is_active == True, 1), else_=0)  # noqa: E712
            ).label("active"),
        ).group_by(EngagementTarget.account_username),
    )).all()
    target_counts = {
        r.account_username: {"total": int(r.total or 0), "active": int(r.active or 0)}
        for r in target_counts_rows
    }

    # Last session per account
    last_session_rows = (await session.execute(
        select(
            EngagementSession.account_username,
            func.max(EngagementSession.started_at).label("last_started"),
        ).group_by(EngagementSession.account_username),
    )).all()
    last_session = {r.account_username: r.last_started for r in last_session_rows}

    # Running session id per account (status='running')
    running_rows = (await session.execute(
        select(EngagementSession.account_username, EngagementSession.id).where(
            EngagementSession.status == "running",
        ),
    )).all()
    running_id = {r.account_username: r.id for r in running_rows}

    # Cumulative totals per window, aggregated across sessions that STARTED
    # inside the window (simple + predictable).
    def _window_query(cutoff: datetime) -> Any:
        return select(
            EngagementSession.account_username,
            func.coalesce(func.sum(EngagementSession.total_likes), 0).label("likes"),
            func.coalesce(func.sum(EngagementSession.total_comments), 0).label("comments"),
            func.coalesce(func.sum(EngagementSession.total_replies), 0).label("replies"),
            func.coalesce(func.sum(EngagementSession.total_shares), 0).label("shares"),
            func.coalesce(func.sum(EngagementSession.reels_watched), 0).label("reels"),
            func.count(EngagementSession.id).label("sessions"),
        ).where(
            EngagementSession.started_at >= cutoff,
        ).group_by(EngagementSession.account_username)

    rows_24h = (await session.execute(_window_query(cutoff_24h))).all()
    rows_7d = (await session.execute(_window_query(cutoff_7d))).all()
    agg_24h = {r.account_username: r for r in rows_24h}
    agg_7d = {r.account_username: r for r in rows_7d}

    def _empty_window() -> dict[str, int]:
        return {"likes": 0, "comments": 0, "replies": 0, "shares": 0, "reels": 0, "sessions": 0}

    def _row_to_window(r: Any) -> dict[str, int]:
        return {
            "likes": int(r.likes or 0),
            "comments": int(r.comments or 0),
            "replies": int(r.replies or 0),
            "shares": int(r.shares or 0),
            "reels": int(r.reels or 0),
            "sessions": int(r.sessions or 0),
        }

    overview = []
    for acc in accounts:
        tc = target_counts.get(acc.username, {"total": 0, "active": 0})
        w24 = agg_24h.get(acc.username)
        w7 = agg_7d.get(acc.username)
        last = last_session.get(acc.username)
        overview.append({
            "account_username": acc.username,
            "engagement_enabled": acc.engagement_enabled,
            "target_count": tc["total"],
            "active_target_count": tc["active"],
            "last_session_at": last.isoformat() if last else None,
            "running_session_id": running_id.get(acc.username),
            "actions_24h": _row_to_window(w24) if w24 else _empty_window(),
            "actions_7d": _row_to_window(w7) if w7 else _empty_window(),
        })
    return overview


@router.post("/{account_username}/targets/copy-from/{source_username}")
async def copy_targets_from(
    account_username: str,
    source_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Copy all active targets from source_username onto account_username.

    Duplicates (same target_username) are silently skipped. Returns counts
    so the UI can show "Copied 5, skipped 2 duplicates".
    """
    if account_username == source_username:
        raise HTTPException(status_code=400, detail="Source and destination are the same account")

    # Verify both accounts exist and are active
    dest_acct = (await session.execute(
        select(Account).where(Account.username == account_username),
    )).scalar_one_or_none()
    if dest_acct is None or not dest_acct.is_active:
        raise HTTPException(status_code=404, detail=f"Destination account @{account_username} not found or inactive")

    src_acct = (await session.execute(
        select(Account).where(Account.username == source_username),
    )).scalar_one_or_none()
    if src_acct is None:
        raise HTTPException(status_code=404, detail=f"Source account @{source_username} not found")

    # Source targets — active only
    src_targets = (await session.execute(
        select(EngagementTarget).where(
            EngagementTarget.account_username == source_username,
            EngagementTarget.is_active == True,  # noqa: E712
        ),
    )).scalars().all()

    if not src_targets:
        return {"copied": 0, "skipped_duplicates": 0, "source_total": 0}

    # Existing target_usernames on destination (to detect dupes)
    existing = {
        r[0] for r in (await session.execute(
            select(EngagementTarget.target_username).where(
                EngagementTarget.account_username == account_username,
                EngagementTarget.is_active == True,  # noqa: E712
            ),
        )).all()
    }

    copied = 0
    skipped_dupes = 0
    for src in src_targets:
        if src.target_username in existing:
            skipped_dupes += 1
            continue
        session.add(EngagementTarget(
            account_username=account_username,
            target_username=src.target_username,
            max_reels=src.max_reels,
            should_follow=src.should_follow,
        ))
        existing.add(src.target_username)
        copied += 1

    await session.flush()
    return {
        "copied": copied,
        "skipped_duplicates": skipped_dupes,
        "source_total": len(src_targets),
    }


@router.get("/{account_username}/targets")
async def list_targets(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return engagement targets for an account."""
    result = await session.execute(
        _active_engagement_targets_query().where(
            EngagementTarget.account_username == account_username,
            EngagementTarget.is_active == True,  # noqa: E712
        ),
    )
    targets = result.scalars().all()
    return [
        {
            "id": t.id,
            "target_username": t.target_username,
            "account_username": t.account_username,
            "max_reels": t.max_reels,
            "should_follow": t.should_follow,
            "is_active": t.is_active,
        }
        for t in targets
    ]


@router.post("/{account_username}/targets", status_code=status.HTTP_201_CREATED)
async def add_target(
    account_username: str,
    body: TargetCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Add an engagement target for an account."""
    # Verify account exists
    account = (await session.execute(
        select(Account).where(Account.username == account_username),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if not account.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account is inactive")
    target_username = _normalize_target_username(body.channel_url or body.target_username)
    await _ensure_unique_target(
        session,
        account_username=account_username,
        target_username=target_username,
    )

    target = EngagementTarget(
        account_username=account_username,
        target_username=target_username,
        max_reels=body.max_reels,
        should_follow=body.should_follow,
    )
    session.add(target)
    await session.flush()
    return {
        "id": target.id,
        "target_username": target.target_username,
        "account_username": target.account_username,
        "max_reels": target.max_reels,
        "should_follow": target.should_follow,
    }


@router.delete("/{account_username}/targets/{target_id}")
async def delete_target(
    account_username: str,
    target_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Deactivate an engagement target."""
    target = (await session.execute(
        select(EngagementTarget).where(
            EngagementTarget.id == target_id,
            EngagementTarget.account_username == account_username,
        ),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    target.is_active = False
    await session.flush()
    return {"id": target.id, "is_active": False}


@router.get("/{account_username}/sessions")
async def list_sessions(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Return engagement sessions for an account."""
    result = await session.execute(
        select(EngagementSession)
        .where(EngagementSession.account_username == account_username)
        .order_by(EngagementSession.id.desc())
        .limit(limit),
    )
    sessions = result.scalars().all()
    return [
        {
            "id": s.id,
            "account_username": s.account_username,
            "status": s.status,
            "channels_visited": s.channels_visited,
            "reels_watched": s.reels_watched,
            "total_likes": s.total_likes,
            "total_comments": s.total_comments,
            "total_replies": s.total_replies,
            "total_follows": s.total_follows,
            "total_shares": s.total_shares,
            "duration_ms": s.duration_ms,
            "error": s.error,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "finished_at": s.finished_at.isoformat() if s.finished_at else None,
        }
        for s in sessions
    ]


@router.get("/{account_username}/actions")
async def list_actions(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return engagement actions for an account."""
    result = await session.execute(
        select(EngagementAction)
        .where(EngagementAction.account_username == account_username)
        .order_by(EngagementAction.id.desc())
        .limit(limit),
    )
    actions = result.scalars().all()
    return [
        {
            "id": a.id,
            "session_id": a.session_id,
            "account_username": a.account_username,
            "action_type": a.action_type,
            "target_username": a.target_username,
            "reel_caption": a.reel_caption,
            "comment_text": a.comment_text,
            "success": a.success,
            "performed_at": a.performed_at.isoformat() if a.performed_at else None,
        }
        for a in actions
    ]


@router.post("/{account_username}/start")
async def start_engagement(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Start an engagement session for an account.

    Sends the engagement command to the device via WebSocket. The device
    must be online; returns 409 if it is not.
    """
    account = (await session.execute(
        select(Account).where(Account.username == account_username),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    if not account.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account is inactive",
        )
    if account.is_paused:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Account is paused",
        )
    if not account.engagement_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Engagement is disabled",
        )

    # Resolve device
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

    # Check for already running session (per-account)
    running = (await session.execute(
        select(func.count(EngagementSession.id)).where(
            EngagementSession.account_username == account_username,
            EngagementSession.status == "running",
        ),
    )).scalar_one()
    if running > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Engagement session already running for this account",
        )

    # Check for running session on the SAME device (only 1 engagement per device)
    device_running = (await session.execute(
        select(func.count(EngagementSession.id)).where(
            EngagementSession.device_id == link.device_id,
            EngagementSession.status == "running",
        ),
    )).scalar_one()
    if device_running > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Device {link.device_id} already running engagement",
        )

    # Build engagement payload matching the Android MessageRouter contract
    targets_result = await session.execute(
        _active_engagement_targets_query().where(
            EngagementTarget.account_username == account_username,
            EngagementTarget.is_active == True,  # noqa: E712
        ),
    )
    targets = targets_result.scalars().all()
    action_probabilities = _target_probabilities(account)
    payload = {
        "accountUsername": account_username,
        "channels": [
            {
                "targetUsername": t.target_username,
                "maxReels": t.max_reels,
                "shouldFollow": t.should_follow,
            }
            for t in targets
        ],
        "dailyBudgetMinutes": _daily_budget_minutes(account),
        "actionProbabilities": {
            "likeProbability": action_probabilities["like_probability"],
            "commentProbability": action_probabilities["comment_probability"],
            "replyProbability": action_probabilities["reply_probability"],
            "shareProbability": action_probabilities["share_probability"],
        },
        "timings": {
            "watchMinMs": 3000,
            "watchMaxMs": 8000,
            "actionCooldownMinMs": 1000,
            "actionCooldownMaxMs": 3000,
            "channelCooldownMinMs": 5000,
            "channelCooldownMaxMs": 15000,
        },
        "llmEndpoint": _build_public_url(config),
        "useVisionLlm": True,
        "interested": 0.0,
    }

    await bridge.start_engagement(link.device_id, payload)

    eng_session = EngagementSession(
        account_username=account_username,
        device_id=link.device_id,
        status="running",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.add(eng_session)
    await session.flush()
    logger.info("Started engagement session %d for %s on device %d",
                eng_session.id, account_username, link.device_id)
    return {"session_id": eng_session.id, "status": "running"}


@router.post("/{account_username}/abort")
async def abort_engagement(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Abort the running engagement session for an account."""
    result = await session.execute(
        select(EngagementSession).where(
            EngagementSession.account_username == account_username,
            EngagementSession.status == "running",
        ),
    )
    eng_session = result.scalar_one_or_none()
    if eng_session is None:
        account = (await session.execute(
            select(Account).where(Account.username == account_username),
        )).scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

        device_id, error = await abort_engagement_on_device(
            account_username=account_username,
            session=session,
            bridge=bridge,
            ws=ws,
        )
        if error is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error,
            )
        if device_id is not None:
            logger.info("Sent orphan abort_engagement to device %d for %s", device_id, account_username)
        return {"session_id": None, "status": "aborted", "orphan_device_abort": True}

    device_id, error = await abort_engagement_on_device(
        account_username=account_username,
        session=session,
        bridge=bridge,
        ws=ws,
        device_id=eng_session.device_id,
    )
    if error is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error,
        )
    if device_id is not None:
        logger.info("Sent abort_engagement to device %d for %s", device_id, account_username)

    eng_session.status = "aborted"
    eng_session.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.flush()
    return {"session_id": eng_session.id, "status": "aborted"}
