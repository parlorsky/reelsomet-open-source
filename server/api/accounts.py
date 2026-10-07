"""Accounts API: CRUD, pause/block toggles."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server import farm_time
from server.config import VPSConfig
from server.dependencies import get_admin_broadcaster, get_bridge, get_config, get_db_session, get_ws_manager, require_auth
from server.device_actions import abort_engagement_on_device
from server.models import (
    Account,
    AccountDevice,
    Device,
    EngagementAction,
    EngagementSession,
    EngagementTarget,
    InsightsCollectionPlan,
    InsightsSnapshot,
    InsightsSnapshotDaily,
    MonitorSnapshot,
    MonitorTarget,
    PhotoSetUsage,
    PostLog,
    StoryAssetUsage,
    Video,
)
from server.ws.bridge import DeviceBridge
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.manager import DeviceConnectionManager

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _normalize_username(username: str) -> str:
    """Trim usernames and reject blank values at the API boundary."""
    normalized = username.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username cannot be empty",
        )
    return normalized


def _blank_to_none(value: str | None) -> str | None:
    """Normalize empty form fields without changing saved non-empty secrets."""
    if value is None:
        return None
    return value if value.strip() else None


def _derived_status(account: Account) -> str:
    """Return the dashboard-facing account status string."""
    if not account.is_active:
        return "inactive"
    if account.is_blocked:
        return "blocked"
    if account.is_paused:
        return "paused"
    return "active"


async def _broadcast_account_inventory(
    broadcaster: AdminBroadcaster,
    *,
    account: Account,
    action: str,
    device_id: int | None,
) -> None:
    """Notify admin dashboards when account roster or assignment changes."""
    await broadcaster.broadcast("account:inventory", {
        "account_id": account.id,
        "username": account.username,
        "status": _derived_status(account),
        "device_id": device_id,
        "action": action,
    })


async def _account_device_map(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Return one device payload per account, preferring primary links when present."""
    result = await session.execute(
        select(AccountDevice.account_username, AccountDevice.device_id, Device.name)
        .join(Device, AccountDevice.device_id == Device.id)
        .where(Device.is_active == True)  # noqa: E712
        .order_by(AccountDevice.account_username, AccountDevice.is_primary.desc(), AccountDevice.id),
    )

    device_info: dict[str, dict[str, Any]] = {}
    for account_username, device_id, device_name in result.all():
        device_info.setdefault(
            account_username,
            {
                "device_id": device_id,
                "device_name": device_name or "",
            },
        )
    return device_info


async def _account_device_payload(session: AsyncSession, account_username: str) -> dict[str, Any]:
    """Return the current device payload for a single account."""
    row = (await session.execute(
        select(AccountDevice.device_id, Device.name)
        .join(Device, AccountDevice.device_id == Device.id)
        .where(
            AccountDevice.account_username == account_username,
            Device.is_active == True,  # noqa: E712
        )
        .order_by(AccountDevice.is_primary.desc(), AccountDevice.id)
        .limit(1),
    )).first()
    if row is None:
        return {"device_id": None, "device_name": ""}
    return {"device_id": row[0], "device_name": row[1] or ""}


async def _set_account_device_link(
    session: AsyncSession,
    *,
    account_username: str,
    device_id: int | None,
) -> dict[str, Any]:
    """Normalize account-device links down to one primary assignment or none."""
    existing_links = (await session.execute(
        select(AccountDevice)
        .where(AccountDevice.account_username == account_username)
        .order_by(AccountDevice.id),
    )).scalars().all()

    if device_id is None:
        for link in existing_links:
            await session.delete(link)
        return {"device_id": None, "device_name": ""}

    device = (await session.execute(
        select(Device).where(
            Device.id == device_id,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    retained_link: AccountDevice | None = None
    for link in existing_links:
        if retained_link is None and link.device_id == device.id:
            retained_link = link
            retained_link.is_primary = True
            continue
        await session.delete(link)

    if retained_link is None:
        session.add(
            AccountDevice(
                account_username=account_username,
                device_id=device.id,
                is_primary=True,
            )
        )

    return {"device_id": device.id, "device_name": device.name or ""}


async def _hard_delete_account(session: AsyncSession, account: Account) -> None:
    """Physically remove an account and rows that are keyed by account_username."""
    account_username = account.username
    video_ids = select(Video.id).where(Video.account_username == account_username)

    for stmt in [
        delete(StoryAssetUsage).where(StoryAssetUsage.account_username == account_username),
        delete(PhotoSetUsage).where(PhotoSetUsage.account_username == account_username),
        delete(InsightsSnapshotDaily).where(InsightsSnapshotDaily.video_id.in_(video_ids)),
        delete(InsightsCollectionPlan).where(InsightsCollectionPlan.account_username == account_username),
        delete(InsightsSnapshot).where(InsightsSnapshot.account_username == account_username),
        delete(PostLog).where(PostLog.account_username == account_username),
        delete(EngagementAction).where(EngagementAction.account_username == account_username),
        delete(EngagementSession).where(EngagementSession.account_username == account_username),
        delete(MonitorSnapshot).where(MonitorSnapshot.account_username == account_username),
        delete(MonitorTarget).where(MonitorTarget.account_username == account_username),
        delete(EngagementTarget).where(EngagementTarget.account_username == account_username),
        delete(AccountDevice).where(AccountDevice.account_username == account_username),
        delete(Video).where(Video.account_username == account_username),
        delete(Account).where(Account.id == account.id),
    ]:
        await session.execute(stmt.execution_options(synchronize_session=False))


class AccountCreate(BaseModel):
    username: str
    device_id: int | None = None
    notes: str = ""
    ig_password: str | None = None
    ig_2fa_secret: str | None = None
    posting_enabled: bool = True
    engagement_enabled: bool = False
    insights_enabled: bool = False
    posting_times: str | None = None
    max_posts_per_day: int | None = None
    # Story dispatcher (2026-04-14): per-account story schedule +
    # cadence overrides. All null by default — scheduler falls back
    # to global `farm_default_story_posting_times` and
    # `farm_max_stories_per_account_per_day`.
    story_posting_times: str | None = None
    story_max_per_day: int | None = None
    story_element_probability: float | None = None
    story_element_weights: dict | str | None = None


class AccountPatch(BaseModel):
    username: str | None = None
    device_id: int | None = None
    is_active: bool | None = None
    is_paused: bool | None = None
    notes: str | None = None
    ig_password: str | None = None
    ig_2fa_secret: str | None = None
    posting_enabled: bool | None = None
    posting_times: str | None = None
    max_posts_per_day: int | None = None
    recreator_model: str | None = None
    recreator_video_type: str | None = None
    gen_style_json: str | None = None
    insights_enabled: bool | None = None
    insights_interval_hours: float | None = None
    insights_max_reels: int | None = None
    engagement_enabled: bool | None = None
    engagement_like_prob: float | None = None
    engagement_comment_prob: float | None = None
    engagement_reply_prob: float | None = None
    engagement_share_prob: float | None = None
    engagement_daily_budget: int | None = None
    engagement_sessions_day: int | None = None
    engagement_max_reels: int | None = None
    engagement_follow: bool | None = None
    max_auto_retries: int | None = None
    auto_retry_delay_minutes: int | None = None
    action_blocked_pause_hours: float | None = None
    # Per-account story cadence overrides — T4.
    story_max_per_day: int | None = None
    story_element_probability: float | None = None
    story_element_weights: dict | str | None = None
    # Story dispatcher (2026-04-14): per-account story wall-clock slots.
    story_posting_times: str | None = None
    # Tri-state override for reel generation path — T8.
    use_scenarios: bool | None = None


@router.get("")
async def list_accounts(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> list[dict[str, Any]]:
    """Return all active accounts."""
    result = await session.execute(
        select(Account).where(Account.is_active == True).order_by(Account.username),  # noqa: E712
    )
    accounts = result.scalars().all()

    device_info = await _account_device_map(session)

    # Pre-fetch posts_today per account. `date.today()` returns the
    # server OS local date which on a UTC VPS diverges from the farm
    # timezone by several hours, causing posts_today to miss recent
    # local-day posts. Use the farm-timezone day window instead
    # (Codex bug hunt iteration 3, 2026-04-14).
    today_start, _today_end = farm_time.local_day_bounds_utc(
        farm_time.utcnow_naive(), config.farm_timezone,
    )
    today_result = await session.execute(
        select(PostLog.account_username, func.count(PostLog.id))
        .where(PostLog.timestamp >= today_start)
        .group_by(PostLog.account_username),
    )
    posts_today_map: dict[str, int] = {row[0]: row[1] for row in today_result}

    # Pre-fetch last_post_at per account
    last_post_result = await session.execute(
        select(PostLog.account_username, func.max(PostLog.timestamp))
        .group_by(PostLog.account_username),
    )
    last_post_map: dict[str, datetime | None] = {row[0]: row[1] for row in last_post_result}

    out = []
    for a in accounts:
        if a.is_blocked:
            derived_status = "blocked"
        elif a.is_paused:
            derived_status = "paused"
        else:
            derived_status = "active"

        last_post = last_post_map.get(a.username)
        out.append({
            "id": a.id,
            "username": a.username,
            "ig_password": a.ig_password or "",
            "ig_2fa_secret": a.ig_2fa_secret or "",  # gitleaks:allow -- serialization, not a credential literal
            "device_id": device_info.get(a.username, {}).get("device_id"),
            "is_active": a.is_active,
            "is_paused": a.is_paused,
            "is_blocked": a.is_blocked,
            "total_posted": a.total_posted,
            "posts_count": a.total_posted,
            "total_failed": a.total_failed,
            "posting_enabled": a.posting_enabled,
            "engagement_enabled": a.engagement_enabled,
            "insights_enabled": a.insights_enabled,
            "device_name": device_info.get(a.username, {}).get("device_name", ""),
            "status": derived_status,
            "posts_today": posts_today_map.get(a.username, 0),
            "last_post_at": farm_time.isoformat_utc(last_post),
            "followers": getattr(a, 'followers', 0) or 0,
            "following": getattr(a, 'following', 0) or 0,
            "notes": a.notes or "",
            "posting_times": a.posting_times or "",
            "max_posts_per_day": a.max_posts_per_day,
            "recreator_model": a.recreator_model or "",
            "recreator_video_type": a.recreator_video_type or "",
            "engagement_like_prob": getattr(a, 'engagement_like_prob', None),
            "engagement_comment_prob": getattr(a, 'engagement_comment_prob', None),
            "engagement_reply_prob": getattr(a, 'engagement_reply_prob', None),
            "engagement_share_prob": getattr(a, 'engagement_share_prob', None),
            "engagement_daily_budget": getattr(a, 'engagement_daily_budget', None),
            "engagement_sessions_day": getattr(a, 'engagement_sessions_day', None),
            "engagement_max_reels": getattr(a, 'engagement_max_reels', None),
            "engagement_follow": getattr(a, 'engagement_follow', False),
            "max_auto_retries": getattr(a, 'max_auto_retries', None),
            "auto_retry_delay_minutes": getattr(a, 'auto_retry_delay_minutes', None),
            "action_blocked_pause_hours": getattr(a, 'action_blocked_pause_hours', None),
            "story_max_per_day": getattr(a, 'story_max_per_day', None),
            "story_element_probability": getattr(a, 'story_element_probability', None),
            "story_element_weights": getattr(a, 'story_element_weights', None),
            "story_posting_times": getattr(a, 'story_posting_times', None) or "",
            "use_scenarios": getattr(a, 'use_scenarios', None),
        })
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_account(
    body: AccountCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Create a new account."""
    normalized_username = _normalize_username(body.username)
    existing = (await session.execute(
        select(Account).where(Account.username == normalized_username),
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Account already exists")

    # Normalize story_element_weights: accept dict (from the admin UI)
    # OR a pre-serialised JSON string, persist as TEXT. Match the PATCH
    # handler semantics so POST-then-PATCH round-trips cleanly.
    story_weights_str: str | None = None
    if body.story_element_weights is not None:
        if isinstance(body.story_element_weights, str):
            story_weights_str = body.story_element_weights
        else:
            story_weights_str = json.dumps(body.story_element_weights)

    account = Account(
        username=normalized_username,
        notes=body.notes,
        ig_password=_blank_to_none(body.ig_password),
        ig_2fa_secret=_blank_to_none(body.ig_2fa_secret),
        posting_enabled=body.posting_enabled,
        engagement_enabled=body.engagement_enabled,
        insights_enabled=body.insights_enabled,
        posting_times=body.posting_times,
        max_posts_per_day=body.max_posts_per_day,
        story_posting_times=body.story_posting_times,
        story_max_per_day=body.story_max_per_day,
        story_element_probability=body.story_element_probability,
        story_element_weights=story_weights_str,
    )
    session.add(account)
    await session.flush()

    device_payload = {"device_id": None, "device_name": ""}
    if body.device_id is not None:
        device_payload = await _set_account_device_link(
            session,
            account_username=account.username,
            device_id=body.device_id,
        )
        await session.flush()
    # Commit before broadcasting so admin WS clients never see an
    # "account added" event for a row that gets rolled back if the
    # response body fails to serialize or the client disconnects
    # mid-response (Codex iter 8 bug hunt 2026-04-14, same pattern
    # as the earlier Feature B queue upload fix).
    await session.commit()
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="added",
        device_id=device_payload["device_id"],
    )

    return {
        "id": account.id,
        "username": account.username,
        "ig_password": account.ig_password or "",
        "ig_2fa_secret": account.ig_2fa_secret or "",
        "device_id": device_payload["device_id"],
        "device_name": device_payload["device_name"],
        "is_active": account.is_active,
        "is_paused": account.is_paused,
        "is_blocked": account.is_blocked,
        "posting_enabled": account.posting_enabled,
        "engagement_enabled": account.engagement_enabled,
        "insights_enabled": account.insights_enabled,
    }


@router.get("/{account_id}")
async def get_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return a single account with video stats."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    pending = (await session.execute(
        select(func.count(Video.id)).where(
            Video.account_username == account.username,
            Video.status == "pending",
        ),
    )).scalar_one()

    return {
        "id": account.id,
        "username": account.username,
        "ig_password": account.ig_password or "",
        "ig_2fa_secret": account.ig_2fa_secret or "",
        "is_active": account.is_active,
        "is_paused": account.is_paused,
        "is_blocked": account.is_blocked,
        "total_posted": account.total_posted,
        "total_failed": account.total_failed,
        "notes": account.notes,
        "pending_videos": pending,
    }


@router.patch("/{account_id}")
async def patch_account(
    account_id: int,
    body: AccountPatch,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Update account fields."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    update_data = body.model_dump(exclude_unset=True)
    device_id_supplied = "device_id" in update_data
    requested_device_id = update_data.pop("device_id", None)
    should_abort_engagement = (
        update_data.get("engagement_enabled") is False
        and account.engagement_enabled is True
    )

    # Handle username rename (cascading FK update)
    raw_new_username = update_data.pop("username", None)
    new_username = _normalize_username(raw_new_username) if raw_new_username is not None else None
    if new_username and new_username != account.username:
        # Check if new username already taken
        existing = (await session.execute(
            select(Account).where(Account.username == new_username),
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

        old_username = account.username
        # Update all FK references (SQLite doesn't enforce CASCADE on UPDATE)
        from sqlalchemy import update as sa_update
        from server.models import AccountDevice, Video, EngagementTarget, EngagementSession, EngagementAction
        from server.models import MonitorTarget, MonitorSnapshot, InsightsSnapshot, PostLog

        for model, col_name in [
            (AccountDevice, "account_username"),
            (Video, "account_username"),
            (EngagementTarget, "account_username"),
            (EngagementSession, "account_username"),
            (EngagementAction, "account_username"),
            (MonitorTarget, "account_username"),
            (MonitorSnapshot, "account_username"),
            (InsightsSnapshot, "account_username"),
            (PostLog, "account_username"),
        ]:
            col = getattr(model, col_name, None)
            if col is not None:
                await session.execute(
                    sa_update(model).where(col == old_username).values({col_name: new_username})
                )

        account.username = new_username
        logger.info("Renamed account %d: %s → %s", account_id, old_username, new_username)

    device_payload: dict[str, Any] | None = None
    if device_id_supplied:
        device_payload = await _set_account_device_link(
            session,
            account_username=account.username,
            device_id=requested_device_id,
        )

    if "ig_password" in update_data:
        new_password = _blank_to_none(update_data.pop("ig_password"))
        if new_password is not None:
            account.ig_password = new_password
    if "ig_2fa_secret" in update_data:
        new_2fa_secret = _blank_to_none(update_data.pop("ig_2fa_secret"))
        if new_2fa_secret is not None:
            account.ig_2fa_secret = new_2fa_secret

    # story_element_probability: 0..1 or null
    # Pop out of update_data so the generic setattr loop below does not
    # double-write an unvalidated value.
    if "story_element_probability" in update_data:
        raw_prob = update_data.pop("story_element_probability")
        if raw_prob is not None:
            val = float(raw_prob)
            if not (0.0 <= val <= 1.0):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="story_element_probability must be 0..1 or null",
                )
            account.story_element_probability = val
        else:
            account.story_element_probability = None

    # story_element_weights: {"poll", "question", "text"}, non-negative, sum>0.
    # Accept either a dict (from the admin UI) or a pre-serialised JSON
    # string; persist as JSON text. Disable an element with weight=0 —
    # missing keys are a 400 so typos do not silently drop a channel.
    if "story_element_weights" in update_data:
        raw_weights = update_data.pop("story_element_weights")
        if raw_weights is not None:
            if isinstance(raw_weights, str):
                try:
                    raw_weights = json.loads(raw_weights)
                except json.JSONDecodeError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="story_element_weights must be valid JSON",
                    )
            if not isinstance(raw_weights, dict):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="story_element_weights must be a JSON object",
                )
            required = {"poll", "question", "text"}
            if set(raw_weights.keys()) != required:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"story_element_weights must have exactly keys {sorted(required)}",
                )
            if any(
                not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0
                for v in raw_weights.values()
            ):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="story_element_weights values must be non-negative numbers",
                )
            if sum(raw_weights.values()) <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="story_element_weights sum must be > 0",
                )
            account.story_element_weights = json.dumps(raw_weights)
        else:
            account.story_element_weights = None

    for key, value in update_data.items():
        setattr(account, key, value)

    if should_abort_engagement:
        _device_id, abort_error = await abort_engagement_on_device(
            account_username=account.username,
            session=session,
            bridge=bridge,
            ws=ws,
        )
        if abort_error is not None:
            logger.warning(
                "Engagement disabled for %s but device abort was skipped: %s",
                account.username,
                abort_error,
            )

    await session.flush()
    if device_payload is None:
        device_payload = await _account_device_payload(session, account.username)
    # Commit before broadcast — mirror add_account. Prevents admin
    # WS clients from seeing an "updated" event for a row that gets
    # rolled back (Codex iter 8 bug hunt 2026-04-14).
    await session.commit()
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="updated",
        device_id=device_payload["device_id"],
    )
    return {
        "id": account.id,
        "username": account.username,
        "ig_password": account.ig_password or "",
        "ig_2fa_secret": account.ig_2fa_secret or "",
        "device_id": device_payload["device_id"],
        "device_name": device_payload["device_name"],
        "notes": account.notes,
        "posting_enabled": account.posting_enabled,
        "posting_times": account.posting_times,
        "max_posts_per_day": account.max_posts_per_day,
        "insights_enabled": account.insights_enabled,
        "engagement_enabled": account.engagement_enabled,
        "story_max_per_day": account.story_max_per_day,
        "story_element_probability": account.story_element_probability,
        "story_element_weights": account.story_element_weights,
        "use_scenarios": account.use_scenarios,
    }


@router.delete("/{account_id}")
async def delete_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Deactivate an account (soft-delete)."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    username = account.username
    account.is_active = False
    await _hard_delete_account(session, account)
    await session.commit()  # Commit before broadcast (Codex iter 8 Q3)
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="removed",
        device_id=None,
    )
    return {"id": account_id, "username": username, "deleted": True}


@router.post("/{account_id}/pause")
async def pause_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Toggle pause state on an account."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    account.is_paused = not account.is_paused
    await session.flush()
    device_payload = await _account_device_payload(session, account.username)
    await session.commit()  # Commit before broadcast (Codex iter 8 Q3)
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="updated",
        device_id=device_payload["device_id"],
    )
    return {"id": account.id, "is_paused": account.is_paused}


@router.post("/{account_id}/resume")
@router.post("/{account_id}/unpause")
async def unpause_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Explicitly unpause an account."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    account.is_paused = False
    await session.flush()
    device_payload = await _account_device_payload(session, account.username)
    await session.commit()  # Commit before broadcast (Codex iter 8 Q3)
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="updated",
        device_id=device_payload["device_id"],
    )
    return {"id": account.id, "is_paused": account.is_paused}


@router.post("/{account_id}/block")
async def block_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Toggle blocked state on an account."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    account.is_blocked = not account.is_blocked
    await session.flush()
    device_payload = await _account_device_payload(session, account.username)
    await session.commit()  # Commit before broadcast (Codex iter 8 Q3)
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="updated",
        device_id=device_payload["device_id"],
    )
    return {"id": account.id, "is_blocked": account.is_blocked}


@router.post("/{account_id}/unblock")
async def unblock_account(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Explicitly unblock an account."""
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    account.is_blocked = False
    account.blocked_until = None
    await session.flush()
    device_payload = await _account_device_payload(session, account.username)
    await session.commit()  # Commit before broadcast (Codex iter 8 Q3)
    await _broadcast_account_inventory(
        broadcaster,
        account=account,
        action="updated",
        device_id=device_payload["device_id"],
    )
    return {"id": account.id, "is_blocked": False}
