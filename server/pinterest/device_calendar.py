"""Same-device calendar guard for Pinterest automation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    Account,
    AccountDevice,
    Device,
    DeviceFsmState,
    EngagementSession,
    Video,
)


@dataclass(frozen=True)
class DeviceCalendarDecision:
    allowed: bool
    reason: str | None = None
    next_check_at: datetime | None = None


_IDLE_FSM_STATES = {
    "idle",
    "done",
    "completed",
    "failed",
    "cancelled",
    "aborted",
    "none",
}


async def check_device_calendar(
    session: AsyncSession,
    device_id: int,
    now: datetime,
    guard_minutes: int,
) -> DeviceCalendarDecision:
    """Return whether Pinterest may start work on a shared physical device.

    This is intentionally DB-only. Runtime Android `activeMode` remains a
    scheduler/bridge check, while this helper protects against known queued or
    persisted Reelsomet work for the same phone.
    """
    device = await session.get(Device, device_id)
    if device is None or not device.is_active or device.status != "online":
        return DeviceCalendarDecision(False, "device_offline")

    if await _find_active_instagram_video_for_device(session, device_id) is not None:
        return DeviceCalendarDecision(False, "instagram_active_video")

    due_at = await _find_due_instagram_video_for_device(session, device_id, now, guard_minutes)
    if due_at is not None:
        return DeviceCalendarDecision(False, "instagram_due_inside_guard", next_check_at=due_at)

    if await _find_running_engagement_for_device(session, device_id) is not None:
        return DeviceCalendarDecision(False, "engagement_running")

    if await _find_busy_device_fsm(session, device_id) is not None:
        return DeviceCalendarDecision(False, "device_fsm_busy")

    return DeviceCalendarDecision(True)


async def _find_active_instagram_video_for_device(
    session: AsyncSession,
    device_id: int,
) -> int | None:
    row = (
        await session.execute(
            select(Video.id)
            .where(
                Video.status.in_(["uploading", "scheduled", "posting"]),
                or_(
                    Video.device_id == device_id,
                    Video.account_username.in_(
                        select(AccountDevice.account_username).where(AccountDevice.device_id == device_id)
                    ),
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _find_due_instagram_video_for_device(
    session: AsyncSession,
    device_id: int,
    now: datetime,
    guard_minutes: int,
) -> datetime | None:
    cutoff = now + timedelta(minutes=guard_minutes)
    row = (
        await session.execute(
            select(Video.scheduled_time)
            .join(Account, Account.username == Video.account_username)
            .join(
                AccountDevice,
                AccountDevice.account_username == Video.account_username,
            )
            .where(
                AccountDevice.device_id == device_id,
                AccountDevice.is_primary.is_(True),
                Video.status == "pending",
                Video.scheduled_time.is_not(None),
                Video.scheduled_time <= cutoff,
                Account.is_active.is_(True),
                Account.is_paused.is_(False),
                Account.posting_enabled.is_(True),
                or_(
                    Account.is_blocked.is_(False),
                    and_(
                        Account.blocked_until.is_not(None),
                        Account.blocked_until <= now,
                    ),
                ),
            )
            .order_by(Video.scheduled_time.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row


async def _find_running_engagement_for_device(
    session: AsyncSession,
    device_id: int,
) -> int | None:
    return (
        await session.execute(
            select(EngagementSession.id)
            .where(
                EngagementSession.device_id == device_id,
                EngagementSession.status == "running",
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _find_busy_device_fsm(
    session: AsyncSession,
    device_id: int,
) -> str | None:
    rows = (
        await session.execute(
            select(DeviceFsmState.fsm_kind, DeviceFsmState.current_state)
            .where(DeviceFsmState.device_id == device_id)
        )
    ).all()
    for fsm_kind, current_state in rows:
        if str(current_state or "").strip().lower() not in _IDLE_FSM_STATES:
            return str(fsm_kind)
    return None
