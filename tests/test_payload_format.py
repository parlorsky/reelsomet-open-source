"""Tests verifying VPS payload formats match Android MessageRouter contracts.

Each test constructs the payload the same way the production code does,
then asserts the EXACT JSON structure the phone expects.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from server.config import VPSConfig
from server.models import (
    Account,
    AccountDevice,
    Base,
    Device,
    EngagementTarget,
    MonitorTarget,
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
    mgr.get_online_device_ids = MagicMock(return_value=[])
    return mgr


@pytest.fixture
def bridge() -> AsyncMock:
    b = AsyncMock(spec=DeviceBridge)
    b.send_schedule = AsyncMock(return_value={"status": "ok"})
    b.start_engagement = AsyncMock(return_value={"started": True})
    b.start_insights = AsyncMock(return_value={"started": True})
    b.start_monitoring = AsyncMock(return_value={"started": True})
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
        farm_max_posts_per_account_per_day=3,
        farm_upload_window_minutes=10,
        farm_schedule_jitter_std_seconds=0,
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
) -> FarmScheduler:
    return FarmScheduler(
        session_factory=session_factory,
        ws_manager=ws_manager,
        bridge=bridge,
        config=config,
        broadcaster=broadcaster,
    )


async def _seed(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_kwargs: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Create one device + one account + link. Return (device.id, username)."""
    async with session_factory() as session:
        dev = Device(
            device_id="SERIAL-001",
            name="TestPhone",
            ip_address="10.0.0.1",
            status="online",
            last_seen_at=datetime.utcnow(),
        )
        session.add(dev)
        await session.flush()

        kw: dict[str, Any] = {"username": "test_user", "is_active": True}
        if account_kwargs:
            kw.update(account_kwargs)
        acct = Account(**kw)
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


# ===========================================================================
# Schedule payload tests
# ===========================================================================


class TestSchedulePayload:

    @pytest.mark.asyncio
    async def test_schedule_payload_has_accounts_array(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """send_schedule payload uses nested accounts[].videos[] structure."""
        dev_id, username = await _seed(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)

        async with session_factory() as session:
            vid = Video(
                filename="reel_test.mp4",
                account_username=username,
                status="pending",
                device_id=dev_id,
                scheduled_time=scheduled,
                caption="Hello world",
                first_comment="First!",
                uploaded_to_phone=True,
            )
            session.add(vid)
            await session.commit()

        await scheduler.process_pending_videos()

        bridge.send_schedule.assert_called_once()
        payload = bridge.send_schedule.call_args[0][1]

        # Top-level key must be "accounts" (array)
        assert "accounts" in payload
        assert isinstance(payload["accounts"], list)
        assert len(payload["accounts"]) == 1

        acct = payload["accounts"][0]
        assert acct["username"] == username

        assert "videos" in acct
        assert isinstance(acct["videos"], list)
        assert len(acct["videos"]) == 1

        vid_entry = acct["videos"][0]
        assert vid_entry["filename"] == "reel_test.mp4"
        from server.insights_watermark import strip_watermark
        assert strip_watermark(vid_entry["caption"]) == "Hello world"
        assert vid_entry["firstComment"] == "First!"

    @pytest.mark.asyncio
    async def test_schedule_payload_has_immediate_scheduledTimeMs_zero(
        self,
        scheduler: FarmScheduler,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: MagicMock,
        bridge: AsyncMock,
    ) -> None:
        """Due videos are posted immediately on phone via scheduledTimeMs=0."""
        dev_id, username = await _seed(session_factory)
        scheduled = datetime.utcnow() - timedelta(seconds=5)

        async with session_factory() as session:
            vid = Video(
                filename="reel_epoch.mp4",
                account_username=username,
                status="pending",
                device_id=dev_id,
                scheduled_time=scheduled,
                uploaded_to_phone=True,
            )
            session.add(vid)
            await session.commit()

        await scheduler.process_pending_videos()

        payload = bridge.send_schedule.call_args[0][1]
        vid_entry = payload["accounts"][0]["videos"][0]

        ts_ms = vid_entry["scheduledTimeMs"]
        assert isinstance(ts_ms, int), f"Expected int, got {type(ts_ms).__name__}"
        assert ts_ms == 0


# ===========================================================================
# Engagement payload tests
# ===========================================================================


class TestEngagementPayload:

    @staticmethod
    def _build_engagement_payload(
        account: Account,
        targets: list[EngagementTarget],
    ) -> dict[str, Any]:
        """Mirror the payload construction from engagement.py start_engagement."""
        return {
            "accountUsername": account.username,
            "channels": [
                {
                    "targetUsername": t.target_username,
                    "maxReels": t.max_reels,
                    "shouldFollow": t.should_follow,
                }
                for t in targets
            ],
            "dailyBudgetMinutes": account.engagement_daily_budget if account.engagement_daily_budget is not None else 30,
            "actionProbabilities": {
                "likeProbability": account.engagement_like_prob if account.engagement_like_prob is not None else 0.7,
                "commentProbability": (
                    account.engagement_comment_prob
                    if account.engagement_comment_prob is not None
                    else 0.3
                ),
                "replyProbability": account.engagement_reply_prob if account.engagement_reply_prob is not None else 0.1,
                "shareProbability": account.engagement_share_prob if account.engagement_share_prob is not None else 0.05,
            },
            "timings": {
                "watchMinMs": 3000,
                "watchMaxMs": 8000,
                "actionCooldownMinMs": 1000,
                "actionCooldownMaxMs": 3000,
                "channelCooldownMinMs": 5000,
                "channelCooldownMaxMs": 15000,
            },
            "llmEndpoint": "",
            "useVisionLlm": True,
            "interested": 0.0,
        }

    def test_engagement_payload_has_accountUsername(self) -> None:
        """Top-level key is 'accountUsername', not 'username'."""
        acct = Account(username="eng_user", is_active=True)
        payload = self._build_engagement_payload(acct, [])
        assert "accountUsername" in payload
        assert payload["accountUsername"] == "eng_user"
        assert "username" not in payload

    def test_engagement_payload_has_channels_not_targets(self) -> None:
        """Channels are under 'channels' key, not 'targets'."""
        acct = Account(username="eng_user", is_active=True)
        t = EngagementTarget(target_username="ch1", max_reels=5, should_follow=True)
        payload = self._build_engagement_payload(acct, [t])
        assert "channels" in payload
        assert "targets" not in payload
        assert len(payload["channels"]) == 1

    def test_engagement_payload_channels_have_camelCase(self) -> None:
        """Channel entries use camelCase: targetUsername, maxReels, shouldFollow."""
        acct = Account(username="eng_user", is_active=True)
        t = EngagementTarget(
            target_username="competitor_x",
            max_reels=7,
            should_follow=False,
        )
        payload = self._build_engagement_payload(acct, [t])
        ch = payload["channels"][0]
        assert ch["targetUsername"] == "competitor_x"
        assert ch["maxReels"] == 7
        assert ch["shouldFollow"] is False
        # Must NOT have snake_case keys
        assert "target_username" not in ch
        assert "max_reels" not in ch
        assert "should_follow" not in ch

    def test_engagement_payload_has_actionProbabilities(self) -> None:
        """actionProbabilities has all 4 probability keys."""
        acct = Account(
            username="eng_user",
            is_active=True,
            engagement_like_prob=0.8,
            engagement_comment_prob=0.4,
            engagement_reply_prob=0.2,
            engagement_share_prob=0.1,
        )
        payload = self._build_engagement_payload(acct, [])
        ap = payload["actionProbabilities"]
        assert ap["likeProbability"] == 0.8
        assert ap["commentProbability"] == 0.4
        assert ap["replyProbability"] == 0.2
        assert ap["shareProbability"] == 0.1
        # Must NOT have snake_case keys
        assert "like_prob" not in ap
        assert "config" not in payload

    def test_engagement_payload_has_dailyBudgetMinutes(self) -> None:
        """dailyBudgetMinutes is present at top level."""
        acct = Account(username="eng_user", is_active=True, engagement_daily_budget=45)
        payload = self._build_engagement_payload(acct, [])
        assert "dailyBudgetMinutes" in payload
        assert payload["dailyBudgetMinutes"] == 45

    def test_engagement_payload_preserves_zero_values(self) -> None:
        """Explicit zero budget/probabilities are not replaced with defaults."""
        acct = Account(
            username="eng_user",
            is_active=True,
            engagement_daily_budget=0,
            engagement_like_prob=0.0,
            engagement_comment_prob=0.0,
            engagement_reply_prob=0.0,
            engagement_share_prob=0.0,
        )
        payload = self._build_engagement_payload(acct, [])
        assert payload["dailyBudgetMinutes"] == 0
        assert payload["actionProbabilities"] == {
            "likeProbability": 0.0,
            "commentProbability": 0.0,
            "replyProbability": 0.0,
            "shareProbability": 0.0,
        }

    def test_engagement_payload_has_llmEndpoint(self) -> None:
        """llmEndpoint is present (even if empty string)."""
        acct = Account(username="eng_user", is_active=True)
        payload = self._build_engagement_payload(acct, [])
        assert "llmEndpoint" in payload
        assert isinstance(payload["llmEndpoint"], str)

    def test_engagement_payload_has_timings(self) -> None:
        """timings object has all expected timing fields."""
        acct = Account(username="eng_user", is_active=True)
        payload = self._build_engagement_payload(acct, [])
        t = payload["timings"]
        assert "watchMinMs" in t
        assert "watchMaxMs" in t
        assert "actionCooldownMinMs" in t
        assert "actionCooldownMaxMs" in t
        assert "channelCooldownMinMs" in t
        assert "channelCooldownMaxMs" in t

    def test_engagement_payload_has_useVisionLlm(self) -> None:
        """useVisionLlm is present as a boolean."""
        acct = Account(username="eng_user", is_active=True)
        payload = self._build_engagement_payload(acct, [])
        assert "useVisionLlm" in payload
        assert isinstance(payload["useVisionLlm"], bool)


# ===========================================================================
# Insights payload tests
# ===========================================================================


class TestInsightsPayload:

    @staticmethod
    def _build_insights_payload(
        account_username: str,
        max_reels: int | None = None,
    ) -> dict[str, Any]:
        """Mirror the payload construction from insights.py collect_insights."""
        return {
            "accounts": [
                {
                    "username": account_username,
                    "knownVideoIds": [],
                    "skipReels": 0,
                },
            ],
            "maxReelsPerAccount": max_reels or 10,
        }

    def test_insights_payload_has_accounts_array(self) -> None:
        """Top-level has 'accounts' array, not flat 'username'."""
        payload = self._build_insights_payload("ins_user")
        assert "accounts" in payload
        assert isinstance(payload["accounts"], list)
        assert len(payload["accounts"]) == 1
        assert payload["accounts"][0]["username"] == "ins_user"
        assert "username" not in payload  # not at top level

    def test_insights_payload_has_maxReelsPerAccount(self) -> None:
        """maxReelsPerAccount is camelCase at top level, not 'max_reels'."""
        payload = self._build_insights_payload("ins_user", max_reels=15)
        assert "maxReelsPerAccount" in payload
        assert payload["maxReelsPerAccount"] == 15
        assert "max_reels" not in payload

    def test_insights_payload_accounts_have_knownVideoIds(self) -> None:
        """Each account entry has 'knownVideoIds' (list) and 'skipReels' (int)."""
        payload = self._build_insights_payload("ins_user")
        acct = payload["accounts"][0]
        assert "knownVideoIds" in acct
        assert isinstance(acct["knownVideoIds"], list)
        assert "skipReels" in acct
        assert isinstance(acct["skipReels"], int)


# ===========================================================================
# Monitoring payload tests
# ===========================================================================


class TestMonitoringPayload:

    @staticmethod
    def _build_monitoring_payload(
        account_username: str,
        targets: list[MonitorTarget],
    ) -> dict[str, Any]:
        """Mirror the payload construction from monitor.py run_monitoring_now."""
        return {
            "accountUsername": account_username,
            "targets": [
                {
                    "targetUsername": t.target_username,
                    "maxReels": t.max_reels,
                }
                for t in targets
            ],
        }

    def test_monitoring_payload_has_accountUsername(self) -> None:
        """Top-level key is 'accountUsername', not 'username'."""
        payload = self._build_monitoring_payload("mon_user", [])
        assert "accountUsername" in payload
        assert payload["accountUsername"] == "mon_user"
        assert "username" not in payload

    def test_monitoring_payload_targets_have_camelCase(self) -> None:
        """Target entries use camelCase: targetUsername, maxReels."""
        t = MonitorTarget(target_username="watched_user", max_reels=12)
        payload = self._build_monitoring_payload("mon_user", [t])
        assert len(payload["targets"]) == 1
        entry = payload["targets"][0]
        assert entry["targetUsername"] == "watched_user"
        assert entry["maxReels"] == 12
        # Must NOT have snake_case keys
        assert "target_username" not in entry
        assert "max_reels" not in entry
