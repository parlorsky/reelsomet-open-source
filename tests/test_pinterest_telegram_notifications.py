from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
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
    PinterestSchedulerSettings,
)
from server.pinterest.device_calendar import DeviceCalendarDecision
from server.scheduler import FarmScheduler
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
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
    return b


@pytest.fixture
def telegram_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.send_notification = AsyncMock()
    return bot


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
    telegram_bot: AsyncMock,
) -> AsyncIterator[FarmScheduler]:
    scheduler = FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=AsyncMock(spec=AdminBroadcaster),
        telegram_bot=telegram_bot,
    )
    try:
        yield scheduler
    finally:
        await scheduler.stop()


async def _seed_pinterest_pin(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_retries: int = 1,
) -> None:
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
        session.add(
            PinterestSchedulerSettings(
                account_id=account.id,
                enabled=True,
                target_pins_per_day=10,
                max_retries=max_retries,
            )
        )

        imp = PinterestImport(
            import_id="pin_import_telegram",
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
            phone_staged_at=datetime.utcnow(),
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
            status="active",
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
            status="ready",
        )
        session.add(pin)
        await session.commit()


def _notification_text(telegram_bot: AsyncMock) -> str:
    telegram_bot.send_notification.assert_awaited_once()
    return str(telegram_bot.send_notification.await_args.args[0])


def _assert_pin_context(text: str) -> None:
    assert "demo_creator" in text
    assert "Mirror Selfies" in text
    assert "Mirror pose" in text
    assert "pin_001" in text


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="KNOWN-001: Pinterest terminal Telegram notifications are not wired; see docs/status.md")
async def test_successful_pinterest_pin_publish_sends_telegram_notification(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    telegram_bot: AsyncMock,
) -> None:
    await _seed_pinterest_pin(session_factory)

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    text = _notification_text(telegram_bot)
    _assert_pin_context(text)
    assert "success" in text.lower()


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="KNOWN-001: Pinterest terminal Telegram notifications are not wired; see docs/status.md")
async def test_terminal_pinterest_pin_publish_failure_sends_telegram_notification(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
    telegram_bot: AsyncMock,
) -> None:
    await _seed_pinterest_pin(session_factory, max_retries=1)
    bridge.pinterest_publish_pin.return_value = {
        "success": False,
        "status": "failed",
        "error_code": "publish_timeout",
        "error_message": "Timed out after Create button",
    }

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    text = _notification_text(telegram_bot)
    _assert_pin_context(text)
    assert "failed" in text.lower()
    assert "Timed out after Create button" in text


@pytest.mark.asyncio
async def test_retryable_pinterest_pin_publish_failure_does_not_notify(
    scheduler: FarmScheduler,
    session_factory: async_sessionmaker[AsyncSession],
    bridge: AsyncMock,
    telegram_bot: AsyncMock,
) -> None:
    await _seed_pinterest_pin(session_factory, max_retries=3)
    bridge.pinterest_publish_pin.return_value = {
        "success": False,
        "status": "failed",
        "error_code": "temporary_timeout",
        "error_message": "Temporary timeout",
    }

    with patch(
        "server.scheduler.check_device_calendar",
        new=AsyncMock(return_value=DeviceCalendarDecision(True)),
    ):
        await scheduler.process_pinterest_pins()

    telegram_bot.send_notification.assert_not_awaited()
