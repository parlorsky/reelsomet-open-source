from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.models import AccountDevice, DeviceFsmState, EngagementSession, Video
from server.pinterest.device_calendar import check_device_calendar


@pytest.mark.asyncio
async def test_calendar_blocks_offline_device(db_session, seed_db: dict[str, Any]):
    offline_device = seed_db["devices"][1]

    decision = await check_device_calendar(
        db_session,
        device_id=offline_device.id,
        now=datetime.utcnow(),
        guard_minutes=20,
    )

    assert decision.allowed is False
    assert decision.reason == "device_offline"


@pytest.mark.asyncio
async def test_calendar_blocks_active_instagram_video(db_session, seed_db: dict[str, Any]):
    device = seed_db["devices"][0]
    db_session.add(
        Video(
            filename="active.mp4",
            account_username="user_alpha",
            device_id=device.id,
            status="uploading",
        )
    )
    await db_session.commit()

    decision = await check_device_calendar(
        db_session,
        device_id=device.id,
        now=datetime.utcnow(),
        guard_minutes=20,
    )

    assert decision.allowed is False
    assert decision.reason == "instagram_active_video"


@pytest.mark.asyncio
async def test_calendar_blocks_due_instagram_video_on_postable_account(db_session, seed_db: dict[str, Any]):
    device = seed_db["devices"][0]
    now = datetime.utcnow()
    db_session.add(
        Video(
            filename="due.mp4",
            account_username="user_alpha",
            status="pending",
            scheduled_time=now + timedelta(minutes=5),
        )
    )
    await db_session.commit()

    decision = await check_device_calendar(
        db_session,
        device_id=device.id,
        now=now,
        guard_minutes=20,
    )

    assert decision.allowed is False
    assert decision.reason == "instagram_due_inside_guard"
    assert decision.next_check_at == now + timedelta(minutes=5)


@pytest.mark.asyncio
async def test_calendar_ignores_due_instagram_video_on_paused_account(db_session, seed_db: dict[str, Any]):
    device = seed_db["devices"][0]
    now = datetime.utcnow()
    db_session.add(
        Video(
            filename="paused_due.mp4",
            account_username="user_beta",
            status="pending",
            scheduled_time=now + timedelta(minutes=5),
        )
    )
    await db_session.commit()

    decision = await check_device_calendar(
        db_session,
        device_id=device.id,
        now=now,
        guard_minutes=20,
    )

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_calendar_blocks_running_engagement_session(db_session, seed_db: dict[str, Any]):
    device = seed_db["devices"][0]
    db_session.add(
        EngagementSession(
            account_username="user_alpha",
            device_id=device.id,
            status="running",
            started_at=datetime.utcnow(),
        )
    )
    await db_session.commit()

    decision = await check_device_calendar(
        db_session,
        device_id=device.id,
        now=datetime.utcnow(),
        guard_minutes=20,
    )

    assert decision.allowed is False
    assert decision.reason == "engagement_running"


@pytest.mark.asyncio
async def test_calendar_blocks_busy_device_fsm(db_session, seed_db: dict[str, Any]):
    device = seed_db["devices"][0]
    db_session.add(
        DeviceFsmState(
            device_id=device.id,
            fsm_kind="posting",
            current_state="FILL_CAPTION",
            state_entered_at=1,
            last_event_id="evt_1",
            updated_at=1,
        )
    )
    await db_session.commit()

    decision = await check_device_calendar(
        db_session,
        device_id=device.id,
        now=datetime.utcnow(),
        guard_minutes=20,
    )

    assert decision.allowed is False
    assert decision.reason == "device_fsm_busy"
