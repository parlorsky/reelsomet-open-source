"""Tests for server.scheduler: FarmScheduler periodic jobs."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.config import VPSConfig
from server.models import (
    Account,
    AccountDevice,
    Base,
    Device,
    EngagementAction,
    EngagementSession,
    EngagementTarget,
    GenerationRun,
    InsightsSnapshot,
    PostLog,
    Video,
)
from server.scheduler import FarmScheduler
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def engine() -> Any:
    """In-memory async SQLite engine with all tables created."""
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
    mgr.is_online = MagicMock(return_value=False)
    mgr.get_online_device_ids = MagicMock(return_value=[])
    return mgr


@pytest.fixture
def bridge() -> AsyncMock:
    b = AsyncMock(spec=DeviceBridge)
    b.send_schedule = AsyncMock(return_value={"status": "ok"})
    b.get_post_logs = AsyncMock(return_value={"logs": []})
    b.start_engagement = AsyncMock(return_value={"started": True})
    b.get_engagement_status = AsyncMock(return_value={"active": False})
    b.get_engagement_actions = AsyncMock(return_value={"actions": []})
    b.start_insights = AsyncMock(return_value={"started": True})
    b.get_insights_status = AsyncMock(return_value={"active": False})
    b.get_insights = AsyncMock(return_value={"snapshots": []})
    b.get_status = AsyncMock(return_value={"activeMode": "NONE"})
    return b


@pytest.fixture
def broadcaster() -> AsyncMock:
    bc = AsyncMock(spec=AdminBroadcaster)
    bc.broadcast = AsyncMock()
    return bc


@pytest.fixture
def config() -> VPSConfig:
    return VPSConfig(
        farm_default_posting_times=["10:00", "14:00", "18:00"],
        farm_timezone="UTC",
        farm_max_posts_per_account_per_day=3,
        farm_upload_window_minutes=10,
        farm_max_auto_retries=3,
        farm_auto_retry_delay_minutes=5,
        farm_action_blocked_pause_hours=48.0,
        farm_schedule_jitter_std_seconds=0,  # no jitter for deterministic tests
        farm_poll_interval_seconds=30,
        farm_health_check_interval_seconds=60,
        farm_result_poll_interval_seconds=15,
        farm_device_silent_timeout_seconds=90,
    )


@pytest_asyncio.fixture
async def scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    ws_manager: MagicMock,
    bridge: AsyncMock,
    config: VPSConfig,
    broadcaster: AsyncMock,
) -> AsyncIterator[FarmScheduler]:
    scheduler = FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=broadcaster,
    )
    try:
        yield scheduler
    finally:
        await scheduler.stop()


async def _seed_device_and_account(
    session_factory: async_sessionmaker[AsyncSession],
    device_status: str = "online",
    account_kwargs: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Seed one device + one account + link them. Return (device.id, username)."""
    async with session_factory() as session:
        dev = Device(
            device_id="TEST-SERIAL",
            name="Test Device",
            ip_address="192.168.1.10",
            status=device_status,
            last_seen_at=datetime.utcnow(),
        )
        session.add(dev)
        await session.flush()

        acct_kw: dict[str, Any] = {
            "username": "test_user",
            "is_active": True,
            **(account_kwargs or {}),
        }
        acct = Account(**acct_kw)
        session.add(acct)
        await session.flush()

        link = AccountDevice(
            account_username=acct.username,
            device_id=dev.id,
            is_primary=True,
        )
        session.add(link)
        await session.commit()
        return dev.id, acct.username


async def _add_video(
    session_factory: async_sessionmaker[AsyncSession],
    username: str,
    device_id: int | None = None,
    **kwargs: Any,
) -> int:
    """Add a Video and return its id."""
    defaults: dict[str, Any] = {
        "filename": "reel.mp4",
        "account_username": username,
        "status": "pending",
        "device_id": device_id,
    }
    defaults.update(kwargs)
    async with session_factory() as session:
        vid = Video(**defaults)
        session.add(vid)
        await session.commit()
        return vid.id


# ---------------------------------------------------------------------------
# Tests: process_pending_videos
# ---------------------------------------------------------------------------

class TestRecoverStaleVideos:

    @pytest.mark.asyncio
    async def test_recover_stale_uploading_video(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        config: VPSConfig,
    ) -> None:
        """A video stuck in 'uploading' beyond the cutoff is reset to 'pending'."""
        dev_id, username = await _seed_device_and_account(session_factory)
        # updated_at must be old enough: cutoff = upload_window_minutes * 3
        stale_time = datetime.utcnow() - timedelta(
            minutes=config.farm_upload_window_minutes * 3 + 1,
        )
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", retry_count=0,
        )
        # Manually set updated_at to a stale timestamp
        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            vid.updated_at = stale_time
            await session.commit()

        await scheduler.recover_stale_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"
            assert vid.uploaded_to_phone is False
            assert vid.retry_count == 1

    @pytest.mark.asyncio
    async def test_recover_stale_scheduled_video(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        config: VPSConfig,
    ) -> None:
        """A video stuck in 'scheduled' beyond the cutoff is reset to 'pending'."""
        dev_id, username = await _seed_device_and_account(session_factory)
        stale_time = datetime.utcnow() - timedelta(
            minutes=config.farm_upload_window_minutes * 3 + 1,
        )
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled", retry_count=1,
        )
        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            vid.updated_at = stale_time
            await session.commit()

        await scheduler.recover_stale_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"
            assert vid.uploaded_to_phone is False
            assert vid.retry_count == 2

    @pytest.mark.asyncio
    async def test_recover_stale_exhausted_retries(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        config: VPSConfig,
    ) -> None:
        """A stale video that has reached max retries is marked 'failed'."""
        dev_id, username = await _seed_device_and_account(session_factory)
        stale_time = datetime.utcnow() - timedelta(
            minutes=config.farm_upload_window_minutes * 3 + 1,
        )
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading",
            retry_count=config.farm_max_auto_retries,  # already at max
        )
        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            vid.updated_at = stale_time
            await session.commit()

        await scheduler.recover_stale_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "failed"
            assert vid.post_error == "Max retries exceeded (stale recovery)"


class TestProcessPendingVideos:

    @pytest.mark.asyncio
    async def test_schedules_online_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A due pending video is dispatched to an online device."""
        dev_id, username = await _seed_device_and_account(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled,
            uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_called_once()
        call_payload = bridge.send_schedule.call_args[0][1]
        # Verify nested accounts[].videos[] format (Android contract)
        assert "accounts" in call_payload
        assert len(call_payload["accounts"]) == 1
        acct_entry = call_payload["accounts"][0]
        assert acct_entry["username"] == username
        assert len(acct_entry["videos"]) == 1
        vid_entry = acct_entry["videos"][0]
        assert vid_entry["filename"] == "reel.mp4"
        assert isinstance(vid_entry["scheduledTimeMs"], int)
        assert vid_entry["scheduledTimeMs"] == 0

        # Video status should now be "scheduled"
        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "scheduled"

        broadcaster.broadcast.assert_called_once_with("queue:update", {
            "dispatched": 1,
            "ts": broadcaster.broadcast.call_args[0][1]["ts"],  # dynamic
        })

    @pytest.mark.asyncio
    async def test_skips_story_rows_until_android_story_posting_exists(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Story rows are not sent to the phone's Reel posting flow."""
        dev_id, username = await _seed_device_and_account(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            filename="story.png",
            content_type="story",
            scheduled_time=scheduled,
            uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()
        bridge.send_video_download.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"

    @pytest.mark.asyncio
    async def test_skips_offline_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A pending video is NOT dispatched when the device is offline."""
        dev_id, username = await _seed_device_and_account(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled,
        )

        ws_manager.is_online.return_value = False

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"

    @pytest.mark.asyncio
    async def test_skips_inactive_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A due video is not dispatched after its account is deactivated."""
        dev_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"is_active": False},
        )
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            scheduled_time=scheduled,
            uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()
        bridge.send_video_download.assert_not_called()
        broadcaster.broadcast.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"
            assert vid.device_id == dev_id

    @pytest.mark.asyncio
    async def test_skips_inactive_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A due video is not dispatched after its linked device is deactivated."""
        dev_id, username = await _seed_device_and_account(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            scheduled_time=scheduled,
            uploaded_to_phone=True,
        )

        async with session_factory() as session:
            device = await session.get(Device, dev_id)
            assert device is not None
            device.is_active = False
            await session.commit()

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()
        bridge.send_video_download.assert_not_called()
        broadcaster.broadcast.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"
            assert vid.device_id == dev_id

    @pytest.mark.asyncio
    async def test_respects_upload_window(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A video scheduled far in the future is not dispatched yet."""
        dev_id, username = await _seed_device_and_account(session_factory)
        # Schedule 2 hours from now -- well outside the 10-minute upload window
        scheduled = datetime.utcnow() + timedelta(hours=2)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"

    @pytest.mark.asyncio
    async def test_skips_paused_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A due video is skipped when its account got paused after scheduling."""
        dev_id, username = await _seed_device_and_account(
            session_factory, account_kwargs={"is_paused": True},
        )
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled, uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()
        bridge.send_video_download.assert_not_called()
        broadcaster.broadcast.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"

    @pytest.mark.asyncio
    async def test_skips_blocked_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A due video is skipped when its account is blocked (blocked_until in future)."""
        future = datetime.utcnow() + timedelta(hours=2)
        dev_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"is_blocked": True, "blocked_until": future},
        )
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled, uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_not_called()
        bridge.send_video_download.assert_not_called()
        broadcaster.broadcast.assert_not_called()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "pending"

    @pytest.mark.asyncio
    async def test_resumes_after_blocked_until_expired(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Dispatch resumes automatically once blocked_until has elapsed.

        Even if `is_blocked` is still True (the unblock job hasn't yet
        flipped the flag), an elapsed `blocked_until` means the temporary
        action-block window is over and queued videos may proceed.
        """
        past = datetime.utcnow() - timedelta(hours=1)
        dev_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"is_blocked": True, "blocked_until": past},
        )
        scheduled = datetime.utcnow() - timedelta(seconds=5)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            scheduled_time=scheduled, uploaded_to_phone=True,
        )

        ws_manager.is_online.return_value = True

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_called_once()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "scheduled"


# ---------------------------------------------------------------------------
# Tests: collect_post_results
# ---------------------------------------------------------------------------

class TestCollectPostResults:

    @pytest.mark.asyncio
    async def test_updates_video_status_on_success(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
        ) -> None:
        """A successful post result updates the video and account stats."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled",
            post_error="Old timeout",
            upload_error="Download failed earlier",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 100,
                "videoId": vid_id,
                "username": username,
                "result": "success",
                "durationMs": 15000,
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "posted"
            assert vid.post_result == "success"
            assert vid.post_error is None
            assert vid.upload_error is None
            assert vid.post_duration_ms == 15000
            assert vid.posted_at is not None

            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.total_posted == 1
            assert acct.last_posted_at is not None

        broadcaster.broadcast.assert_called_once()
        call_args = broadcaster.broadcast.call_args
        assert call_args[0][0] == "post:result"
        assert call_args[0][1]["result"] == "success"

    @pytest.mark.asyncio
    async def test_handles_failure(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A failed post increments retry_count and marks the video as failed."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 200,
                "videoId": vid_id,
                "username": username,
                "result": "failed",
                "error": "Upload timeout",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "failed"
            assert vid.post_result == "failed"
            assert vid.post_error == "Upload timeout"
            assert vid.retry_count == 1

            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.total_failed == 1

    @pytest.mark.asyncio
    async def test_retryable_failure_does_not_send_telegram_failure(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Transient phone failures stay in DB but do not page Telegram before retries are exhausted."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="scheduled",
            retry_count=0,
        )
        bot = MagicMock()
        bot.notify_post_success = AsyncMock()
        bot.notify_post_failure = AsyncMock()
        scheduler.telegram_bot = bot

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 220,
                "videoId": vid_id,
                "username": username,
                "result": "failed",
                "errorMessage": "Timeout in state SELECTING_VIDEO",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "failed"
            assert vid.retry_count == 1

        bot.notify_post_failure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_terminal_failure_still_sends_telegram_failure(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        config: VPSConfig,
    ) -> None:
        """The operator still gets a Telegram failure after the final retry is spent."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="scheduled",
            retry_count=config.farm_max_auto_retries - 1,
        )
        bot = MagicMock()
        bot.notify_post_success = AsyncMock()
        bot.notify_post_failure = AsyncMock()
        scheduler.telegram_bot = bot

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 221,
                "videoId": vid_id,
                "username": username,
                "result": "failed",
                "errorMessage": "Timeout in state SELECTING_VIDEO",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        bot.notify_post_failure.assert_awaited_once_with(
            username,
            f"video:{vid_id}",
            "Timeout in state SELECTING_VIDEO",
        )

    @pytest.mark.asyncio
    async def test_handles_failure_with_error_message_alias(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Phone logs using errorMessage still populate VPS failure details."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 201,
                "videoId": vid_id,
                "username": username,
                "result": "failed",
                "errorMessage": "Posting timed out on phone",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.status == "failed"
            assert vid.post_result == "failed"
            assert vid.post_error == "Posting timed out on phone"

            post_log = (await session.execute(
                select(PostLog).where(PostLog.phone_log_id == 201)
            )).scalar_one()
            assert post_log.error_message == "Posting timed out on phone"

        broadcaster.broadcast.assert_called_once()
        call_args = broadcaster.broadcast.call_args
        assert call_args[0][0] == "post:result"
        assert call_args[0][1]["error"] == "Posting timed out on phone"

    @pytest.mark.asyncio
    async def test_handles_timeout_result_as_failed(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Device timeout results should terminate the queue item as failed."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 202,
                "videoId": vid_id,
                "username": username,
                "result": "timeout",
                "errorMessage": "Timeout",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid is not None
            assert vid.status == "failed"
            assert vid.post_result == "timeout"
            assert vid.post_error == "Timeout"
            assert vid.retry_count == 1

            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.total_failed == 1

    @pytest.mark.asyncio
    async def test_profile_count_reconcile_notifies_success(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A profile post-count confirmation rescues a known false profile-grid failure."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="failed",
            post_result="failed",
            post_error="post not visible on profile after 60s",
            scheduled_time=datetime.utcnow(),
            filename="confirmed-by-count.mp4",
        )
        bot = MagicMock()
        bot.notify_post_success = AsyncMock()
        bot.notify_post_failure = AsyncMock()
        scheduler.telegram_bot = bot

        async with session_factory() as session:
            session.add(PostLog(
                video_id=vid_id,
                device_id=dev_id,
                account_username=username,
                result="failed",
                error_message="post not visible on profile after 60s",
                duration_ms=90_000,
                phone_log_id=321,
            ))
            await session.commit()
            account = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()

            marked = await scheduler.reconcile_profile_post_count(
                session,
                account=account,
                device_id=dev_id,
                old_posts_count=10,
                new_posts_count=11,
            )

        assert len(marked) == 1
        bot.notify_post_success.assert_awaited_once_with(
            username,
            "confirmed-by-count.mp4",
            0,
        )

    @pytest.mark.asyncio
    async def test_profile_count_reconcile_does_not_confirm_active_posting(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Profile stats must not send success before the phone reports the Share flow outcome."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="uploading",
            scheduled_time=datetime.utcnow(),
            filename="still-in-instagram-share-flow.mp4",
        )
        bot = MagicMock()
        bot.notify_post_success = AsyncMock()
        bot.notify_post_failure = AsyncMock()
        scheduler.telegram_bot = bot

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()

            marked = await scheduler.reconcile_profile_post_count(
                session,
                account=account,
                device_id=dev_id,
                old_posts_count=10,
                new_posts_count=11,
            )
            video = await session.get(Video, vid_id)

        assert marked == []
        assert video is not None
        assert video.status == "uploading"
        assert video.post_result is None
        assert video.posted_at is None
        bot.notify_post_success.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prefers_phone_video_id_over_colliding_vps_row_id(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Phone-side video IDs must not be mistaken for unrelated VPS primary keys."""
        async with session_factory() as session:
            dev_a = Device(
                device_id="DEVICE-A",
                name="Device A",
                ip_address="10.0.0.1",
                status="online",
                last_seen_at=datetime.utcnow(),
            )
            dev_b = Device(
                device_id="DEVICE-B",
                name="Device B",
                ip_address="10.0.0.2",
                status="online",
                last_seen_at=datetime.utcnow(),
            )
            session.add_all([dev_a, dev_b])
            await session.flush()

            acc_a = Account(username="user_a", is_active=True)
            acc_b = Account(username="user_b", is_active=True)
            session.add_all([acc_a, acc_b])
            await session.flush()

            session.add_all([
                AccountDevice(account_username="user_a", device_id=dev_a.id, is_primary=True),
                AccountDevice(account_username="user_b", device_id=dev_b.id, is_primary=True),
            ])

            colliding_video = Video(
                filename="collision.mp4",
                account_username="user_a",
                device_id=dev_a.id,
                status="pending",
            )
            session.add(colliding_video)
            await session.flush()

            target_video = Video(
                filename="target.mp4",
                account_username="user_b",
                device_id=dev_b.id,
                status="scheduled",
                phone_video_id=colliding_video.id,
            )
            session.add(target_video)
            await session.commit()

            colliding_id = colliding_video.id
            target_id = target_video.id
            target_device_id = dev_b.id

        ws_manager.get_online_device_ids.return_value = [target_device_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 901,
                "videoId": colliding_id,
                "username": "user_b",
                "result": "failed",
                "error": "Posting failed on device B",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            collision = await session.get(Video, colliding_id)
            target = await session.get(Video, target_id)
            assert collision is not None
            assert target is not None
            assert collision.status == "pending"
            assert collision.post_error is None
            assert target.status == "failed"
            assert target.post_error == "Posting failed on device B"
            assert target.retry_count == 1

    @pytest.mark.asyncio
    async def test_disambiguates_reused_phone_video_id_by_log_timestamp(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A phone DB reset can reuse video IDs; the log timestamp picks the matching VPS row."""
        dev_id, username = await _seed_device_and_account(session_factory)
        old_time = datetime.utcnow() - timedelta(days=3)
        new_time = datetime.utcnow() - timedelta(minutes=5)
        async with session_factory() as session:
            old_video = Video(
                filename="old.mp4",
                account_username=username,
                device_id=dev_id,
                phone_video_id=2,
                status="posted",
                post_result="success",
                posted_at=old_time,
                scheduled_time=old_time,
            )
            new_video = Video(
                filename="new.mp4",
                account_username=username,
                device_id=dev_id,
                phone_video_id=2,
                status="scheduled",
                scheduled_time=new_time,
            )
            session.add_all([old_video, new_video])
            await session.commit()
            old_id = old_video.id
            new_id = new_video.id

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 502,
                "videoId": 2,
                "username": username,
                "result": "failed",
                "errorMessage": "Timeout in state SELECTING_VIDEO",
                "timestamp": int(new_time.timestamp() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            old_video = await session.get(Video, old_id)
            new_video = await session.get(Video, new_id)
            assert old_video.status == "posted"
            assert new_video.status == "failed"
            assert new_video.post_error == "Timeout in state SELECTING_VIDEO"

    @pytest.mark.asyncio
    async def test_action_blocked_reports_without_automatic_quarantine(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Action-blocked events record failure; the operator controls quarantine."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 300,
                "videoId": vid_id,
                "username": username,
                "result": "action_blocked",
                "error": "Instagram action blocked",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.is_blocked is False
            assert acct.blocked_until is None
            assert acct.total_failed == 1


# ---------------------------------------------------------------------------
# Tests: health_check
# ---------------------------------------------------------------------------

class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_marks_disconnected_device_offline(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A device that lost its WebSocket is marked offline in DB."""
        dev_id, _ = await _seed_device_and_account(session_factory, device_status="online")

        ws_manager.is_online.return_value = False

        await scheduler.health_check()

        async with session_factory() as session:
            dev = await session.get(Device, dev_id)
            assert dev.status == "offline"

        broadcaster.broadcast.assert_called_once_with("device:disconnected", {
            "device_id": dev_id,
            "device_name": "Test Device",
        })

    @pytest.mark.asyncio
    async def test_marks_silent_device_offline(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A device with stale last_seen_at is marked offline even if WS connected."""
        dev_id, _ = await _seed_device_and_account(session_factory, device_status="online")

        # Set last_seen_at to 120s ago (> 90s threshold)
        async with session_factory() as session:
            dev = await session.get(Device, dev_id)
            dev.last_seen_at = datetime.utcnow() - timedelta(seconds=120)
            await session.commit()

        ws_manager.is_online.return_value = True

        await scheduler.health_check()

        async with session_factory() as session:
            dev = await session.get(Device, dev_id)
            assert dev.status == "offline"

        # Check broadcast was called with the silent device info
        assert broadcaster.broadcast.call_count >= 1
        call_args = broadcaster.broadcast.call_args
        assert call_args[0][0] == "device:disconnected"
        assert call_args[0][1]["device_id"] == dev_id

    @pytest.mark.asyncio
    async def test_online_device_stays_online(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        broadcaster: AsyncMock,
    ) -> None:
        """A device with recent heartbeat stays online."""
        dev_id, _ = await _seed_device_and_account(session_factory, device_status="online")

        # last_seen_at was set to utcnow() by _seed_device_and_account
        ws_manager.is_online.return_value = True

        await scheduler.health_check()

        async with session_factory() as session:
            dev = await session.get(Device, dev_id)
            assert dev.status == "online"

        broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: auto_schedule_videos
# ---------------------------------------------------------------------------

class TestAutoScheduleVideos:

    @pytest.mark.asyncio
    async def test_assigns_scheduled_time(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An unscheduled pending video gets a scheduled_time assigned."""
        _, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(session_factory, username)

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time is not None
            # Should be in the future
            assert vid.scheduled_time > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_respects_horizon_cap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        config: VPSConfig,
    ) -> None:
        """When horizon_cap (daily_cap * horizon_days) is full, nothing else lands."""
        _, username = await _seed_device_and_account(session_factory)

        # Fill the full 7-day horizon = 3 * 7 = 21 slots with existing
        # scheduled videos. Any extra pending video must NOT land anywhere
        # inside the horizon window.
        horizon_cap = (
            config.farm_max_posts_per_account_per_day
            * config.farm_schedule_horizon_days
        )
        now = datetime.utcnow()
        for i in range(horizon_cap):
            await _add_video(
                session_factory, username,
                filename=f"filled_{i}.mp4",
                scheduled_time=now + timedelta(hours=i + 1),
            )

        # Add one more unscheduled
        extra_id = await _add_video(
            session_factory, username, filename="extra.mp4",
        )

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            extra = await session.get(Video, extra_id)
            # Horizon is full — the extra should stay unscheduled.
            assert extra.scheduled_time is None

    @pytest.mark.asyncio
    async def test_respects_explicit_zero_max_posts_per_day(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """An explicit per-account zero limit disables auto-scheduling for that account."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"max_posts_per_day": 0},
        )
        vid_id = await _add_video(session_factory, username)

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time is None

    @pytest.mark.asyncio
    async def test_skips_paused_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Paused accounts are not auto-scheduled."""
        _, username = await _seed_device_and_account(
            session_factory, account_kwargs={"is_paused": True},
        )
        vid_id = await _add_video(session_factory, username)

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time is None

    @pytest.mark.asyncio
    async def test_skips_blocked_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Blocked accounts are not auto-scheduled."""
        _, username = await _seed_device_and_account(
            session_factory, account_kwargs={"is_blocked": True},
        )
        vid_id = await _add_video(session_factory, username)

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time is None

    @pytest.mark.asyncio
    async def test_uses_account_posting_times(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Account-level posting_times override the defaults."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"posting_times": "06:00, 22:00"},
        )
        vid_id = await _add_video(session_factory, username)

        await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time is not None
            # The time should be at 06:00 or 22:00 (tomorrow if both past)
            assert vid.scheduled_time.minute == 0
            assert vid.scheduled_time.hour in (6, 22)

    @pytest.mark.asyncio
    async def test_uses_farm_timezone_for_slot_assignment(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Posting times are interpreted in farm local time, then stored in UTC."""
        scheduler.config.farm_timezone = "Europe/Moscow"
        scheduler.config.farm_default_posting_times = ["21:00"]

        _, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(session_factory, username)

        fixed_now = datetime(2026, 1, 1, 18, 30, 0)  # 21:30 in Europe/Moscow
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            vid = await session.get(Video, vid_id)
            assert vid.scheduled_time == datetime(2026, 1, 2, 18, 0, 0)


# ---------------------------------------------------------------------------
# Tests: stride-based uniform distribution across the schedule horizon
# ---------------------------------------------------------------------------

class TestUniformDistribution:
    """Verify that _schedule_for_account spreads pending videos evenly
    across the full ``farm_schedule_horizon_days`` slot grid instead of
    packing them onto the next N consecutive slots."""

    @pytest.mark.asyncio
    async def test_spreads_videos_across_horizon(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """4 pending / 21 slots → stride=5, lands at slot[0,5,10,15]."""
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00", "14:00", "18:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        # Disable sibling spread to keep slot times exact / predictable.
        scheduler.config.farm_intra_slot_spread_seconds = 0

        _, username = await _seed_device_and_account(session_factory)
        vid_ids = []
        for i in range(4):
            vid_ids.append(
                await _add_video(
                    session_factory, username, filename=f"reel_{i}.mp4",
                ),
            )

        fixed_now = datetime(2026, 1, 1, 0, 0, 0)  # UTC midnight, day 0
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        # Expected 21 slots over 7 days × 3 times/day.
        # stride = 21 // 4 = 5
        # slot[0]  = 2026-01-01 10:00
        # slot[5]  = 2026-01-02 18:00
        # slot[10] = 2026-01-04 14:00
        # slot[15] = 2026-01-06 10:00
        expected = {
            datetime(2026, 1, 1, 10, 0, 0),
            datetime(2026, 1, 2, 18, 0, 0),
            datetime(2026, 1, 4, 14, 0, 0),
            datetime(2026, 1, 6, 10, 0, 0),
        }

        async with session_factory() as session:
            actual = set()
            for vid_id in vid_ids:
                vid = await session.get(Video, vid_id)
                assert vid.scheduled_time is not None
                actual.add(vid.scheduled_time)

        assert actual == expected

    @pytest.mark.asyncio
    async def test_excludes_already_booked_slots(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pre-booked slot times are removed from the candidate pool."""
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00", "14:00", "18:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        scheduler.config.farm_intra_slot_spread_seconds = 0

        _, username = await _seed_device_and_account(session_factory)

        # Pre-book 2 real slot times: slot[0] and slot[3].
        booked_slot_0 = datetime(2026, 1, 1, 10, 0, 0)
        booked_slot_3 = datetime(2026, 1, 2, 10, 0, 0)
        await _add_video(
            session_factory, username, filename="prebooked_0.mp4",
            scheduled_time=booked_slot_0,
        )
        await _add_video(
            session_factory, username, filename="prebooked_3.mp4",
            scheduled_time=booked_slot_3,
        )

        new_ids = []
        for i in range(3):
            new_ids.append(
                await _add_video(
                    session_factory, username, filename=f"new_{i}.mp4",
                ),
            )

        fixed_now = datetime(2026, 1, 1, 0, 0, 0)
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            for vid_id in new_ids:
                vid = await session.get(Video, vid_id)
                assert vid.scheduled_time is not None
                assert vid.scheduled_time != booked_slot_0
                assert vid.scheduled_time != booked_slot_3

    @pytest.mark.asyncio
    async def test_falls_back_to_packing_when_pending_exceeds_slots(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """When pending > slots, stride collapses to 1 and overflow stays unscheduled."""
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00", "14:00", "18:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        scheduler.config.farm_intra_slot_spread_seconds = 0
        # Make the daily cap large so horizon cap doesn't throttle below 21.
        scheduler.config.farm_max_posts_per_account_per_day = 10

        _, username = await _seed_device_and_account(session_factory)

        # 25 pending, only 21 slots.
        vid_ids = []
        for i in range(25):
            vid_ids.append(
                await _add_video(
                    session_factory, username, filename=f"reel_{i:02d}.mp4",
                ),
            )

        fixed_now = datetime(2026, 1, 1, 0, 0, 0)
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            scheduled = 0
            unscheduled = 0
            for vid_id in vid_ids:
                vid = await session.get(Video, vid_id)
                if vid.scheduled_time is not None:
                    scheduled += 1
                else:
                    unscheduled += 1

        assert scheduled == 21
        assert unscheduled == 4

    @pytest.mark.asyncio
    async def test_horizon_respects_daily_cap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        daily_cap=3 × horizon_days=7 → horizon_cap=21. With 5 videos
        already booked and 10 new pending, all 10 should fit
        (21 - 5 = 16 remaining > 10).
        """
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00", "14:00", "18:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        scheduler.config.farm_max_posts_per_account_per_day = 3
        scheduler.config.farm_intra_slot_spread_seconds = 0

        _, username = await _seed_device_and_account(session_factory)

        # 5 videos already occupying 5 non-slot timestamps inside the
        # horizon window — they count toward ``already_count`` but
        # DON'T collide with any slot time, so all 21 slots remain
        # available.
        fixed_now = datetime(2026, 1, 1, 0, 0, 0)
        for i in range(5):
            await _add_video(
                session_factory, username, filename=f"booked_{i}.mp4",
                scheduled_time=fixed_now + timedelta(hours=i + 1, minutes=30),
            )

        new_ids = []
        for i in range(10):
            new_ids.append(
                await _add_video(
                    session_factory, username, filename=f"new_{i:02d}.mp4",
                ),
            )

        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            scheduled = 0
            for vid_id in new_ids:
                vid = await session.get(Video, vid_id)
                if vid.scheduled_time is not None:
                    scheduled += 1

        # All 10 new videos should fit within the remaining horizon cap.
        assert scheduled == 10

    @pytest.mark.asyncio
    async def test_sibling_spread_filters_inactive(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        Inactive siblings must NOT consume a position slot: with 3 siblings
        A/B/C where B is inactive, C's spread position should be 1 (not 2).
        """
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        scheduler.config.farm_intra_slot_spread_seconds = 60  # 1 min per position
        scheduler.config.farm_schedule_jitter_std_seconds = 0

        async with session_factory() as session:
            dev = Device(
                device_id="TEST-DEV-SIBLINGS",
                name="sibling-device",
                ip_address="10.0.0.1",
                status="online",
                last_seen_at=datetime.utcnow(),
            )
            session.add(dev)
            await session.flush()
            dev_id = dev.id

            # 3 siblings, ordered A / B / C by AccountDevice.id (the tie
            # broken by ACD.id.asc() in _schedule_for_account).
            for username, active in [
                ("sib_a", True),
                ("sib_b", False),  # inactive — must NOT hold position 1
                ("sib_c", True),
            ]:
                acct = Account(username=username, is_active=active)
                session.add(acct)
                session.add(
                    AccountDevice(
                        account_username=username,
                        device_id=dev_id,
                        is_primary=False,
                    ),
                )
            await session.commit()

        await _add_video(session_factory, "sib_a", filename="a.mp4")
        await _add_video(session_factory, "sib_c", filename="c.mp4")

        fixed_now = datetime(2026, 1, 1, 0, 0, 0)  # UTC
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            row_a = (await session.execute(
                select(Video).where(Video.account_username == "sib_a"),
            )).scalar_one()
            row_c = (await session.execute(
                select(Video).where(Video.account_username == "sib_c"),
            )).scalar_one()

        # sib_a position=0 → offset 0s   → 10:00:00
        # sib_c position=1 → offset 60s  → 10:01:00 (would be 10:02:00
        # if inactive sib_b consumed position 1)
        assert row_a.scheduled_time == datetime(2026, 1, 1, 10, 0, 0)
        assert row_c.scheduled_time == datetime(2026, 1, 1, 10, 1, 0)

    @pytest.mark.asyncio
    async def test_same_account_rerun_respects_prior_spread_offset(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """
        Re-running _schedule_for_account for an account whose sibling
        position gives it a non-zero spread_offset must NOT collide with
        its OWN prior bookings, even though Video.scheduled_time stores
        the offset-adjusted time while all_slots holds raw base slots.

        Regression coverage for the T9 coordinate-space bug: the booked
        filter used to compare raw scheduled_time (e.g. 10:04) against
        base slot (e.g. 10:00), missed the match, picked the same base
        slot again, re-added the offset, and collided.
        """
        scheduler.config.farm_timezone = "UTC"
        scheduler.config.farm_default_posting_times = ["10:00"]
        scheduler.config.farm_schedule_horizon_days = 7
        scheduler.config.farm_intra_slot_spread_seconds = 240  # 4 min per position
        scheduler.config.farm_schedule_jitter_std_seconds = 0
        # Horizon cap = 3 × 7 = 21; plenty of headroom for 2 pre-booked
        # + 3 new = 5 total < 21.
        scheduler.config.farm_max_posts_per_account_per_day = 3

        async with session_factory() as session:
            dev = Device(
                device_id="TEST-DEV-RERUN",
                name="rerun-device",
                ip_address="10.0.0.2",
                status="online",
                last_seen_at=datetime.utcnow(),
            )
            session.add(dev)
            await session.flush()
            dev_id = dev.id

            # 2 siblings, both active. AccountDevice.id ordering means
            # sib_a is position 0 (offset 0s) and sib_b is position 1
            # (offset 240s).
            for username in ["sib_a", "sib_b"]:
                acct = Account(username=username, is_active=True)
                session.add(acct)
                session.add(
                    AccountDevice(
                        account_username=username,
                        device_id=dev_id,
                        is_primary=False,
                    ),
                )
            await session.commit()

        # Pre-seed 2 sib_b videos as if a previous scheduler run had
        # placed them at base_slot_0 + 240s and base_slot_1 + 240s
        # (i.e. day-0 10:04 and day-1 10:04 — exactly the shape
        # _schedule_for_account would produce for a position-1 sibling).
        booked_0 = datetime(2026, 1, 1, 10, 4, 0)  # day 0, 10:00 + 240s
        booked_1 = datetime(2026, 1, 2, 10, 4, 0)  # day 1, 10:00 + 240s
        await _add_video(
            session_factory, "sib_b", filename="prebooked_0.mp4",
            scheduled_time=booked_0,
        )
        await _add_video(
            session_factory, "sib_b", filename="prebooked_1.mp4",
            scheduled_time=booked_1,
        )

        new_ids = []
        for i in range(3):
            new_ids.append(
                await _add_video(
                    session_factory, "sib_b", filename=f"new_{i}.mp4",
                ),
            )

        fixed_now = datetime(2026, 1, 1, 0, 0, 0)  # UTC midnight, day 0
        with patch("server.scheduler._utcnow", return_value=fixed_now):
            await scheduler.auto_schedule_videos()

        async with session_factory() as session:
            new_times: list[datetime] = []
            for vid_id in new_ids:
                vid = await session.get(Video, vid_id)
                assert vid.scheduled_time is not None
                new_times.append(vid.scheduled_time)

        # Correctness: none of the new 3 may collide with the 2 pre-seeded.
        booked_pre = {booked_0, booked_1}
        for t in new_times:
            assert t not in booked_pre, (
                f"New video scheduled at {t} collides with prior booking"
            )

        # Stronger assertion: the filter must exclude BOTH base slots
        # whose spread-adjusted projection was pre-booked. After filter,
        # base slots remaining = day_2..day_6 = 5. With 3 pending and
        # stride = 5 // 3 = 1, the picks are day_2, day_3, day_4 at the
        # 10:00 base, each + 240s offset → 10:04:00.
        expected = {
            datetime(2026, 1, 3, 10, 4, 0),  # day 2
            datetime(2026, 1, 4, 10, 4, 0),  # day 3
            datetime(2026, 1, 5, 10, 4, 0),  # day 4
        }
        assert set(new_times) == expected


# ---------------------------------------------------------------------------
# Tests: auto_generate_videos
# ---------------------------------------------------------------------------

class TestAutoGenerateVideos:

    @pytest.mark.asyncio
    async def test_generates_video_when_queue_empty(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Auto-generation inserts a pending video and generation run."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie", "use_scenarios": True},
        )

        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        async def _fake_generate_vid_bait(**kwargs: Any) -> None:
            output_path = kwargs["output_path"]
            output_path.write_bytes(b"generated-video")

        with patch(
            "server.video_gen.generate_vid_bait",
            new=AsyncMock(side_effect=_fake_generate_vid_bait),
        ):
            await scheduler._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
            runs = (await session.execute(select(GenerationRun))).scalars().all()

        assert len(videos) == 4
        assert len(runs) == 4

        video = videos[0]
        assert video.status == "pending"
        assert video.caption == "Scenario caption"
        assert video.original_path is not None
        assert Path(video.original_path).exists()

    @pytest.mark.asyncio
    async def test_vid_bait_skip_text_overlay_uses_blank_overlay_and_caption(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Per-clip skip_text_overlay bypasses scenario text but still renders."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie", "use_scenarios": True},
        )

        model_dir = tmp_path / "models" / "baddie"
        clip_dir = model_dir / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")
        (model_dir / "photo_catalog.json").write_text(
            json.dumps([
                {
                    "folder": "vid_bait",
                    "filename": "seed.mp4",
                    "skip_text_overlay": True,
                },
            ]),
            encoding="utf-8",
        )

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        captured_text_lines: list[list[str]] = []

        async def _fake_generate_vid_bait(**kwargs: Any) -> None:
            captured_text_lines.append(list(kwargs["text_lines"]))
            kwargs["output_path"].write_bytes(b"generated-video")

        with patch(
            "server.video_gen.generate_vid_bait",
            new=AsyncMock(side_effect=_fake_generate_vid_bait),
        ):
            await scheduler._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        assert captured_text_lines
        assert all(lines == [] for lines in captured_text_lines)
        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
        assert videos
        assert {video.caption for video in videos} == {""}

    @pytest.mark.asyncio
    async def test_reports_blocked_output_directory(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        telegram_bot: AsyncMock,
    ) -> None:
        """Generation failure due to unwritable output dir is surfaced clearly."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie", "use_scenarios": True},
        )

        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        fake_issue = "Permission denied; mode=0o755 uid=0 gid=0"
        with patch.object(
            scheduler_with_bot,
            "_check_generation_output_dir",
            return_value=fake_issue,
        ), patch(
            "server.video_gen.generate_vid_bait",
            new=AsyncMock(),
        ) as fake_generate:
            await scheduler_with_bot._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        fake_generate.assert_not_called()
        telegram_bot.send_notification.assert_called_once()
        alert_text = telegram_bot.send_notification.call_args[0][0]
        assert username in alert_text
        assert fake_issue in alert_text

        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
            runs = (await session.execute(select(GenerationRun))).scalars().all()

        assert videos == []
        assert runs == []


class TestRawVideoPath:
    """T8: per-account Account.use_scenarios tri-state toggle.

    When ``use_scenarios`` resolves to False the auto_generate_videos
    path is a no-op for that account — the scheduler must NOT create
    any Video rows, must NOT call generate_vid_bait, and must NOT raise.
    When it resolves to True (explicit or via the config default) the
    normal generation path runs.
    """

    @pytest.mark.asyncio
    async def test_generate_skipped_when_use_scenarios_false(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """use_scenarios=False short-circuits before any file/DB work."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={
                "recreator_model": "baddie",
                "use_scenarios": False,
            },
        )

        # Seed a clip so the legacy path would normally generate a video.
        # If the guard misfires, this file makes the miss loud.
        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        fake_generate = AsyncMock()
        with patch("server.video_gen.generate_vid_bait", new=fake_generate):
            # Must not raise even when the clip dir/scenarios exist.
            await scheduler._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        fake_generate.assert_not_called()
        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
            runs = (await session.execute(select(GenerationRun))).scalars().all()
        assert videos == []
        assert runs == []

    @pytest.mark.asyncio
    async def test_generate_runs_when_use_scenarios_null(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """use_scenarios=None falls back to the global default (True) → runs."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={
                "recreator_model": "baddie",
                "use_scenarios": None,
            },
        )

        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        # farm_use_scenarios_default defaults to True in VPSConfig.
        scheduler.config.farm_use_scenarios_default = True

        async def _fake_generate_vid_bait(**kwargs: Any) -> None:
            output_path = kwargs["output_path"]
            output_path.write_bytes(b"generated-video")

        with patch(
            "server.video_gen.generate_vid_bait",
            new=AsyncMock(side_effect=_fake_generate_vid_bait),
        ):
            await scheduler._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
        assert len(videos) == 4
        assert videos[0].status == "pending"

    @pytest.mark.asyncio
    async def test_generate_runs_when_use_scenarios_true(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """use_scenarios=True runs the normal pipeline — explicit opt-in."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={
                "recreator_model": "baddie",
                "use_scenarios": True,
            },
        )

        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        async def _fake_generate_vid_bait(**kwargs: Any) -> None:
            output_path = kwargs["output_path"]
            output_path.write_bytes(b"generated-video")

        with patch(
            "server.video_gen.generate_vid_bait",
            new=AsyncMock(side_effect=_fake_generate_vid_bait),
        ):
            await scheduler._generate_for_account(
                account=account,
                data_dir=tmp_path,
                scenarios=[{"text": "Scenario caption"}],
                font_path=None,
            )

        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
        assert len(videos) == 4

    @pytest.mark.asyncio
    async def test_generate_skipped_when_global_default_false(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """use_scenarios=None with farm default=False short-circuits."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={
                "recreator_model": "baddie",
                "use_scenarios": None,  # fall back to config
            },
        )

        clip_dir = tmp_path / "models" / "baddie" / "vid_bait"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "seed.mp4").write_bytes(b"fake-clip")

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()

        # Flip the global default under the feet of the scheduler.
        scheduler.config.farm_use_scenarios_default = False

        fake_generate = AsyncMock()
        try:
            with patch("server.video_gen.generate_vid_bait", new=fake_generate):
                await scheduler._generate_for_account(
                    account=account,
                    data_dir=tmp_path,
                    scenarios=[{"text": "Scenario caption"}],
                    font_path=None,
                )
        finally:
            scheduler.config.farm_use_scenarios_default = True

        fake_generate.assert_not_called()
        async with session_factory() as session:
            videos = (
                await session.execute(
                    select(Video).where(Video.account_username == username),
                )
            ).scalars().all()
        assert videos == []


# ---------------------------------------------------------------------------
# Tests: scheduler lifecycle
# ---------------------------------------------------------------------------

class TestSchedulerLifecycle:

    @pytest.mark.asyncio
    async def test_starts_and_stops(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """The scheduler starts and stops without error."""
        await scheduler.start()
        assert scheduler.running is True

        # Verify all 9 jobs are registered
        job_ids = {j.id for j in scheduler.scheduler.get_jobs()}
        assert "process_pending_videos" in job_ids
        assert "collect_post_results" in job_ids
        assert "health_check" in job_ids
        assert "auto_schedule_videos" in job_ids
        assert "recover_stale" in job_ids
        assert "auto_engagement" in job_ids
        assert "collect_engagement" in job_ids
        assert "auto_insights" in job_ids
        assert "collect_insights" in job_ids

        await scheduler.stop()
        assert scheduler.running is False

    @pytest.mark.asyncio
    async def test_duplicate_log_ignored(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A post log with the same phone_log_id is not inserted twice."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id, status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        log_entry = {
            "id": 500,
            "videoId": vid_id,
            "username": username,
            "result": "success",
            "durationMs": 10000,
            "timestamp": int(time.time() * 1000),
        }
        bridge.get_post_logs.return_value = {"logs": [log_entry]}

        # Collect twice
        await scheduler.collect_post_results()
        # Reset poll so it re-fetches
        scheduler._last_poll_ms.clear()
        await scheduler.collect_post_results()

        async with session_factory() as session:
            result = await session.execute(
                select(PostLog).where(PostLog.phone_log_id == 500)
            )
            logs = result.scalars().all()
            assert len(logs) == 1

    @pytest.mark.asyncio
    async def test_duplicate_log_checks_all_phone_log_id_collisions(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A replay is ignored when any same phone_log_id row matches the phone timestamp."""
        dev_id, username = await _seed_device_and_account(session_factory)
        log_time = datetime.utcnow() - timedelta(minutes=10)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="scheduled",
            phone_video_id=7,
        )
        async with session_factory() as session:
            session.add_all([
                PostLog(
                    video_id=None,
                    device_id=dev_id,
                    account_username="old_user",
                    result="failed",
                    phone_log_id=501,
                    timestamp=log_time - timedelta(days=3),
                ),
                PostLog(
                    video_id=vid_id,
                    device_id=dev_id,
                    account_username=username,
                    result="success",
                    phone_log_id=501,
                    timestamp=log_time,
                ),
            ])
            await session.commit()

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 501,
                "videoId": 7,
                "username": username,
                "result": "success",
                "durationMs": 10000,
                "timestamp": int(log_time.timestamp() * 1000),
            }],
        }

        await scheduler.collect_post_results()

        async with session_factory() as session:
            result = await session.execute(
                select(PostLog).where(PostLog.phone_log_id == 501)
            )
            logs = result.scalars().all()
            assert len(logs) == 2

    @pytest.mark.asyncio
    async def test_first_poll_after_restart_uses_db_post_log_offset(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A scheduler restart should not ask the phone for its whole historical log buffer."""
        dev_id, username = await _seed_device_and_account(session_factory)
        last_seen = datetime(2026, 5, 11, 8, 0, 0)
        async with session_factory() as session:
            session.add(PostLog(
                video_id=None,
                device_id=dev_id,
                account_username=username,
                result="success",
                phone_log_id=10,
                timestamp=last_seen,
            ))
            await session.commit()

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {"logs": []}

        await scheduler.collect_post_results()

        expected_since_ms = int(
            last_seen.replace(tzinfo=timezone.utc).timestamp() * 1000
        )
        bridge.get_post_logs.assert_awaited_once_with(
            dev_id,
            since_ms=expected_since_ms,
        )


# ---------------------------------------------------------------------------
# Tests: Telegram bot notifications
# ---------------------------------------------------------------------------

@pytest.fixture
def telegram_bot() -> AsyncMock:
    """AsyncMock mimicking VPSTelegramBot notification methods."""
    bot = AsyncMock()
    bot.notify_post_success = AsyncMock()
    bot.notify_post_failure = AsyncMock()
    bot.notify_device_offline = AsyncMock()
    bot.notify_device_online = AsyncMock()
    return bot


@pytest_asyncio.fixture
async def scheduler_with_bot(
    session_factory: async_sessionmaker[AsyncSession],
    ws_manager: MagicMock,
    bridge: AsyncMock,
    config: VPSConfig,
    broadcaster: AsyncMock,
    telegram_bot: AsyncMock,
) -> FarmScheduler:
    return FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=broadcaster,
        telegram_bot=telegram_bot,
    )


class TestTelegramNotifications:

    @pytest.mark.asyncio
    async def test_collect_results_notifies_on_success(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """On a successful post, bot.notify_post_success is called."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id, status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 1001,
                "videoId": vid_id,
                "username": username,
                "result": "success",
                "filename": "reel.mp4",
                "durationMs": 12000,
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler_with_bot.collect_post_results()

        telegram_bot.notify_post_success.assert_called_once_with(
            username, "reel.mp4", 12000,
        )
        telegram_bot.notify_post_failure.assert_not_called()

    @pytest.mark.asyncio
    async def test_collect_results_notifies_on_terminal_failure(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        telegram_bot: AsyncMock,
        config: VPSConfig,
    ) -> None:
        """On a final failed post, bot.notify_post_failure is called with the error."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="scheduled",
            retry_count=config.farm_max_auto_retries - 1,
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 1002,
                "videoId": vid_id,
                "username": username,
                "result": "failed",
                "filename": "reel.mp4",
                "error": "Upload timeout",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler_with_bot.collect_post_results()

        telegram_bot.notify_post_failure.assert_called_once_with(
            username, "reel.mp4", "Upload timeout",
        )
        telegram_bot.notify_post_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_collect_results_notifies_on_action_blocked(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """On action_blocked, bot.notify_post_failure is called with the blocked message."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id, status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 1003,
                "videoId": vid_id,
                "username": username,
                "result": "action_blocked",
                "filename": "reel.mp4",
                "error": "Instagram action blocked",
                "timestamp": int(time.time() * 1000),
            }],
        }

        await scheduler_with_bot.collect_post_results()

        telegram_bot.notify_post_failure.assert_called_once_with(
            username, "reel.mp4", "Action blocked by Instagram",
        )

    @pytest.mark.asyncio
    async def test_health_check_notifies_device_offline(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """When a device goes offline, bot.notify_device_offline is called."""
        dev_id, _ = await _seed_device_and_account(
            session_factory, device_status="online",
        )

        ws_manager.is_online.return_value = False

        await scheduler_with_bot.health_check()

        telegram_bot.notify_device_offline.assert_called_once_with("Test Device")

    @pytest.mark.asyncio
    async def test_health_check_notifies_device_online(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """When a device comes back online, bot.notify_device_online is called."""
        dev_id, _ = await _seed_device_and_account(
            session_factory, device_status="offline",
        )

        ws_manager.is_online.return_value = True

        await scheduler_with_bot.health_check()

        telegram_bot.notify_device_online.assert_called_once_with("Test Device")
        telegram_bot.notify_device_offline.assert_not_called()

    @pytest.mark.asyncio
    async def test_notifications_skip_if_no_bot(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """When telegram_bot is None, notification calls are skipped without error."""
        assert scheduler.telegram_bot is None

        # Test post result without bot
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id, status="scheduled",
        )

        ws_manager.get_online_device_ids.return_value = [dev_id]
        bridge.get_post_logs.return_value = {
            "logs": [{
                "id": 1004,
                "videoId": vid_id,
                "username": username,
                "result": "success",
                "durationMs": 5000,
                "timestamp": int(time.time() * 1000),
            }],
        }

        # Should not raise
        await scheduler.collect_post_results()

        # Test health check without bot — device goes offline
        ws_manager.is_online.return_value = False

        # Should not raise
        await scheduler.health_check()


# ---------------------------------------------------------------------------
# Tests: health_check disk space monitoring
# ---------------------------------------------------------------------------

class TestHealthCheckDiskSpace:

    @pytest.mark.asyncio
    async def test_health_check_alerts_low_disk(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """When disk space is below 500MB, a Telegram alert is sent."""
        # Seed a device so the health check DB queries don't fail
        await _seed_device_and_account(session_factory, device_status="offline")
        ws_manager.is_online.return_value = False

        # Mock disk_usage to return low free space (200 MB)
        low_usage = MagicMock()
        low_usage.free = 200 * 1024 * 1024  # 200 MB
        low_usage.total = 10 * 1024**3
        low_usage.used = low_usage.total - low_usage.free

        with patch("server.scheduler.shutil.disk_usage", return_value=low_usage):
            await scheduler_with_bot.health_check()

        # Verify Telegram notification was sent with low disk space warning
        telegram_bot.send_notification.assert_called_once()
        call_text = telegram_bot.send_notification.call_args[0][0]
        assert "Low disk space" in call_text
        assert "0.2 GB" in call_text

    @pytest.mark.asyncio
    async def test_health_check_no_alert_when_disk_ok(
        self,
        scheduler_with_bot: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        telegram_bot: AsyncMock,
    ) -> None:
        """When disk space is above 500MB, no disk alert is sent."""
        await _seed_device_and_account(session_factory, device_status="offline")
        ws_manager.is_online.return_value = False

        # Mock disk_usage to return plenty of free space (5 GB)
        ok_usage = MagicMock()
        ok_usage.free = 5 * 1024**3  # 5 GB
        ok_usage.total = 10 * 1024**3
        ok_usage.used = ok_usage.total - ok_usage.free

        with patch("server.scheduler.shutil.disk_usage", return_value=ok_usage):
            await scheduler_with_bot.health_check()

        # send_notification should NOT have been called for disk space
        telegram_bot.send_notification.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers for engagement/insights tests
# ---------------------------------------------------------------------------

async def _seed_engagement_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str = "eng_user",
    engagement_enabled: bool = True,
    engagement_sessions_day: int | None = None,
    engagement_daily_budget: int | None = None,
    engagement_like_prob: float | None = None,
    engagement_comment_prob: float | None = None,
    engagement_reply_prob: float | None = None,
    engagement_share_prob: float | None = None,
    last_engagement_at: datetime | None = None,
    is_paused: bool = False,
    is_blocked: bool = False,
    targets: list[dict[str, Any]] | None = None,
) -> tuple[int, str]:
    """Seed device + engagement-enabled account + targets. Return (device.id, username)."""
    async with session_factory() as session:
        dev = Device(
            device_id=f"ENG-{username}",
            name=f"Eng Device ({username})",
            ip_address="192.168.1.50",
            status="online",
            last_seen_at=datetime.utcnow(),
        )
        session.add(dev)
        await session.flush()

        acct = Account(
            username=username,
            is_active=True,
            is_paused=is_paused,
            is_blocked=is_blocked,
            engagement_enabled=engagement_enabled,
            engagement_sessions_day=engagement_sessions_day,
            engagement_daily_budget=engagement_daily_budget,
            engagement_like_prob=engagement_like_prob,
            engagement_comment_prob=engagement_comment_prob,
            engagement_reply_prob=engagement_reply_prob,
            engagement_share_prob=engagement_share_prob,
            last_engagement_at=last_engagement_at,
        )
        session.add(acct)
        await session.flush()

        link = AccountDevice(
            account_username=username,
            device_id=dev.id,
            is_primary=True,
        )
        session.add(link)

        for t_data in (targets or []):
            t = EngagementTarget(
                target_username=t_data.get("target_username", "competitor"),
                account_username=username,
                max_reels=t_data.get("max_reels", 5),
                should_follow=t_data.get("should_follow", True),
                is_active=t_data.get("is_active", True),
            )
            session.add(t)

        await session.commit()
        return dev.id, username


async def _seed_insights_account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    username: str = "ins_user",
    insights_enabled: bool = True,
    insights_interval_hours: float | None = None,
    insights_max_reels: int | None = None,
    last_insights_at: datetime | None = None,
    is_paused: bool = False,
) -> tuple[int, str]:
    """Seed device + insights-enabled account. Return (device.id, username)."""
    async with session_factory() as session:
        dev = Device(
            device_id=f"INS-{username}",
            name=f"Ins Device ({username})",
            ip_address="192.168.1.60",
            status="online",
            last_seen_at=datetime.utcnow(),
        )
        session.add(dev)
        await session.flush()

        acct = Account(
            username=username,
            is_active=True,
            is_paused=is_paused,
            insights_enabled=insights_enabled,
            insights_interval_hours=insights_interval_hours,
            insights_max_reels=insights_max_reels,
            last_insights_at=last_insights_at,
        )
        session.add(acct)
        await session.flush()

        link = AccountDevice(
            account_username=username,
            device_id=dev.id,
            is_primary=True,
        )
        session.add(link)
        await session.commit()
        return dev.id, username


# ---------------------------------------------------------------------------
# Tests: auto_start_engagement
# ---------------------------------------------------------------------------

class TestAutoStartEngagement:

    @pytest.mark.asyncio
    async def test_starts_session(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """An engagement-enabled account with no running session gets started."""
        dev_id, username = await _seed_engagement_account(
            session_factory,
            targets=[
                {"target_username": "ch1", "max_reels": 5, "should_follow": True},
            ],
        )
        ws_manager.is_online.return_value = True

        await scheduler.auto_start_engagement()

        bridge.start_engagement.assert_called_once()
        call_args = bridge.start_engagement.call_args
        assert call_args[0][0] == dev_id
        payload = call_args[0][1]
        assert payload["accountUsername"] == username
        assert len(payload["channels"]) == 1
        assert payload["channels"][0]["targetUsername"] == "ch1"
        assert "actionProbabilities" in payload
        assert "likeProbability" in payload["actionProbabilities"]
        assert "timings" in payload
        assert payload["dailyBudgetMinutes"] == 30  # default

        # Verify session record created
        async with session_factory() as session:
            stmt = select(EngagementSession).where(
                EngagementSession.account_username == username,
            )
            result = await session.execute(stmt)
            eng_sessions = result.scalars().all()
            assert len(eng_sessions) == 1
            assert eng_sessions[0].status == "running"
            assert eng_sessions[0].device_id == dev_id

            # Verify last_engagement_at updated
            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.last_engagement_at is not None

    @pytest.mark.asyncio
    async def test_preserves_zero_budget_and_probabilities(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Explicit zero engagement settings should reach the device payload unchanged."""
        dev_id, username = await _seed_engagement_account(
            session_factory,
            engagement_daily_budget=0,
            engagement_like_prob=0.0,
            engagement_comment_prob=0.0,
            engagement_reply_prob=0.0,
            engagement_share_prob=0.0,
            targets=[
                {"target_username": "ch1", "max_reels": 5, "should_follow": True},
            ],
        )
        ws_manager.is_online.return_value = True

        await scheduler.auto_start_engagement()

        bridge.start_engagement.assert_called_once()
        call_args = bridge.start_engagement.call_args
        assert call_args[0][0] == dev_id
        payload = call_args[0][1]
        assert payload["accountUsername"] == username
        assert payload["dailyBudgetMinutes"] == 0
        assert payload["actionProbabilities"] == {
            "likeProbability": 0.0,
            "commentProbability": 0.0,
            "replyProbability": 0.0,
            "shareProbability": 0.0,
        }

    @pytest.mark.asyncio
    async def test_skips_paused_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A paused account is not started even if engagement_enabled."""
        await _seed_engagement_account(
            session_factory,
            is_paused=True,
        )
        ws_manager.is_online.return_value = True

        await scheduler.auto_start_engagement()

        bridge.start_engagement.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_daily_limit(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """When today's session count reaches the limit, no new session is started."""
        dev_id, username = await _seed_engagement_account(
            session_factory,
            engagement_sessions_day=1,
        )
        ws_manager.is_online.return_value = True

        # Create a completed session for today (use midday to avoid midnight edge case)
        today_midday = datetime.utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
        async with session_factory() as session:
            existing = EngagementSession(
                device_id=dev_id,
                account_username=username,
                status="completed",
                started_at=today_midday - timedelta(hours=2),
                finished_at=today_midday - timedelta(hours=1),
            )
            session.add(existing)
            await session.commit()

        await scheduler.auto_start_engagement()

        bridge.start_engagement.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_running_session(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """If a session is already running for the account, skip it."""
        dev_id, username = await _seed_engagement_account(
            session_factory,
            engagement_sessions_day=5,  # high limit to ensure skip is from running check
        )
        ws_manager.is_online.return_value = True

        # Create a running session
        async with session_factory() as session:
            running = EngagementSession(
                device_id=dev_id,
                account_username=username,
                status="running",
                started_at=datetime.utcnow() - timedelta(minutes=5),
            )
            session.add(running)
            await session.commit()

        await scheduler.auto_start_engagement()

        bridge.start_engagement.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: collect_engagement_results
# ---------------------------------------------------------------------------

class TestCollectEngagementResults:

    @pytest.mark.asyncio
    async def test_completes_session(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """When device reports engagement not active, session is marked completed."""
        dev_id, username = await _seed_engagement_account(session_factory)
        ws_manager.is_online.return_value = True

        # Create a running session
        async with session_factory() as session:
            eng = EngagementSession(
                device_id=dev_id,
                account_username=username,
                status="running",
                started_at=datetime.utcnow() - timedelta(minutes=30),
            )
            session.add(eng)
            await session.commit()
            session_id = eng.id

        # Bridge returns some actions and then reports not active
        bridge.get_engagement_actions.return_value = {
            "actions": [
                {
                    "id": 1,
                    "actionType": "like",
                    "targetUsername": "ch1",
                    "success": True,
                    "timestamp": int(time.time() * 1000),
                },
                {
                    "id": 2,
                    "actionType": "comment",
                    "targetUsername": "ch1",
                    "commentText": "Nice!",
                    "success": True,
                    "timestamp": int(time.time() * 1000),
                },
            ],
        }
        bridge.get_engagement_status.return_value = {"active": False}

        await scheduler.collect_engagement_results()

        # Session should be completed
        async with session_factory() as session:
            eng = await session.get(EngagementSession, session_id)
            assert eng.status == "completed"
            assert eng.finished_at is not None
            assert eng.total_likes == 1
            assert eng.total_comments == 1
            assert eng.duration_ms is not None
            assert eng.duration_ms > 0

            # Actions should be saved
            actions_result = await session.execute(
                select(EngagementAction).where(
                    EngagementAction.session_id == session_id,
                )
            )
            actions = actions_result.scalars().all()
            assert len(actions) == 2

        broadcaster.broadcast.assert_called_once_with("engagement:update", {
            "ts": broadcaster.broadcast.call_args[0][1]["ts"],
        })

    @pytest.mark.asyncio
    async def test_deduplicates_actions(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Actions with the same phone_action_id are not inserted twice."""
        dev_id, username = await _seed_engagement_account(session_factory)
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            eng = EngagementSession(
                device_id=dev_id,
                account_username=username,
                status="running",
                started_at=datetime.utcnow() - timedelta(minutes=10),
            )
            session.add(eng)
            await session.commit()
            session_id = eng.id

        action_entry = {
            "id": 42,
            "actionType": "like",
            "targetUsername": "ch1",
            "success": True,
            "timestamp": int(time.time() * 1000),
        }
        bridge.get_engagement_actions.return_value = {"actions": [action_entry]}
        bridge.get_engagement_status.return_value = {"active": True}

        # Collect twice
        await scheduler.collect_engagement_results()
        scheduler._last_engagement_poll_ms.clear()
        await scheduler.collect_engagement_results()

        async with session_factory() as session:
            result = await session.execute(
                select(EngagementAction).where(
                    EngagementAction.phone_action_id == 42,
                    EngagementAction.device_id == dev_id,
                )
            )
            assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_disabled_account_keeps_session_running_when_abort_fails(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A failed device abort must not mark the running session aborted locally."""
        dev_id, username = await _seed_engagement_account(
            session_factory,
            engagement_enabled=False,
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            eng = EngagementSession(
                device_id=dev_id,
                account_username=username,
                status="running",
                started_at=datetime.utcnow() - timedelta(minutes=5),
            )
            session.add(eng)
            await session.commit()
            session_id = eng.id

        bridge.abort_engagement.side_effect = RuntimeError("timeout")
        bridge.get_engagement_actions.return_value = {"actions": []}
        bridge.get_engagement_status.return_value = {"active": True}

        await scheduler.collect_engagement_results()

        async with session_factory() as session:
            eng = await session.get(EngagementSession, session_id)
            assert eng.status == "running"
            assert eng.finished_at is None


# ---------------------------------------------------------------------------
# Tests: auto_start_insights
# ---------------------------------------------------------------------------

class TestAutoStartInsights:

    @pytest.mark.asyncio
    async def test_skips_account_without_due_insights_plan(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """An account without due posted-video insight jobs must not run."""
        dev_id, username = await _seed_insights_account(
            session_factory,
            insights_max_reels=15,
        )
        ws_manager.is_online.return_value = True
        bridge.get_status.return_value = {"activeMode": "NONE"}

        await scheduler.auto_start_insights()

        bridge.start_insights.assert_not_called()

        # No collection has been started
        async with session_factory() as session:
            acct = (await session.execute(
                select(Account).where(Account.username == username)
            )).scalar_one()
            assert acct.last_insights_at is None

    @pytest.mark.asyncio
    async def test_respects_interval(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """An account with recent insights collection is skipped."""
        await _seed_insights_account(
            session_factory,
            insights_interval_hours=6.0,
            last_insights_at=datetime.utcnow() - timedelta(hours=2),  # too recent
        )
        ws_manager.is_online.return_value = True
        bridge.get_status.return_value = {"activeMode": "NONE"}

        await scheduler.auto_start_insights()

        bridge.start_insights.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_busy_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """When device has an active FSM mode, insights is skipped."""
        await _seed_insights_account(session_factory)
        ws_manager.is_online.return_value = True
        bridge.get_status.return_value = {"activeMode": "POSTING"}

        await scheduler.auto_start_insights()

        bridge.start_insights.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_paused_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """A paused account is not started for insights."""
        await _seed_insights_account(
            session_factory,
            is_paused=True,
        )
        ws_manager.is_online.return_value = True

        await scheduler.auto_start_insights()

        bridge.start_insights.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: collect_insights_results
# ---------------------------------------------------------------------------

class TestCollectInsightsResults:

    @pytest.mark.asyncio
    async def test_upserts_snapshots(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Insights snapshots from the device are saved to the DB."""
        dev_id, username = await _seed_insights_account(session_factory)
        ws_manager.is_online.return_value = True
        ws_manager.get_online_device_ids.return_value = [dev_id]

        bridge.get_insights_status.return_value = {"active": True}
        bridge.get_insights.return_value = {
            "snapshots": [
                {
                    "id": 101,
                    "username": username,
                    "captionSnippet": "My first reel",
                    "reelPosition": 1,
                    "plays": 5000,
                    "likes": 200,
                    "comments": 30,
                    "shares": 10,
                    "saves": 5,
                    "reach": 4000,
                    "timestamp": int(time.time() * 1000),
                },
                {
                    "id": 102,
                    "username": username,
                    "captionSnippet": "Second reel",
                    "reelPosition": 2,
                    "plays": 3000,
                    "likes": 100,
                    "comments": 15,
                    "shares": 5,
                    "saves": 2,
                    "reach": 2500,
                    "timestamp": int(time.time() * 1000),
                },
            ],
        }

        await scheduler.collect_insights_results()

        async with session_factory() as session:
            result = await session.execute(
                select(InsightsSnapshot).where(
                    InsightsSnapshot.account_username == username,
                )
            )
            snapshots = result.scalars().all()
            assert len(snapshots) == 2

            snap_by_pos = {s.reel_position: s for s in snapshots}
            assert snap_by_pos[1].plays == 5000
            assert snap_by_pos[1].likes == 200
            assert snap_by_pos[2].plays == 3000
            assert snap_by_pos[2].likes == 100

        broadcaster.broadcast.assert_called_once_with("insights:update", {
            "ts": broadcaster.broadcast.call_args[0][1]["ts"],
        })

    @pytest.mark.asyncio
    async def test_deduplicates_snapshots(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Snapshots with the same phone_snapshot_id are not inserted twice."""
        dev_id, username = await _seed_insights_account(session_factory)
        ws_manager.is_online.return_value = True
        ws_manager.get_online_device_ids.return_value = [dev_id]

        bridge.get_insights_status.return_value = {"active": True}
        snapshot_entry = {
            "id": 200,
            "username": username,
            "plays": 1000,
            "likes": 50,
            "timestamp": int(time.time() * 1000),
        }
        bridge.get_insights.return_value = {"snapshots": [snapshot_entry]}

        # Collect twice
        await scheduler.collect_insights_results()
        scheduler._last_insights_poll_ms.clear()
        await scheduler.collect_insights_results()

        async with session_factory() as session:
            result = await session.execute(
                select(InsightsSnapshot).where(
                    InsightsSnapshot.phone_snapshot_id == 200,
                    InsightsSnapshot.device_id == dev_id,
                )
            )
            assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_skips_inactive_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """When device reports insights not active, no snapshots are collected."""
        dev_id, _ = await _seed_insights_account(session_factory)
        ws_manager.is_online.return_value = True
        ws_manager.get_online_device_ids.return_value = [dev_id]

        bridge.get_insights_status.return_value = {"active": False}

        await scheduler.collect_insights_results()

        bridge.get_insights.assert_not_called()
        broadcaster.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Background dispatch (deadlock fix)
# ---------------------------------------------------------------------------

class TestBackgroundDispatch:
    """handle_download_complete must not block the WS read loop.

    Previously it called _dispatch_video (which uses send_command) inline,
    deadlocking because the read loop was waiting for handle_message to return
    before it could receive the send_command response.

    The fix spawns _dispatch_video as an asyncio background task.
    """

    @pytest.mark.asyncio
    async def test_handle_download_complete_returns_immediately(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """handle_download_complete should return fast (not wait for send_schedule)."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True

        # Make send_schedule take a long time (simulates waiting for WS response)
        async def slow_schedule(*args: Any, **kwargs: Any) -> dict[str, Any]:
            await asyncio.sleep(10)  # would deadlock if called inline
            return {"status": "ok"}
        bridge.send_schedule = AsyncMock(side_effect=slow_schedule)

        # handle_download_complete must return quickly (< 2s), not wait for
        # the 10s slow_schedule
        try:
            await asyncio.wait_for(
                scheduler.handle_download_complete(dev_id, {
                    "filename": "reel.mp4",
                    "username": username,
                    "success": True,
                }),
                timeout=2.0,
            )
        except asyncio.TimeoutError:
            pytest.fail("handle_download_complete blocked — deadlock not fixed")

        # Give background task a moment to start
        await asyncio.sleep(0.1)

        # Verify video is marked as uploaded
        async with session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == vid_id)
            )).scalar_one()
            assert video.uploaded_to_phone is True

    @pytest.mark.asyncio
    async def test_background_dispatch_sends_schedule(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Background task should eventually call send_schedule."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True
        bridge.send_schedule.return_value = {"status": "ok"}

        await scheduler.handle_download_complete(dev_id, {
            "filename": "reel.mp4",
            "username": username,
            "success": True,
        })

        # Wait for background task to complete
        await asyncio.sleep(0.3)

        bridge.send_schedule.assert_called_once()

        # Video should be scheduled
        async with session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == vid_id)
            )).scalar_one()
            assert video.status == "scheduled"

    @pytest.mark.asyncio
    async def test_background_dispatch_handles_failure(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Background dispatch failure should not crash the scheduler."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True
        bridge.send_schedule.side_effect = asyncio.TimeoutError("simulated timeout")

        await scheduler.handle_download_complete(dev_id, {
            "filename": "reel.mp4",
            "username": username,
            "success": True,
        })

        # Wait for background task to complete
        await asyncio.sleep(0.3)

        # Video should still be marked as uploaded (not crashed)
        async with session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == vid_id)
            )).scalar_one()
            assert video.uploaded_to_phone is True

    @pytest.mark.asyncio
    async def test_background_dispatch_skips_inactive_account(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Download-complete does not finish dispatching after account deactivation."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="uploading",
            uploaded_to_phone=True,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.username == username),
                )
            ).scalar_one()
            account.is_active = False
            await session.commit()

        await scheduler._background_dispatch(vid_id, dev_id)

        bridge.send_schedule.assert_not_called()

        async with session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == vid_id)
            )).scalar_one()
            assert video.uploaded_to_phone is True
            assert video.status == "uploading"

    @pytest.mark.asyncio
    async def test_background_dispatch_skips_inactive_device(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Background dispatch does not finish after the linked device is deactivated."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory,
            username,
            device_id=dev_id,
            status="uploading",
            uploaded_to_phone=True,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            device = await session.get(Device, dev_id)
            assert device is not None
            device.is_active = False
            await session.commit()

        await scheduler._background_dispatch(vid_id, dev_id)

        bridge.send_schedule.assert_not_called()

        async with session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == vid_id)
            )).scalar_one()
            assert video.uploaded_to_phone is True
            assert video.status == "uploading"

    @pytest.mark.asyncio
    async def test_background_tasks_gc_protected(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Background tasks should be tracked to prevent GC collection."""
        dev_id, username = await _seed_device_and_account(session_factory)
        await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True
        bridge.send_schedule.return_value = {"status": "ok"}

        await scheduler.handle_download_complete(dev_id, {
            "filename": "reel.mp4",
            "username": username,
            "success": True,
        })

        # Task should be tracked
        assert len(scheduler._background_tasks) >= 1

        # After completion, it should be removed
        await asyncio.sleep(0.3)
        assert len(scheduler._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_background_dispatch(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Stopping the scheduler cancels any in-flight background dispatch task."""
        dev_id, username = await _seed_device_and_account(session_factory)
        await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True

        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow_schedule(*args: Any, **kwargs: Any) -> dict[str, Any]:
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return {"status": "ok"}

        bridge.send_schedule = AsyncMock(side_effect=slow_schedule)

        await scheduler.handle_download_complete(dev_id, {
            "filename": "reel.mp4",
            "username": username,
            "success": True,
        })

        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert len(scheduler._background_tasks) == 1

        await scheduler.stop()

        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        assert len(scheduler._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_duplicate_download_complete_ignored(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
        broadcaster: AsyncMock,
    ) -> None:
        """Multiple download_complete events for the same video dispatch only once."""
        dev_id, username = await _seed_device_and_account(session_factory)
        vid_id = await _add_video(
            session_factory, username, device_id=dev_id,
            status="uploading", uploaded_to_phone=False,
            scheduled_time=datetime.utcnow() + timedelta(minutes=5),
        )
        ws_manager.is_online.return_value = True
        bridge.send_schedule.return_value = {"status": "ok"}

        payload = {"filename": "reel.mp4", "username": username, "success": True}

        # Fire 3 download_complete events (simulates phone reporting 100% 3x)
        await scheduler.handle_download_complete(dev_id, payload)
        await scheduler.handle_download_complete(dev_id, payload)
        await scheduler.handle_download_complete(dev_id, payload)

        # Wait for background tasks
        await asyncio.sleep(0.5)

        # Only one schedule command should have been sent
        assert bridge.send_schedule.call_count == 1


# ---------------------------------------------------------------------------
# Feature A — _maybe_generate_carousel dedup queries
# ---------------------------------------------------------------------------


async def _seed_photo_set(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    model: str | None,
    max_uses_per_account: int | None = None,
    num_images: int = 3,
    photo_set_uuid: str | None = None,
) -> int:
    from server.models import PhotoSet, PhotoSetImage
    async with session_factory() as session:
        ps = PhotoSet(
            uuid=photo_set_uuid or f"uuid-{name}",
            name=name,
            model=model,
            tags="[]",
            is_active=True,
            max_uses_per_account=max_uses_per_account,
        )
        session.add(ps)
        await session.flush()
        for i in range(num_images):
            session.add(PhotoSetImage(
                set_id=ps.id,
                filename=f"{i + 1:02d}.jpg",
                sort_order=i,
            ))
        await session.commit()
        return ps.id


async def _seed_photo_set_usage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    set_id: int,
    account_username: str,
    video_id: int = 1,
    used_at: datetime | None = None,
) -> None:
    from server.models import PhotoSetUsage
    async with session_factory() as session:
        usage = PhotoSetUsage(
            set_id=set_id,
            video_id=video_id,
            account_username=account_username,
        )
        if used_at is not None:
            usage.used_at = used_at
        session.add(usage)
        await session.commit()


class TestMaybeGenerateCarousel:
    """Exercise the rejection-query logic added in Feature A.

    Tests call `_maybe_generate_carousel` directly with a mocked LLM
    caption path so the focus stays on the PhotoSet→Video transition
    (which is where the dedup filters live).
    """

    @pytest.mark.asyncio
    async def test_creates_video_when_eligible_set_exists(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Happy path: eligible set → Video + PhotoSetUsage created."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="ps1", model="baddie",
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()

            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="auto caption"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(Video.account_username == username),
            )).scalars().all()
            from server.models import PhotoSetUsage
            usages = (await session.execute(
                select(PhotoSetUsage).where(
                    PhotoSetUsage.account_username == username,
                ),
            )).scalars().all()

        assert len(videos) == 1
        assert videos[0].content_type == "carousel"
        assert videos[0].caption == "auto caption"
        assert len(usages) == 1
        assert usages[0].set_id == set_id

    @pytest.mark.asyncio
    async def test_one_shot_blocks_after_first_use(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """max_uses_per_account=1 → after one use, set is permanently excluded."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="one-shot", model="baddie",
            max_uses_per_account=1,
        )
        # Pre-seed one usage row → cap hit
        await _seed_photo_set_usage(
            session_factory, set_id=set_id, account_username=username,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()

            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="should not be called"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 0, "Capped set must not produce a new Video row"

    @pytest.mark.asyncio
    async def test_max_uses_three_allows_post_at_two_uses(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """max_uses=3 → set eligible at 2/3 uses (ceiling not yet hit).

        Split from the old combined test (Codex Q3): this one exercises
        only the "N-1 uses = eligible" branch.
        """
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="triple-eligible", model="baddie",
            max_uses_per_account=3,
        )
        # Two usage rows — cap not yet hit (2 < 3)
        await _seed_photo_set_usage(
            session_factory, set_id=set_id, account_username=username, video_id=1,
        )
        await _seed_photo_set_usage(
            session_factory, set_id=set_id, account_username=username, video_id=2,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="third post"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 1, "Set must still be eligible at 2/3 uses"

    @pytest.mark.asyncio
    async def test_max_uses_three_blocks_at_three_uses(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """max_uses=3 → set blocked once three usage rows exist.

        Split from the old combined test (Codex Q3 fix): fresh state,
        no mid-test row manipulation. Pre-seeds exactly three
        PhotoSetUsage rows, then asserts the scheduler creates NO
        Video row.
        """
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="triple-blocked", model="baddie",
            max_uses_per_account=3,
        )
        # Pre-seed three usage rows: cap is exactly hit.
        for vid in (1, 2, 3):
            await _seed_photo_set_usage(
                session_factory,
                set_id=set_id,
                account_username=username,
                video_id=vid,
            )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="should not fire"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 0, "Set must be excluded once 3 uses recorded"

    @pytest.mark.asyncio
    async def test_null_max_uses_respects_cooldown_window(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """NULL max_uses → cooldown window applies; recent usage blocks it.

        Reads the actual `carousel_set_cooldown_days` from the scheduler's
        config (Codex Q4) so the test stays correct if the default
        changes.
        """
        cooldown_days = scheduler.config.carousel_set_cooldown_days
        assert cooldown_days >= 2, (
            f"Test fixture requires cooldown >= 2 days, got {cooldown_days}"
        )
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="legacy", model="baddie",
            max_uses_per_account=None,
        )
        # "Recent" = halfway inside the configured cooldown. Guaranteed
        # to be inside the window regardless of config tweaks.
        inside_window = datetime.utcnow() - timedelta(days=cooldown_days // 2)
        await _seed_photo_set_usage(
            session_factory, set_id=set_id, account_username=username, used_at=inside_window,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="blocked"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 0, "NULL cap must still respect cooldown window"

    @pytest.mark.asyncio
    async def test_null_max_uses_allowed_after_cooldown_expires(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """NULL max_uses + usage older than cooldown → set becomes eligible again."""
        cooldown_days = scheduler.config.carousel_set_cooldown_days
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="old-usage", model="baddie",
            max_uses_per_account=None,
        )
        # Usage well outside the cooldown window — 2x the configured
        # value so any reasonable config change still passes.
        outside_window = datetime.utcnow() - timedelta(days=cooldown_days * 2 + 1)
        await _seed_photo_set_usage(
            session_factory, set_id=set_id, account_username=username, used_at=outside_window,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="fresh cycle"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 1, "Stale usage must not block beyond cooldown"

    @pytest.mark.asyncio
    async def test_in_session_guard_prevents_duplicate(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two sequential calls on the SAME session must not create duplicates.

        The guard at scheduler.py:1743 counts pending carousels and
        returns early if one already exists. The first call flushes
        (but doesn't commit) a Video row; SQLAlchemy's identity map
        makes that row visible to subsequent queries in the same
        session, so the second call's guard fires. Codex Q5
        concurrent dedup coverage.
        """
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        await _seed_photo_set(session_factory, name="dupe-test", model="baddie")

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="caption"),
            ):
                await scheduler._maybe_generate_carousel(account, session)
                # Second call — guard must fire, no second Video row
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
            from server.models import PhotoSetUsage
            usages = (await session.execute(
                select(PhotoSetUsage).where(
                    PhotoSetUsage.account_username == username,
                ),
            )).scalars().all()

        assert len(videos) == 1, "Guard must prevent duplicate Video rows"
        assert len(usages) == 1, "Guard must prevent duplicate PhotoSetUsage rows"

    @pytest.mark.asyncio
    async def test_cross_session_guard_prevents_duplicate(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two sequential calls in SEPARATE sessions must still dedupe.

        Simulates two sequential APScheduler ticks: tick 1 creates the
        Video + commits; tick 2 opens a fresh session and must see the
        committed Video so its pending-count guard catches the dupe.
        """
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        await _seed_photo_set(session_factory, name="cross-session", model="baddie")

        with patch.object(
            scheduler,
            "_generate_carousel_caption",
            new=AsyncMock(return_value="caption"),
        ):
            # Tick 1 — own session + commit
            async with session_factory() as session:
                account = (await session.execute(
                    select(Account).where(Account.username == username),
                )).scalar_one()
                await scheduler._maybe_generate_carousel(account, session)

            # Tick 2 — fresh session, same account
            async with session_factory() as session:
                account = (await session.execute(
                    select(Account).where(Account.username == username),
                )).scalar_one()
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
        assert len(videos) == 1, (
            "Cross-session dedup must hold — second tick sees committed Video"
        )

    @pytest.mark.asyncio
    async def test_video_and_usage_row_both_committed(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Single call → both Video and PhotoSetUsage present after commit.

        Atomicity check (Codex Q5): the two rows live in the same
        transaction. A partial commit (only Video, no usage row) would
        be a silent corruption of the dedup bookkeeping. Verified by
        re-reading in a fresh session post-call.
        """
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="atomic", model="baddie",
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="atomic caption"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        # Fresh session — enforces a round-trip to disk
        async with session_factory() as session:
            from server.models import PhotoSetUsage
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "carousel",
                ),
            )).scalars().all()
            usages = (await session.execute(
                select(PhotoSetUsage).where(
                    PhotoSetUsage.set_id == set_id,
                    PhotoSetUsage.account_username == username,
                ),
            )).scalars().all()

        assert len(videos) == 1
        assert len(usages) == 1
        # The usage row must reference the same Video that was created
        assert usages[0].video_id == videos[0].id

    @pytest.mark.asyncio
    async def test_deterministic_order_picks_lowest_id(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """With two eligible sets, ORDER BY PhotoSet.id.asc() picks the older one."""
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        first_id = await _seed_photo_set(
            session_factory, name="first", model="baddie",
        )
        second_id = await _seed_photo_set(
            session_factory, name="second", model="baddie",
        )
        assert first_id < second_id

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            with patch.object(
                scheduler,
                "_generate_carousel_caption",
                new=AsyncMock(return_value="deterministic"),
            ):
                await scheduler._maybe_generate_carousel(account, session)

        async with session_factory() as session:
            from server.models import PhotoSetUsage
            usages = (await session.execute(
                select(PhotoSetUsage).where(
                    PhotoSetUsage.account_username == username,
                ),
            )).scalars().all()
        assert len(usages) == 1
        assert usages[0].set_id == first_id, "Lower-id set must be chosen first"


# ---------------------------------------------------------------------------
# prune_donor_usage — 72h retention
# ---------------------------------------------------------------------------


class TestPruneDonorUsage:
    @pytest.mark.asyncio
    async def test_drops_old_rows(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Rows older than 72h are deleted; fresher rows survive."""
        from server.models import DonorUsage

        # Seed 1 stale (4 days old) + 1 fresh (1 hour old) row.
        # DonorUsage FK -> devices.id ON DELETE CASCADE, so we need a
        # real device.
        dev_id, _ = await _seed_device_and_account(session_factory)

        async with session_factory() as session:
            session.add(DonorUsage(
                device_id=dev_id,
                donor_model="iphone_12",
                donor_index=5,
                used_at=datetime.utcnow() - timedelta(days=4),
            ))
            session.add(DonorUsage(
                device_id=dev_id,
                donor_model="iphone_15_pro_max",
                donor_index=10,
                used_at=datetime.utcnow() - timedelta(hours=1),
            ))
            await session.commit()

        await scheduler.prune_donor_usage()

        async with session_factory() as session:
            surviving = (await session.execute(
                select(DonorUsage),
            )).scalars().all()
        assert len(surviving) == 1
        assert surviving[0].donor_model == "iphone_15_pro_max"

    @pytest.mark.asyncio
    async def test_no_op_when_table_empty(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No rows → no crash, no error."""
        # Should not raise.
        await scheduler.prune_donor_usage()


class TestPhotoSetSiblingGap:
    """T1: dispatch-time sibling gap for photo set reuse on same device."""

    async def _prep_carousel_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        device_id: int,
        username: str,
        set_id: int,
        filename: str = "carousel_1.jpg",
        uploaded_to_phone: bool = False,
    ) -> int:
        """Seed a carousel Video row + linked PhotoSetUsage, return video.id."""
        from server.models import PhotoSetUsage
        async with session_factory() as session:
            vid = Video(
                filename=filename,
                account_username=username,
                content_type="carousel",
                image_filenames='["01.jpg","02.jpg","03.jpg"]',
                status="pending",
                device_id=device_id,
                uploaded_to_phone=uploaded_to_phone,
                scheduled_time=datetime.utcnow() - timedelta(seconds=5),
            )
            session.add(vid)
            await session.flush()
            session.add(PhotoSetUsage(
                set_id=set_id,
                video_id=vid.id,
                account_username=username,
            ))
            await session.commit()
            return vid.id

    @pytest.mark.asyncio
    async def test_dispatch_blocks_within_gap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """Second carousel using same photo set on same device within gap → deferred."""
        from server.models import PhotoSetUsage
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="sibling-gap", model="baddie",
        )
        ws_manager.is_online.return_value = True

        # Pre-seed a PhotoSetUsage row that simulates a recent dispatch
        from server.models import PhotoSetUsage as PSU
        async with session_factory() as session:
            recent_usage = PSU(
                set_id=set_id,
                video_id=9999,
                account_username="sibling",
                device_id=device_id,
            )
            recent_usage.dispatched_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(recent_usage)
            await session.commit()

        # New carousel video to try to dispatch
        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_id,
            username=username,
            set_id=set_id,
        )

        async with session_factory() as session:
            vid = (await session.execute(
                select(Video).where(Video.id == video_id),
            )).scalar_one()
            result = await scheduler._dispatch_video(session, vid)
            await session.commit()

        assert result == "deferred"
        bridge.send_carousel_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_allows_outside_gap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """Previous dispatch 60 min ago (gap=45) → new dispatch proceeds."""
        from server.models import PhotoSetUsage as PSU
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="outside-gap", model="baddie",
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            old = PSU(
                set_id=set_id, video_id=8888, account_username="sibling",
                device_id=device_id,
            )
            old.dispatched_at = datetime.utcnow() - timedelta(minutes=60)
            session.add(old)
            await session.commit()

        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_id,
            username=username,
            set_id=set_id,
        )

        scheduler.config.ghost_enabled = False
        async with session_factory() as session:
                vid = (await session.execute(
                    select(Video).where(Video.id == video_id),
                )).scalar_one()
                result = await scheduler._dispatch_video(session, vid)
                await session.commit()

        assert result == "dispatched"
        bridge.send_carousel_download.assert_called_once()

    @pytest.mark.asyncio
    async def test_different_device_no_block(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """Recent usage on device A should not block dispatch on device B."""
        from server.models import PhotoSetUsage as PSU
        device_a, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        # Add a second device + second account linked to it
        async with session_factory() as session:
            dev_b = Device(
                device_id="TEST-SERIAL-B",
                name="Device B",
                ip_address="192.168.1.11",
                status="online",
                last_seen_at=datetime.utcnow(),
            )
            session.add(dev_b)
            await session.flush()
            acct_b = Account(username="user_b", is_active=True, recreator_model="baddie")
            session.add(acct_b)
            await session.flush()
            link = AccountDevice(
                account_username="user_b", device_id=dev_b.id, is_primary=True,
            )
            session.add(link)
            await session.commit()
            device_b = dev_b.id

        set_id = await _seed_photo_set(
            session_factory, name="per-device", model="baddie",
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            usage_a = PSU(
                set_id=set_id, video_id=7777, account_username="other",
                device_id=device_a,
            )
            usage_a.dispatched_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(usage_a)
            await session.commit()

        # New video on device B using same photo set
        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_b,
            username="user_b",
            set_id=set_id,
        )

        scheduler.config.ghost_enabled = False
        async with session_factory() as session:
            vid = (await session.execute(
                select(Video).where(Video.id == video_id),
            )).scalar_one()
            result = await scheduler._dispatch_video(session, vid)
            await session.commit()

        assert result == "dispatched"

    @pytest.mark.asyncio
    async def test_exempt_device_bypasses_gap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """Device in farm_rate_limit_exempt_devices bypasses the gap."""
        from server.models import PhotoSetUsage as PSU
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="exempt-device", model="baddie",
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            recent = PSU(
                set_id=set_id, video_id=6666, account_username="sibling",
                device_id=device_id,
            )
            recent.dispatched_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(recent)
            await session.commit()

        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_id,
            username=username,
            set_id=set_id,
        )

        scheduler.config.ghost_enabled = False
        scheduler.config.farm_rate_limit_exempt_devices = [device_id]
        async with session_factory() as session:
            vid = (await session.execute(
                select(Video).where(Video.id == video_id),
            )).scalar_one()
            result = await scheduler._dispatch_video(session, vid)
            await session.commit()

        assert result == "dispatched"

    @pytest.mark.asyncio
    async def test_uploaded_to_phone_bypasses_gap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """Gap check is skipped when uploaded_to_phone=True — prevents
        stranding videos whose assets already reached the phone."""
        from server.models import PhotoSetUsage as PSU
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="already-uploaded", model="baddie",
        )
        ws_manager.is_online.return_value = True

        async with session_factory() as session:
            recent = PSU(
                set_id=set_id, video_id=5555, account_username="sibling",
                device_id=device_id,
            )
            recent.dispatched_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(recent)
            await session.commit()

        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_id,
            username=username,
            set_id=set_id,
            uploaded_to_phone=True,
        )

        bridge.send_schedule.return_value = {
            "videoIds": [{"filename": "carousel_1.jpg", "videoId": 42}],
        }
        async with session_factory() as session:
            vid = (await session.execute(
                select(Video).where(Video.id == video_id),
            )).scalar_one()
            result = await scheduler._dispatch_video(session, vid)
            await session.commit()

        assert result == "dispatched"
        bridge.send_schedule.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatched_at_stamped_after_bridge_success(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        bridge: AsyncMock,
        ws_manager: MagicMock,
    ) -> None:
        """After bridge.send_carousel_download succeeds, PhotoSetUsage.
        dispatched_at and device_id must be set."""
        from server.models import PhotoSetUsage as PSU
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="stamp-check", model="baddie",
        )
        ws_manager.is_online.return_value = True

        video_id = await self._prep_carousel_dispatch(
            session_factory,
            device_id=device_id,
            username=username,
            set_id=set_id,
        )

        scheduler.config.ghost_enabled = False
        async with session_factory() as session:
            vid = (await session.execute(
                select(Video).where(Video.id == video_id),
            )).scalar_one()
            await scheduler._dispatch_video(session, vid)
            await session.commit()

        async with session_factory() as session:
            usage = (await session.execute(
                select(PSU).where(PSU.video_id == video_id),
            )).scalar_one()
            assert usage.dispatched_at is not None
            assert usage.device_id == device_id
            assert usage.posted_at is None

    @pytest.mark.asyncio
    async def test_upsert_post_log_stamps_posted_at(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Successful post result should stamp PhotoSetUsage.posted_at via Video.id."""
        from server.models import PhotoSetUsage as PSU
        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        set_id = await _seed_photo_set(
            session_factory, name="posted-stamp", model="baddie",
        )

        # Seed a carousel Video with phone_video_id mapping
        async with session_factory() as session:
            vid = Video(
                filename="carousel_1.jpg",
                account_username=username,
                content_type="carousel",
                status="uploading",
                device_id=device_id,
                phone_video_id=77,
            )
            session.add(vid)
            await session.flush()
            session.add(PSU(
                set_id=set_id, video_id=vid.id, account_username=username,
                device_id=device_id,
            ))
            await session.commit()
            video_pk = vid.id

        entry = {
            "id": 12345,
            "videoId": 77,
            "accountUsername": username,
            "result": "success",
            "durationMs": 1500,
        }
        async with session_factory() as session:
            await scheduler._upsert_post_log(session, device_id, entry)
            await session.commit()

        async with session_factory() as session:
            psu = (await session.execute(
                select(PSU).where(PSU.video_id == video_pk),
            )).scalar_one()
            assert psu.posted_at is not None


class TestStoryElementPick:
    """T4: per-account story element resolvers.

    Covers: weighted pick at boundary, degenerate zero-sum fallback,
    per-account override beating global defaults, max_per_day resolution.
    """

    @pytest.mark.asyncio
    async def test_pick_respects_weights_boundary(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """random.random()=0.6 × total(1.0) = 0.6 lands in 'question' bucket.
        Cumulative: poll[0..0.5], question[0.5..0.8], text[0.8..1.0]."""
        account = Account(
            username="u1",
            is_active=True,
            story_element_weights='{"poll":0.5,"question":0.3,"text":0.2}',
        )
        with patch("server.scheduler.random.random", return_value=0.6):
            result = scheduler._resolve_story_element_type(account)
        assert result == "question"

    @pytest.mark.asyncio
    async def test_pick_at_poll_boundary(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """random.random()=0.4 × 1.0 = 0.4 lands in 'poll' bucket."""
        account = Account(
            username="u2",
            is_active=True,
            story_element_weights='{"poll":0.5,"question":0.3,"text":0.2}',
        )
        with patch("server.scheduler.random.random", return_value=0.4):
            result = scheduler._resolve_story_element_type(account)
        assert result == "poll"

    @pytest.mark.asyncio
    async def test_pick_falls_back_to_last_when_total_zero(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """All-zero weights → graceful fallback, not a division error."""
        account = Account(
            username="u3",
            is_active=True,
            story_element_weights='{"poll":0,"question":0,"text":0}',
        )
        # Should not raise; should return last key (fallback)
        result = scheduler._resolve_story_element_type(account)
        assert result in ("poll", "question", "text")

    @pytest.mark.asyncio
    async def test_pick_respects_zero_weight_disable(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """weight=0 on question+text → only poll ever picked."""
        account = Account(
            username="u4",
            is_active=True,
            story_element_weights='{"poll":1,"question":0,"text":0}',
        )
        # With only poll having weight, any random value returns poll
        for r in [0.0, 0.25, 0.5, 0.75, 0.99]:
            with patch("server.scheduler.random.random", return_value=r):
                assert scheduler._resolve_story_element_type(account) == "poll"

    @pytest.mark.asyncio
    async def test_pick_falls_back_to_global_when_null(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """Account with no override uses config.story_element_weights_default."""
        account = Account(
            username="u5",
            is_active=True,
            story_element_weights=None,
        )
        # Global default is {"poll":0.5,"question":0.3,"text":0.2}
        with patch("server.scheduler.random.random", return_value=0.1):
            result = scheduler._resolve_story_element_type(account)
        assert result == "poll"

    @pytest.mark.asyncio
    async def test_resolve_max_per_day_prefers_account(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        account = Account(
            username="u6",
            is_active=True,
            story_max_per_day=5,
        )
        assert scheduler._resolve_story_max_per_day(account) == 5

    @pytest.mark.asyncio
    async def test_resolve_max_per_day_falls_back_to_global(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        account = Account(
            username="u7",
            is_active=True,
            story_max_per_day=None,
        )
        # Global default is farm_max_stories_per_account_per_day=3
        assert scheduler._resolve_story_max_per_day(account) == scheduler.config.farm_max_stories_per_account_per_day

    @pytest.mark.asyncio
    async def test_resolve_element_probability_prefers_account(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        account = Account(
            username="u8",
            is_active=True,
            story_element_probability=0.9,
        )
        assert scheduler._resolve_story_element_probability(account) == 0.9

    @pytest.mark.asyncio
    async def test_resolve_element_probability_falls_back(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        account = Account(
            username="u9",
            is_active=True,
            story_element_probability=None,
        )
        expected = scheduler.config.story_element_probability
        assert scheduler._resolve_story_element_probability(account) == expected

    @pytest.mark.asyncio
    async def test_pick_handles_invalid_json_gracefully(
        self,
        scheduler: FarmScheduler,
    ) -> None:
        """Invalid JSON in story_element_weights → falls back to defaults."""
        account = Account(
            username="u10",
            is_active=True,
            story_element_weights="not valid json at all {",
        )
        # Should not raise; should use global default
        result = scheduler._resolve_story_element_type(account)
        assert result in ("poll", "question", "text")


# ---------------------------------------------------------------------------
# T2 — _maybe_generate_story single-file story post creation
# ---------------------------------------------------------------------------


async def _seed_story_asset(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    filename: str,
    model: str | None,
    media_type: str = "photo",
    max_uses_per_account: int | None = None,
    is_active: bool = True,
    caption_fallback: str | None = None,
) -> int:
    from server.models import StoryAsset
    async with session_factory() as session:
        asset = StoryAsset(
            filename=filename,
            media_type=media_type,
            model=model,
            tags="[]",
            is_active=is_active,
            caption_fallback=caption_fallback,
            max_uses_per_account=max_uses_per_account,
        )
        session.add(asset)
        await session.commit()
        return asset.id


async def _seed_story_asset_usage(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    asset_id: int,
    account_username: str,
    video_id: int = 1,
    device_id: int | None = None,
    used_at: datetime | None = None,
) -> None:
    from server.models import StoryAssetUsage
    async with session_factory() as session:
        usage = StoryAssetUsage(
            asset_id=asset_id,
            video_id=video_id,
            device_id=device_id,
            account_username=account_username,
        )
        if used_at is not None:
            usage.used_at = used_at
        session.add(usage)
        await session.commit()


def _write_story_source(data_dir: Path, filename: str) -> Path:
    """Create a real file on disk at data_dir/story_assets/<filename>.

    _maybe_generate_story verifies the source exists before copying
    it into stories/<account>/, so tests need a real byte payload —
    an empty file is fine for unit tests (we are not exercising the
    ghost pipeline here).
    """
    src_dir = data_dir / "story_assets"
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / filename
    src.write_bytes(b"fake-image-bytes")
    return src


class TestMaybeGenerateStory:
    """Unit tests for ``_maybe_generate_story``.

    These hit the rejection-query logic identical in shape to
    ``_maybe_generate_carousel`` but over the story_assets /
    story_asset_usage tables. The generation hook writes a real
    file on disk via ``shutil.copy2``, so each test seeds a
    story_assets/<filename> inside the scheduler's configured
    ``data_dir`` (the ``config`` fixture uses ``tmp_path`` in
    sibling tests but here defaults to ``/tmp/test-data``; we
    override via ``monkeypatch`` to keep the tree in tmp_path).
    """

    @pytest.mark.asyncio
    async def test_creates_video_when_eligible_asset_exists(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Happy path: eligible asset → Video + StoryAssetUsage created."""
        # Point the scheduler at an isolated data_dir so the
        # stories/ + story_assets/ trees don't leak between tests.
        scheduler.config.data_dir = str(tmp_path / "data")
        _write_story_source(Path(scheduler.config.data_dir), "happy.jpg")

        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        asset_id = await _seed_story_asset(
            session_factory, filename="happy.jpg", model="baddie",
            caption_fallback="hey bae",
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            await scheduler._generate_story_for_account(session, account)
            await session.commit()

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "story",
                ),
            )).scalars().all()
            from server.models import StoryAssetUsage
            usages = (await session.execute(
                select(StoryAssetUsage).where(
                    StoryAssetUsage.account_username == username,
                ),
            )).scalars().all()

        assert len(videos) == 1
        vid = videos[0]
        assert vid.content_type == "story"
        # Filename must be the copied stories/<account>/<uuid8>_<orig>,
        # NOT the raw story_assets filename (Codex flag #2).
        assert vid.filename.endswith("_happy.jpg")
        assert vid.filename != "happy.jpg"
        assert vid.caption == "hey bae"
        # Codex flag #1: story dispatch uses video.download, so
        # image_filenames MUST be NULL on story rows.
        assert vid.image_filenames is None
        # original_path tracks the copied destination for cleanup.
        assert vid.original_path is not None
        assert "stories" in vid.original_path
        assert Path(vid.original_path).exists()

        assert len(usages) == 1
        assert usages[0].asset_id == asset_id

    @pytest.mark.asyncio
    async def test_respects_hard_cap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """max_uses_per_account=1 → no Video after first usage row exists."""
        scheduler.config.data_dir = str(tmp_path / "data")
        _write_story_source(Path(scheduler.config.data_dir), "capped.jpg")

        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        asset_id = await _seed_story_asset(
            session_factory, filename="capped.jpg", model="baddie",
            max_uses_per_account=1,
        )
        await _seed_story_asset_usage(
            session_factory, asset_id=asset_id, account_username=username,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            await scheduler._generate_story_for_account(session, account)
            await session.commit()

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "story",
                ),
            )).scalars().all()
        assert len(videos) == 0, "Capped asset must not produce a Video"

    @pytest.mark.asyncio
    async def test_respects_cooldown_for_null_cap(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """NULL cap + recent usage → excluded by the cooldown subquery."""
        scheduler.config.data_dir = str(tmp_path / "data")
        _write_story_source(Path(scheduler.config.data_dir), "cool.jpg")

        device_id, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        asset_id = await _seed_story_asset(
            session_factory, filename="cool.jpg", model="baddie",
            max_uses_per_account=None,
        )
        gap = scheduler.config.farm_min_storyasset_reuse_gap_minutes
        assert gap > 0
        inside_window = datetime.utcnow() - timedelta(minutes=gap / 2)
        await _seed_story_asset_usage(
            session_factory, asset_id=asset_id, account_username=username,
            used_at=inside_window, device_id=device_id,
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            await scheduler._generate_story_for_account(session, account)
            await session.commit()

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "story",
                ),
            )).scalars().all()
        assert len(videos) == 0, "Recent NULL-cap usage must block the pick"

    @pytest.mark.asyncio
    async def test_no_eligible_asset_is_no_op(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """No asset for the account's model → scheduler is a no-op."""
        scheduler.config.data_dir = str(tmp_path / "data")
        _, username = await _seed_device_and_account(
            session_factory,
            account_kwargs={"recreator_model": "baddie"},
        )
        # Seed an asset for a DIFFERENT model so the filter excludes it.
        await _seed_story_asset(
            session_factory, filename="other.jpg", model="other_model",
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == username),
            )).scalar_one()
            await scheduler._generate_story_for_account(session, account)
            await session.commit()

        async with session_factory() as session:
            videos = (await session.execute(
                select(Video).where(
                    Video.account_username == username,
                    Video.content_type == "story",
                ),
            )).scalars().all()
            from server.models import StoryAssetUsage
            usages = (await session.execute(
                select(StoryAssetUsage).where(
                    StoryAssetUsage.account_username == username,
                ),
            )).scalars().all()
        assert len(videos) == 0
        assert len(usages) == 0
