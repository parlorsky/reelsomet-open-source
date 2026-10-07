from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.models import DeviceFsmState, RedditAccount, RedditSchedulerSettings, Video
from server.reddit.device_guard import check_reddit_device_guard


async def _seed_reddit_account(
    db_session,
    *,
    device_id: int,
    commenting_enabled: bool = True,
    auto_reply_enabled: bool = True,
    guard_minutes: int = 20,
) -> RedditAccount:
    account = RedditAccount(
        username="demo_creator_",
        device_id=device_id,
        status="active",
        posting_enabled=False,
        commenting_enabled=commenting_enabled,
        auto_reply_enabled=auto_reply_enabled,
        app_installed=True,
        logged_in=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        RedditSchedulerSettings(
            account_id=account.id,
            posting_enabled=False,
            scan_comments_enabled=True,
            auto_reply_enabled=auto_reply_enabled,
            device_guard_minutes=guard_minutes,
        )
    )
    await db_session.commit()
    return account


@pytest.mark.asyncio
async def test_reddit_reply_guard_blocks_busy_instagram_fsm_on_same_phone(
    db_session,
    seed_db: dict[str, Any],
) -> None:
    device = seed_db["devices"][0]
    account = await _seed_reddit_account(db_session, device_id=device.id)
    db_session.add(
        DeviceFsmState(
            device_id=device.id,
            fsm_kind="instagram_posting",
            current_state="FILL_CAPTION",
            state_entered_at=1,
            last_event_id="evt_1",
            updated_at=1,
        )
    )
    await db_session.commit()

    decision = await check_reddit_device_guard(
        db_session,
        account_id=account.id,
        now=datetime.utcnow(),
        action="reply",
    )

    assert decision.allowed is False
    assert decision.reason == "device_fsm_busy"
    assert decision.device_id == device.id


@pytest.mark.asyncio
async def test_reddit_reply_guard_blocks_due_instagram_post_inside_guard(
    db_session,
    seed_db: dict[str, Any],
) -> None:
    device = seed_db["devices"][0]
    now = datetime.utcnow()
    account = await _seed_reddit_account(db_session, device_id=device.id, guard_minutes=40)
    db_session.add(
        Video(
            filename="due.mp4",
            account_username="user_alpha",
            status="pending",
            scheduled_time=now + timedelta(minutes=30),
        )
    )
    await db_session.commit()

    decision = await check_reddit_device_guard(
        db_session,
        account_id=account.id,
        now=now,
        action="reply",
    )

    assert decision.allowed is False
    assert decision.reason == "instagram_due_inside_guard"
    assert decision.next_check_at == now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_reddit_reply_guard_allows_when_due_instagram_post_is_outside_guard(
    db_session,
    seed_db: dict[str, Any],
) -> None:
    device = seed_db["devices"][0]
    now = datetime.utcnow()
    account = await _seed_reddit_account(db_session, device_id=device.id, guard_minutes=20)
    db_session.add(
        Video(
            filename="later.mp4",
            account_username="user_alpha",
            status="pending",
            scheduled_time=now + timedelta(minutes=30),
        )
    )
    await db_session.commit()

    decision = await check_reddit_device_guard(
        db_session,
        account_id=account.id,
        now=now,
        action="reply",
    )

    assert decision.allowed is True
    assert decision.reason is None
    assert decision.device_id == device.id


@pytest.mark.asyncio
async def test_reddit_reply_guard_requires_reply_switches_before_touching_phone(
    db_session,
    seed_db: dict[str, Any],
) -> None:
    device = seed_db["devices"][0]
    account = await _seed_reddit_account(
        db_session,
        device_id=device.id,
        commenting_enabled=False,
        auto_reply_enabled=True,
    )

    decision = await check_reddit_device_guard(
        db_session,
        account_id=account.id,
        now=datetime.utcnow(),
        action="reply",
    )

    assert decision.allowed is False
    assert decision.reason == "account_commenting_disabled"
