"""Same-device guard for Reddit automation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import RedditAccount, RedditSchedulerSettings
from server.pinterest.device_calendar import check_device_calendar


RedditDeviceAction = Literal["post", "comment_scan", "reply"]


@dataclass(frozen=True)
class RedditDeviceGuardDecision:
    allowed: bool
    reason: str | None = None
    next_check_at: datetime | None = None
    device_id: int | None = None


async def check_reddit_device_guard(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    action: RedditDeviceAction,
) -> RedditDeviceGuardDecision:
    """Return whether Reddit automation may touch the assigned phone.

    Reddit shares physical devices with Instagram/Reelsomet. Any Reddit FSM
    must pass through this DB guard before sending a command to Android.
    """
    if action not in {"post", "comment_scan", "reply"}:
        return RedditDeviceGuardDecision(False, "invalid_action")

    account = await session.get(RedditAccount, account_id)
    if account is None:
        return RedditDeviceGuardDecision(False, "account_missing")
    if account.status != "active":
        return RedditDeviceGuardDecision(False, "account_not_active", device_id=account.device_id)
    if account.device_id is None:
        return RedditDeviceGuardDecision(False, "device_missing")

    settings = (
        await session.execute(
            select(RedditSchedulerSettings)
            .where(RedditSchedulerSettings.account_id == account.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if settings is None:
        return RedditDeviceGuardDecision(False, "settings_missing", device_id=account.device_id)

    if action == "post":
        if not account.posting_enabled:
            return RedditDeviceGuardDecision(False, "account_posting_disabled", device_id=account.device_id)
        if not settings.posting_enabled:
            return RedditDeviceGuardDecision(False, "scheduler_posting_disabled", device_id=account.device_id)

    if action in {"comment_scan", "reply"}:
        if not account.commenting_enabled:
            return RedditDeviceGuardDecision(False, "account_commenting_disabled", device_id=account.device_id)
        if not settings.scan_comments_enabled:
            return RedditDeviceGuardDecision(False, "scheduler_comment_scan_disabled", device_id=account.device_id)

    if action == "reply":
        if not account.auto_reply_enabled:
            return RedditDeviceGuardDecision(False, "account_auto_reply_disabled", device_id=account.device_id)
        if not settings.auto_reply_enabled:
            return RedditDeviceGuardDecision(False, "scheduler_auto_reply_disabled", device_id=account.device_id)

    device_id = int(account.device_id)
    decision = await check_device_calendar(
        session,
        device_id=device_id,
        now=now,
        guard_minutes=int(settings.device_guard_minutes or 0),
    )
    return RedditDeviceGuardDecision(
        decision.allowed,
        decision.reason,
        decision.next_check_at,
        device_id=device_id,
    )
