"""Tests for server.telegram — VPS Telegram bot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.config import VPSConfig
from server.models import Account, Base, Device, PostLog, Video
from server.telegram import VPSTelegramBot
from server.ws.manager import DeviceConnectionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bot_config() -> VPSConfig:
    return VPSConfig(
        telegram_bot_token="123456:ABC-TEST-TOKEN",
        telegram_admin_chat_ids=[111, 222],
        domain="test.example.com",
        llm_model="test-model",
    )


@pytest.fixture
def empty_token_config() -> VPSConfig:
    return VPSConfig(
        telegram_bot_token="",
        telegram_admin_chat_ids=[111],
    )


@pytest.fixture
def ws_manager() -> DeviceConnectionManager:
    return DeviceConnectionManager()


@pytest_asyncio.fixture
async def db_engine() -> Any:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def seeded_factory(
    db_engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """Seed test data and return the session factory."""
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        dev1 = Device(
            device_id="DEV-1", name="Test Realme", ip_address="192.168.1.10",
            port=8080, device_model="RMX3771", android_version="15",
            is_active=True, status="online", last_seen_at=now - timedelta(minutes=2),
        )
        dev2 = Device(
            device_id="DEV-2", name="Test Honor", ip_address="192.168.1.11",
            port=8080, device_model="FNE-NX9", android_version="14",
            is_active=True, status="offline", last_seen_at=now - timedelta(hours=1),
        )
        session.add_all([dev1, dev2])
        await session.flush()

        acc1 = Account(
            username="prosto.o.lubvi", is_active=True, total_posted=45,
            total_failed=2, recreator_model="blonde",
        )
        acc2 = Account(
            username="julia1999uch", is_active=True, is_paused=True,
            total_posted=12, total_failed=0,
        )
        acc3 = Account(
            username="engagement_acc", is_active=True, engagement_enabled=True,
            total_posted=20, total_failed=1,
        )
        session.add_all([acc1, acc2, acc3])
        await session.flush()

        vid1 = Video(
            filename="rec_blonde_sunset_143022.mp4",
            account_username="prosto.o.lubvi", status="pending",
        )
        vid2 = Video(
            filename="rec_dark_love_092011.mp4",
            account_username="julia1999uch", status="posted",
            posted_at=now - timedelta(hours=1),
        )
        vid3 = Video(
            filename="rec_failed.mp4",
            account_username="prosto.o.lubvi", status="failed",
            post_error="Upload timeout",
        )
        vid4 = Video(
            filename="rec_scheduled.mp4",
            account_username="prosto.o.lubvi", status="scheduled",
            scheduled_time=now + timedelta(hours=2),
        )
        session.add_all([vid1, vid2, vid3, vid4])
        await session.flush()

        log1 = PostLog(
            account_username="prosto.o.lubvi", result="success",
            duration_ms=45000,
        )
        log2 = PostLog(
            account_username="julia1999uch", result="failed",
            error_message="timeout",
        )
        session.add_all([log1, log2])
        await session.commit()

    return session_factory


def _make_update(user_id: int, text: str = "/start") -> MagicMock:
    """Create a mock Update with a message from the given user."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.message.text = text
    update.callback_query = None
    return update


def _make_callback_update(user_id: int, callback_data: str) -> MagicMock:
    """Create a mock Update with a callback query."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = None
    update.callback_query = AsyncMock()
    update.callback_query.data = callback_data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = AsyncMock()
    return update


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBotCreation:

    @pytest.mark.asyncio
    async def test_bot_creates_application(
        self, bot_config: VPSConfig, session_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """VPSTelegramBot.start() builds and starts a PTB Application."""
        bot = VPSTelegramBot(bot_config, session_factory, ws_manager)

        with patch("server.telegram.Application") as MockApp:
            mock_builder = MagicMock()
            mock_app_instance = AsyncMock()
            mock_app_instance.updater = AsyncMock()
            mock_app_instance.updater.start_polling = AsyncMock()
            mock_app_instance.updater.running = True
            mock_app_instance.running = True
            mock_app_instance.bot = AsyncMock()
            mock_app_instance.add_handler = MagicMock()

            mock_builder.token.return_value = mock_builder
            mock_builder.build.return_value = mock_app_instance
            MockApp.builder.return_value = mock_builder

            await bot.start()

            mock_builder.token.assert_called_once_with("123456:ABC-TEST-TOKEN")
            mock_app_instance.initialize.assert_awaited_once()
            mock_app_instance.start.assert_awaited_once()
            mock_app_instance.updater.start_polling.assert_awaited_once()

            # Cleanup
            await bot.stop()

    @pytest.mark.asyncio
    async def test_bot_skips_if_no_token(
        self, empty_token_config: VPSConfig, session_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """No crash when telegram_bot_token is empty."""
        bot = VPSTelegramBot(empty_token_config, session_factory, ws_manager)
        await bot.start()
        assert bot._app is None
        # stop should also be safe
        await bot.stop()


class TestAdminGuard:

    @pytest.mark.asyncio
    async def test_admin_guard_allows_admin(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Admin user (id=111) is allowed to run commands."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=111, text="/status")
        context = MagicMock()

        await bot._cmd_status(update, context)

        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args[0][0]
        assert "Reelsomet VPS Status" in text

    @pytest.mark.asyncio
    async def test_admin_guard_blocks_non_admin(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Non-admin user gets 'Unauthorized'."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=999, text="/status")
        context = MagicMock()

        await bot._cmd_status(update, context)

        update.message.reply_text.assert_awaited_once_with("Unauthorized")


class TestCommands:

    @pytest.mark.asyncio
    async def test_status_command(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """The /status command returns a formatted dashboard."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=111, text="/status")
        context = MagicMock()

        await bot._cmd_status(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Devices:" in text
        assert "Accounts:" in text
        assert "Queue:" in text
        assert "Posted today:" in text

    @pytest.mark.asyncio
    async def test_devices_command(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """The /devices command lists all devices with inline keyboards."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=111, text="/devices")
        context = MagicMock()

        await bot._cmd_devices(update, context)

        call_kwargs = update.message.reply_text.call_args
        text = call_kwargs[0][0]
        assert "Test Realme" in text
        assert "Test Honor" in text
        # Should have inline keyboard
        assert call_kwargs[1]["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_accounts_command(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """The /accounts command lists all accounts."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=111, text="/accounts")
        context = MagicMock()

        await bot._cmd_accounts(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "prosto.o.lubvi" in text
        assert "julia1999uch" in text
        assert "Posted: 45" in text
        assert "Paused" in text

    @pytest.mark.asyncio
    async def test_queue_command(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """The /queue command shows queue summary and recent videos."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)
        update = _make_update(user_id=111, text="/queue")
        context = MagicMock()

        await bot._cmd_queue(update, context)

        text = update.message.reply_text.call_args[0][0]
        assert "Video Queue" in text
        assert "Pending:" in text
        assert "Scheduled:" in text
        assert "Recent:" in text


class TestNotifications:

    @pytest.mark.asyncio
    async def test_send_notification(
        self, bot_config: VPSConfig, session_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """send_notification sends to all admin chat_ids."""
        bot = VPSTelegramBot(bot_config, session_factory, ws_manager)
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        mock_app.bot.send_message = AsyncMock()
        bot._app = mock_app

        await bot.send_notification("Test message")

        assert mock_app.bot.send_message.await_count == 2
        calls = mock_app.bot.send_message.call_args_list
        chat_ids_called = {c[1]["chat_id"] for c in calls}
        assert chat_ids_called == {111, 222}

    @pytest.mark.asyncio
    async def test_notify_post_success(
        self, bot_config: VPSConfig, session_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """notify_post_success formats a success message with duration."""
        bot = VPSTelegramBot(bot_config, session_factory, ws_manager)
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        mock_app.bot.send_message = AsyncMock()
        bot._app = mock_app

        await bot.notify_post_success("prosto.o.lubvi", "reel_001.mp4", 45000)

        text = mock_app.bot.send_message.call_args[1]["text"]
        assert "Post success" in text
        assert "prosto.o.lubvi" in text
        assert "reel_001.mp4" in text
        assert "45.0s" in text

    @pytest.mark.asyncio
    async def test_notify_post_failure(
        self, bot_config: VPSConfig, session_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """notify_post_failure formats a failure message with error."""
        bot = VPSTelegramBot(bot_config, session_factory, ws_manager)
        mock_app = MagicMock()
        mock_app.bot = AsyncMock()
        mock_app.bot.send_message = AsyncMock()
        bot._app = mock_app

        await bot.notify_post_failure("julia1999uch", "reel_002.mp4", "Upload timeout")

        text = mock_app.bot.send_message.call_args[1]["text"]
        assert "Post failed" in text
        assert "julia1999uch" in text
        assert "Upload timeout" in text


class TestCallbacks:

    @pytest.mark.asyncio
    async def test_callback_ping_device(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Inline ping button triggers WS ping."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)

        # We need a device ID. Query the DB for the first device.
        async with seeded_factory() as session:
            result = await session.execute(select(Device).limit(1))
            dev = result.scalar_one()
            device_id = dev.id

        update = _make_callback_update(user_id=111, callback_data=f"dev:ping:{device_id}")
        context = MagicMock()

        # Device is offline (not in ws_manager), so expect offline message
        await bot._on_callback(update, context)

        update.callback_query.answer.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "offline" in text.lower()

    @pytest.mark.asyncio
    async def test_callback_ping_online_device(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Ping an online device calls ws_manager.ping_device."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)

        async with seeded_factory() as session:
            result = await session.execute(select(Device).limit(1))
            dev = result.scalar_one()
            device_id = dev.id

        # Fake the device as online
        mock_ws = AsyncMock()
        await ws_manager.connect(device_id, mock_ws)

        update = _make_callback_update(user_id=111, callback_data=f"dev:ping:{device_id}")
        context = MagicMock()

        with patch.object(ws_manager, "ping_device", new_callable=AsyncMock, return_value=True):
            await bot._on_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "pong" in text.lower()

        # Cleanup
        await ws_manager.disconnect(device_id)

    @pytest.mark.asyncio
    async def test_callback_account_pause(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Pause toggle via inline button updates the DB."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)

        # Verify account starts not paused
        async with seeded_factory() as session:
            result = await session.execute(
                select(Account).where(Account.username == "prosto.o.lubvi"),
            )
            acc = result.scalar_one()
            assert not acc.is_paused

        update = _make_callback_update(
            user_id=111, callback_data="acc:pause:prosto.o.lubvi",
        )
        context = MagicMock()

        await bot._on_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "paused" in text.lower()

        # Verify DB was updated
        async with seeded_factory() as session:
            result = await session.execute(
                select(Account).where(Account.username == "prosto.o.lubvi"),
            )
            acc = result.scalar_one()
            assert acc.is_paused is True

    @pytest.mark.asyncio
    async def test_callback_device_detail(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Device detail callback shows device information."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)

        async with seeded_factory() as session:
            result = await session.execute(select(Device).limit(1))
            dev = result.scalar_one()
            device_id = dev.id

        update = _make_callback_update(
            user_id=111, callback_data=f"dev:detail:{device_id}",
        )
        context = MagicMock()

        await bot._on_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "Test Realme" in text
        assert "RMX3771" in text
        assert "DEV-1" in text

    @pytest.mark.asyncio
    async def test_callback_account_detail(
        self, bot_config: VPSConfig, seeded_factory: async_sessionmaker, ws_manager: DeviceConnectionManager,
    ) -> None:
        """Account detail callback shows account information."""
        bot = VPSTelegramBot(bot_config, seeded_factory, ws_manager)

        update = _make_callback_update(
            user_id=111, callback_data="acc:detail:prosto.o.lubvi",
        )
        context = MagicMock()

        await bot._on_callback(update, context)

        text = update.callback_query.edit_message_text.call_args[0][0]
        assert "prosto.o.lubvi" in text
        assert "Posted: 45" in text
        assert "blonde" in text
