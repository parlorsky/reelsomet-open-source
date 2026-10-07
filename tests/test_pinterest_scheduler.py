from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.config import VPSConfig
from server.models import (
    Base,
    Device,
    PinterestAccount,
    PinterestAsset,
    PinterestBoard,
    PinterestImport,
    PinterestPin,
    PinterestPostAttempt,
    PinterestSchedulerSettings,
)
from server.pinterest.device_calendar import DeviceCalendarDecision
from server.scheduler import FarmScheduler
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def ws_manager() -> MagicMock:
    mgr = MagicMock(spec=DeviceConnectionManager)
    mgr.is_online = MagicMock(return_value=True)
    mgr.get_online_device_ids = MagicMock(return_value=[1])
    return mgr


@pytest.fixture
def bridge() -> AsyncMock:
    b = AsyncMock(spec=DeviceBridge)
    b.pinterest_ensure_board = AsyncMock(return_value={"success": True, "status": "ok"})
    b.pinterest_publish_pin = AsyncMock(return_value={"success": True, "status": "ok"})
    b.push_image_assets = AsyncMock(return_value={"success": True, "status": "ok", "downloadedCount": 1})
    return b


@pytest.fixture
def config() -> VPSConfig:
    return VPSConfig(
        farm_poll_interval_seconds=30,
        farm_health_check_interval_seconds=60,
        farm_result_poll_interval_seconds=15,
    )


@pytest_asyncio.fixture
async def scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    ws_manager: MagicMock,
    bridge: AsyncMock,
    config: VPSConfig,
) -> AsyncIterator[FarmScheduler]:
    scheduler = FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=AsyncMock(spec=AdminBroadcaster),
    )
    try:
        yield scheduler
    finally:
        await scheduler.stop()


async def _seed_pinterest_pin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    board_status: str = "active",
    pin_status: str = "ready",
    asset_staged: bool = True,
    scheduler_kwargs: dict[str, Any] | None = None,
) -> tuple[int, int, int]:
    async with session_factory() as session:
        device = Device(
            device_id="HONOR-PINTEREST",
            name="Honor",
            ip_address="127.0.0.1",
            status="online",
        )
        session.add(device)
        await session.flush()

        account = PinterestAccount(
            username="demo_creator",
            device_id=device.id,
            status="active",
            app_installed=True,
            gallery_permission_granted=True,
        )
        session.add(account)
        await session.flush()
        settings_values: dict[str, Any] = {
            "enabled": True,
            "target_pins_per_day": 10,
            **(scheduler_kwargs or {}),
        }
        session.add(PinterestSchedulerSettings(account_id=account.id, **settings_values))

        imp = PinterestImport(
            import_id="pin_import_001",
            platform="pinterest",
            manifest_hash="hash",
            manifest_json="{}",
            status="imported",
        )
        session.add(imp)
        await session.flush()

        asset = PinterestAsset(
            import_id=imp.id,
            original_file="a.jpg",
            storage_path="/tmp/a.jpg",
            phone_storage_path="/storage/emulated/0/Pictures/Reelsomet/a.jpg",
            phone_staged_at=datetime.utcnow() if asset_staged else None,
            media_hash="sha256",
            mime_type="image/jpeg",
            status="ready",
        )
        board = PinterestBoard(
            account_id=account.id,
            source_import_id=imp.id,
            key="mirror",
            name="Mirror Selfies",
            description="Mirror ideas.",
            visibility="public",
            status=board_status,
        )
        session.add_all([asset, board])
        await session.flush()

        pin = PinterestPin(
            external_id="pin_001",
            account_id=account.id,
            board_id=board.id,
            asset_id=asset.id,
            source_import_id=imp.id,
            title="Mirror pose",
            description="Clean pose idea.",
            priority=100,
            order_index=0,
            status=pin_status,
        )
        session.add(pin)
        await session.commit()
        return account.id, board.id, pin.id


async def _add_posted_pinterest_pin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    template_pin_id: int,
    external_id: str,
    posted_at: datetime,
) -> None:
    async with session_factory() as session:
        template = await session.get(PinterestPin, template_pin_id)
        assert template is not None
        posted = PinterestPin(
            external_id=external_id,
            account_id=template.account_id,
            board_id=template.board_id,
            asset_id=template.asset_id,
            source_import_id=template.source_import_id,
            title="Already posted",
            description="Already posted.",
            priority=50,
            order_index=99,
            status="posted",
            posted_at=posted_at,
        )
        session.add(posted)
        await session.commit()


async def _add_pinterest_publish_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    template_pin_id: int,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        template = await session.get(PinterestPin, template_pin_id)
        assert template is not None
        attempt = PinterestPostAttempt(
            task_id="pinterest-pin-recent-attempt",
            trace_id="ptrace-recent-attempt",
            task_type="pinterest.publish_pin",
            status=status,
            account_id=template.account_id,
            board_id=template.board_id,
            pin_id=template.id,
            started_at=started_at,
            finished_at=finished_at,
            error_code="publish_verify_failed" if status == "failed" else None,
            error_message="publish_verify_failed" if status == "failed" else None,
        )
        session.add(attempt)
        await session.commit()


def _assert_no_link_key(value: Any) -> None:
    if isinstance(value, dict):
        assert "link" not in value
        assert "destination_url" not in value
        for child in value.values():
            _assert_no_link_key(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_link_key(child)


@pytest.mark.asyncio
async def test_pinterest_scheduler_skips_when_device_calendar_blocks(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, pin_id = await _seed_pinterest_pin(session_factory)

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(False, "device_fsm_busy")),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_not_called()
    bridge.pinterest_ensure_board.assert_not_called()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        assert pin.status == "ready"


@pytest.mark.asyncio
async def test_pinterest_scheduler_ensures_board_before_publishing_pin(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, board_id, pin_id = await _seed_pinterest_pin(session_factory, board_status="needs_create")

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_ensure_board.assert_awaited_once()
    bridge.pinterest_publish_pin.assert_not_called()
    payload = bridge.pinterest_ensure_board.await_args.args[1]
    assert payload["board"]["name"] == "Mirror Selfies"
    assert payload["board"]["visibility"] == "public"

    async with session_factory() as session:
        board = await session.get(PinterestBoard, board_id)
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert board.status == "active"
        assert pin.status == "ready"
        assert attempts[0].task_type == "pinterest.ensure_board"
        assert attempts[0].status == "success"


@pytest.mark.asyncio
async def test_pinterest_scheduler_publishes_active_board_pin_without_destination_link(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, pin_id = await _seed_pinterest_pin(session_factory, board_status="active")

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_awaited_once()
    bridge.push_image_assets.assert_awaited_once()
    bridge.pinterest_ensure_board.assert_not_called()
    payload = bridge.pinterest_publish_pin.await_args.args[1]
    assert payload["pin"]["title"] == "Mirror pose"
    assert payload["pin"]["description"] == "Clean pose idea."
    assert payload["board"]["name"] == "Mirror Selfies"
    assert payload["media"]["phone_storage_path"] == "/storage/emulated/0/Pictures/Reelsomet/a.jpg"
    _assert_no_link_key(payload)

    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert pin.status == "posted"
        assert pin.posted_at is not None
        assert attempts[0].task_type == "pinterest.publish_pin"
        assert attempts[0].status == "success"


@pytest.mark.asyncio
async def test_pinterest_scheduler_respects_min_gap_between_successful_posts(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 16, 8, 30, 0)
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        scheduler_kwargs={"min_gap_minutes": 70},
    )
    await _add_posted_pinterest_pin(
        session_factory,
        template_pin_id=pin_id,
        external_id="pin_recent",
        posted_at=now - timedelta(minutes=30),
    )

    with (
        patch("server.scheduler._utcnow", return_value=now),
        patch(
            "server.scheduler.check_device_calendar",
            new=AsyncMock(return_value=DeviceCalendarDecision(True)),
        ),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_not_called()
    bridge.push_image_assets.assert_not_called()
    bridge.pinterest_ensure_board.assert_not_called()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert pin.status == "ready"
        assert attempts == []


@pytest.mark.asyncio
async def test_pinterest_scheduler_respects_daily_target_in_account_timezone(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 16, 8, 30, 0)
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        scheduler_kwargs={
            "timezone": "America/New_York",
            "target_pins_per_day": 1,
            "min_gap_minutes": 0,
        },
    )
    await _add_posted_pinterest_pin(
        session_factory,
        template_pin_id=pin_id,
        external_id="pin_today",
        posted_at=now - timedelta(hours=2),
    )

    with (
        patch("server.scheduler._utcnow", return_value=now),
        patch(
            "server.scheduler.check_device_calendar",
            new=AsyncMock(return_value=DeviceCalendarDecision(True)),
        ),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_not_called()
    bridge.push_image_assets.assert_not_called()
    bridge.pinterest_ensure_board.assert_not_called()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert pin.status == "ready"
        assert attempts == []


@pytest.mark.asyncio
async def test_pinterest_scheduler_skips_outside_posting_window(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 16, 8, 30, 0)  # 04:30 in America/New_York
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        scheduler_kwargs={
            "timezone": "America/New_York",
            "posting_windows_json": '[{"start":"09:00","end":"10:00"}]',
            "min_gap_minutes": 0,
        },
    )

    with (
        patch("server.scheduler._utcnow", return_value=now),
        patch(
            "server.scheduler.check_device_calendar",
            new=AsyncMock(return_value=DeviceCalendarDecision(True)),
        ),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_not_called()
    bridge.push_image_assets.assert_not_called()
    bridge.pinterest_ensure_board.assert_not_called()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert pin.status == "ready"
        assert attempts == []


@pytest.mark.asyncio
async def test_pinterest_scheduler_posts_inside_posting_window(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 16, 13, 30, 0)  # 09:30 in America/New_York
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        scheduler_kwargs={
            "timezone": "America/New_York",
            "posting_windows_json": '[{"start":"09:00","end":"10:00"}]',
            "min_gap_minutes": 0,
        },
    )

    with (
        patch("server.scheduler._utcnow", return_value=now),
        patch(
            "server.scheduler.check_device_calendar",
            new=AsyncMock(return_value=DeviceCalendarDecision(True)),
        ),
    ):
        await scheduler.process_pinterest_pins()

    bridge.push_image_assets.assert_awaited_once()
    bridge.pinterest_publish_pin.assert_awaited_once()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        assert pin.status == "posted"


@pytest.mark.asyncio
async def test_pinterest_scheduler_applies_min_gap_after_failed_publish_attempt(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    now = datetime(2026, 5, 16, 8, 30, 0)
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        scheduler_kwargs={"min_gap_minutes": 70},
    )
    await _add_pinterest_publish_attempt(
        session_factory,
        template_pin_id=pin_id,
        status="failed",
        started_at=now - timedelta(minutes=12),
        finished_at=now - timedelta(minutes=10),
    )

    with (
        patch("server.scheduler._utcnow", return_value=now),
        patch(
            "server.scheduler.check_device_calendar",
            new=AsyncMock(return_value=DeviceCalendarDecision(True)),
        ),
    ):
        await scheduler.process_pinterest_pins()

    bridge.pinterest_publish_pin.assert_not_called()
    bridge.push_image_assets.assert_not_called()
    bridge.pinterest_ensure_board.assert_not_called()
    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(select(PinterestPostAttempt).order_by(PinterestPostAttempt.id))
        ).scalars().all()
        assert pin.status == "ready"
        assert len(attempts) == 1


@pytest.mark.asyncio
async def test_pinterest_scheduler_stages_asset_before_publishing(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        asset_staged=False,
    )

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    bridge.push_image_assets.assert_awaited_once()
    bridge.pinterest_publish_pin.assert_awaited_once()
    stage_payload = bridge.push_image_assets.await_args.kwargs["assets"][0]
    assert stage_payload["filename"] == "a.jpg"
    assert "/api/pinterest/assets/" in stage_payload["url"]

    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        asset = await session.get(PinterestAsset, pin.asset_id)
        assert asset.phone_staged_at is not None
        assert asset.last_error is None
        assert pin.status == "posted"


@pytest.mark.asyncio
async def test_pinterest_scheduler_refreshes_staged_asset_before_publishing(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
) -> None:
    _, _, pin_id = await _seed_pinterest_pin(
        session_factory,
        board_status="active",
        asset_staged=True,
    )

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    bridge.push_image_assets.assert_awaited_once()
    bridge.pinterest_publish_pin.assert_awaited_once()
    stage_payload = bridge.push_image_assets.await_args.kwargs["assets"][0]
    assert stage_payload["filename"] == "a.jpg"

    async with session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        asset = await session.get(PinterestAsset, pin.asset_id)
        assert asset.phone_staged_at is not None
        assert asset.last_error is None
        assert pin.status == "posted"
