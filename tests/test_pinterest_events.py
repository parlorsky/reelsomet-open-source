from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, async_sessionmaker
from unittest.mock import AsyncMock

from server.app import _wire_device_events
from server.models import (
    Device,
    PinterestAccount,
    PinterestBoard,
    PinterestPostAttempt,
    PinterestTaskEvent,
)
from server.pinterest.events import persist_pinterest_fsm_event
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType, WSMessage


@pytest.mark.asyncio
async def test_persist_pinterest_fsm_event_updates_attempt_debug_fields(db_session):
    device = Device(
        device_id="HONOR-PINTEREST",
        name="Honor",
        ip_address="127.0.0.1",
        status="online",
    )
    db_session.add(device)
    await db_session.flush()

    account = PinterestAccount(
        username="demo_creator",
        device_id=device.id,
        status="active",
    )
    db_session.add(account)
    await db_session.flush()

    board = PinterestBoard(
        account_id=account.id,
        key="mirror",
        name="Mirror Selfies",
        description="Mirror ideas.",
        visibility="public",
        status="active",
    )
    db_session.add(board)
    await db_session.flush()

    attempt = PinterestPostAttempt(
        task_id="ptask_1",
        trace_id="ptrace_1",
        task_type="pinterest.publish_pin",
        status="running",
        account_id=account.id,
        board_id=board.id,
        device_id=device.id,
    )
    db_session.add(attempt)
    await db_session.flush()

    event = await persist_pinterest_fsm_event(
        db_session,
        device_id=device.id,
        payload={
            "eventId": "evt_1",
            "ts": 1_715_000_000_000,
            "taskId": "ptask_1",
            "traceId": "ptrace_1",
            "fsm": "pinterest_pin_publish",
            "state": "FILL_TITLE",
            "stateEnteredAt": 1_715_000_000_001,
            "action": {"name": "set_text", "target": "Title", "result": "success"},
            "nextAction": {
                "name": "fill_description",
                "target": "Description",
                "scheduledAt": 1_715_000_000_501,
            },
            "screen": {"activity": "Pinterest", "screenHash": "abc123"},
            "message": "Title filled",
        },
    )
    await db_session.commit()

    saved = (
        await db_session.execute(select(PinterestTaskEvent).where(PinterestTaskEvent.event_id == "evt_1"))
    ).scalar_one()
    assert saved.id == event.id
    assert saved.attempt_id == attempt.id
    assert saved.device_id == device.id
    assert saved.account_id == account.id
    assert saved.board_id == board.id
    assert saved.state == "FILL_TITLE"
    assert saved.action_name == "set_text"
    assert saved.next_action_name == "fill_description"
    assert saved.next_action_at_ms == 1_715_000_000_501
    assert saved.screen_hash == "abc123"

    refreshed_attempt = await db_session.get(PinterestPostAttempt, attempt.id)
    assert refreshed_attempt.latest_state == "FILL_TITLE"
    assert refreshed_attempt.latest_action == "set_text"
    assert refreshed_attempt.next_action == "fill_description"


@pytest.mark.asyncio
async def test_persist_pinterest_fsm_event_is_idempotent_by_event_id(db_session):
    event_1 = await persist_pinterest_fsm_event(
        db_session,
        device_id=2,
        payload={
            "eventId": "evt_duplicate",
            "ts": 100,
            "taskId": "ptask_orphan",
            "traceId": "ptrace_orphan",
            "fsm": "pinterest_health_check",
            "state": "OPEN_PINTEREST",
            "action": {"name": "launch_app", "target": "com.pinterest", "result": "started"},
            "message": "Opening Pinterest",
        },
    )
    await db_session.flush()
    event_2 = await persist_pinterest_fsm_event(
        db_session,
        device_id=2,
        payload={
            "eventId": "evt_duplicate",
            "ts": 101,
            "taskId": "ptask_orphan",
            "traceId": "ptrace_orphan",
            "fsm": "pinterest_health_check",
            "state": "OPEN_PINTEREST",
            "action": {"name": "launch_app"},
            "message": "Duplicate resend",
        },
    )

    assert event_2.id == event_1.id
    rows = (await db_session.execute(select(PinterestTaskEvent))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_pinterest_fsm_event_from_phone_is_persisted_and_broadcast(db_engine: AsyncEngine, seed_db):
    del seed_db
    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    manager = DeviceConnectionManager()
    broadcaster = AsyncMock()
    broadcaster.broadcast = AsyncMock()
    _wire_device_events(manager, broadcaster, session_factory=session_factory)

    payload = {
        "eventId": "evt_ws_1",
        "ts": 200,
        "taskId": "ptask_ws",
        "traceId": "ptrace_ws",
        "fsm": "pinterest_health_check",
        "state": "CHECK_LOGIN",
        "action": {"name": "inspect", "target": "home_navigation", "result": "success"},
        "nextAction": {"name": "done", "etaMs": 50},
        "message": "Pinterest health check",
    }
    await manager.handle_message(
        1,
        WSMessage(type=MessageType.EVENT_PINTEREST_FSM, payload=payload),
    )

    async with session_factory() as session:
        saved = (
            await session.execute(
                select(PinterestTaskEvent).where(PinterestTaskEvent.event_id == "evt_ws_1")
            )
        ).scalar_one()
        assert saved.device_id == 1
        assert saved.state == "CHECK_LOGIN"
        assert saved.next_action_at_ms == 250

    broadcaster.broadcast.assert_any_await(
        "pinterest:fsm",
        {
            "device_id": 1,
            **payload,
        },
    )
