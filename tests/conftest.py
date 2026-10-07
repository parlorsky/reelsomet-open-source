"""Shared fixtures for the Reelsomet VPS test suite."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth import create_jwt_token, hash_password
from server.config import VPSConfig, load_config
from server.models import (
    Account,
    AccountDevice,
    Base,
    Device,
    EngagementAction,
    EngagementSession,
    EngagementTarget,
    InsightsSnapshot,
    MonitorSnapshot,
    MonitorTarget,
    PostLog,
    Video,
)
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import WSMessage


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

SAMPLE_YAML = {
    "server": {
        "host": "127.0.0.1",
        "port": 9999,
        "api_key": "test-api-key",
        "domain": "test.example.com",
    },
    "websocket": {
        "ping_interval": 15.0,
        "ping_timeout": 5.0,
    },
    "database": {
        "path": "/tmp/test-farm.db",
    },
    "farm": {
        "timezone": "Europe/Moscow",
    },
    "data_dir": "/tmp/test-data",
    "static_dir": "/tmp/test-static",
    "auth": {
        "admin_password_hash": "",
        "jwt_secret": "test-jwt-secret-value-for-tests",
        "jwt_expire_hours": 2,
        "setup_token": "sample-setup-token",
    },
    "telegram": {
        "bot_token": "123456:ABC-DEF",
        "admin_chat_ids": [111, 222],
    },
    "llm": {
        "base_url": "http://localhost:11434",
        "api_key": "test-llm-key",
        "model": "test-model",
        "engagement_timeout": 15.0,
    },
}

# A fixed admin password for tests
TEST_ADMIN_PASSWORD = "test-admin-password-123"
TEST_JWT_SECRET = "test-jwt-secret-value-for-tests"
_TEST_PASSWORD_HASH = hash_password(TEST_ADMIN_PASSWORD)


@pytest.fixture
def tmp_config_path(tmp_path: Path) -> Path:
    """Create a temp YAML config file with test values and return its path."""
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.dump(SAMPLE_YAML, f, default_flow_style=False)
    return config_file


@pytest.fixture
def test_config(tmp_path: Path) -> VPSConfig:
    """Return a VPSConfig with known test defaults.

    Uses tmp_path for database_path so the lifespan can create the DB file
    without polluting the filesystem.
    """
    db_path = str(tmp_path / "data" / "test-farm.db")
    return VPSConfig(
        host="127.0.0.1",
        port=9999,
        api_key="test-api-key",
        ws_ping_interval=15.0,
        ws_ping_timeout=5.0,
        database_path=db_path,
        data_dir=str(tmp_path / "data"),
        static_dir=str(tmp_path / "static"),
        domain="test.example.com",
        admin_password_hash=_TEST_PASSWORD_HASH,
        jwt_secret=TEST_JWT_SECRET,
        jwt_expire_hours=2,
        telegram_bot_token="123456:ABC-DEF",
        telegram_admin_chat_ids=[111, 222],
        llm_base_url="http://localhost:11434",
        llm_api_key="test-llm-key",
        llm_model="test-model",
        engagement_llm_timeout=15.0,
        farm_timezone="Europe/Moscow",
        ghost_enabled=True,
    )


# ---------------------------------------------------------------------------
# Database fixtures (in-memory SQLite)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Async SQLAlchemy engine backed by in-memory SQLite."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Async session from the in-memory engine."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def seed_db(db_engine: AsyncEngine) -> dict[str, Any]:
    """Seed the in-memory DB with test data. Returns a dict of created objects.

    Creates:
    - 2 devices (dev-A, dev-B)
    - 3 accounts (user_alpha, user_beta, user_gamma)
    - 5 videos (mixed statuses)
    - 3 post logs
    - 1 engagement session with 2 actions
    - 2 engagement targets
    - 2 insights snapshots
    - 1 monitor target + 1 monitor snapshot
    """
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.utcnow()

    async with factory() as session:
        # Devices
        dev_a = Device(
            device_id="DEV-A-SERIAL",
            name="Test Honor",
            ip_address="192.168.1.10",
            port=8080,
            device_model="FNE-NX9",
            android_version="14",
            is_active=True,
            status="online",
        )
        dev_b = Device(
            device_id="DEV-B-SERIAL",
            name="Test Realme",
            ip_address="192.168.1.11",
            port=8080,
            device_model="RMX3771",
            android_version="15",
            is_active=True,
            status="offline",
        )
        session.add_all([dev_a, dev_b])
        await session.flush()

        # Accounts
        acc_alpha = Account(username="user_alpha", is_active=True, total_posted=10, total_failed=1)
        acc_beta = Account(username="user_beta", is_active=True, is_paused=True, total_posted=5, total_failed=0)
        acc_gamma = Account(username="user_gamma", is_active=True, engagement_enabled=True, total_posted=20, total_failed=3)
        session.add_all([acc_alpha, acc_beta, acc_gamma])
        await session.flush()

        # Account-Device links
        link_a = AccountDevice(account_username="user_alpha", device_id=dev_a.id, is_primary=True)
        link_b = AccountDevice(account_username="user_beta", device_id=dev_a.id)
        link_c = AccountDevice(account_username="user_gamma", device_id=dev_b.id, is_primary=True)
        session.add_all([link_a, link_b, link_c])

        # Videos (5 total, mixed statuses)
        vid_1 = Video(filename="reel_001.mp4", account_username="user_alpha", status="pending", device_id=dev_a.id)
        vid_2 = Video(filename="reel_002.mp4", account_username="user_alpha", status="posted", device_id=dev_a.id, posted_at=now - timedelta(hours=2))
        vid_3 = Video(filename="reel_003.mp4", account_username="user_beta", status="failed", device_id=dev_a.id, post_error="Upload timeout")
        vid_4 = Video(filename="reel_004.mp4", account_username="user_gamma", status="pending", device_id=dev_b.id)
        vid_5 = Video(filename="reel_005.mp4", account_username="user_gamma", status="cancelled", device_id=dev_b.id)
        session.add_all([vid_1, vid_2, vid_3, vid_4, vid_5])
        await session.flush()

        # Post logs
        log_1 = PostLog(
            account_username="user_alpha",
            video_id=vid_2.id,
            device_id=dev_a.id,
            result="success",
            duration_ms=12345,
        )
        log_2 = PostLog(
            account_username="user_beta",
            video_id=vid_3.id,
            device_id=dev_a.id,
            result="failed",
            error_message="Upload timeout",
        )
        log_3 = PostLog(
            account_username="user_gamma",
            device_id=dev_b.id,
            result="success",
            duration_ms=9876,
        )
        session.add_all([log_1, log_2, log_3])

        # Engagement session + actions
        eng_session = EngagementSession(
            account_username="user_gamma",
            device_id=dev_b.id,
            status="completed",
            channels_visited=3,
            reels_watched=10,
            total_likes=5,
            total_comments=2,
            started_at=now - timedelta(hours=1),
            finished_at=now,
        )
        session.add(eng_session)
        await session.flush()

        eng_action_1 = EngagementAction(
            session_id=eng_session.id,
            account_username="user_gamma",
            target_username="competitor_1",
            action_type="like",
            success=True,
            performed_at=now - timedelta(minutes=30),
        )
        eng_action_2 = EngagementAction(
            session_id=eng_session.id,
            account_username="user_gamma",
            target_username="competitor_1",
            action_type="comment",
            comment_text="Nice reel!",
            success=True,
            performed_at=now - timedelta(minutes=25),
        )
        session.add_all([eng_action_1, eng_action_2])

        # Engagement targets
        eng_target_1 = EngagementTarget(
            target_username="competitor_1",
            account_username="user_gamma",
            max_reels=5,
            should_follow=True,
        )
        eng_target_2 = EngagementTarget(
            target_username="competitor_2",
            account_username="user_gamma",
            max_reels=3,
            should_follow=False,
        )
        session.add_all([eng_target_1, eng_target_2])

        # Insights snapshots
        snap_1 = InsightsSnapshot(
            account_username="user_alpha",
            plays=1000,
            likes=50,
            comments=10,
            shares=5,
            saves=3,
            reach=800,
            caption_snippet="First reel caption",
            reel_position=1,
        )
        snap_2 = InsightsSnapshot(
            account_username="user_alpha",
            plays=2000,
            likes=100,
            comments=20,
            shares=10,
            saves=8,
            reach=1500,
            caption_snippet="Second reel caption",
            reel_position=2,
        )
        session.add_all([snap_1, snap_2])

        # Monitor targets + snapshots
        mon_target = MonitorTarget(
            target_username="monitored_user",
            account_username="user_alpha",
            max_reels=12,
        )
        session.add(mon_target)
        await session.flush()

        mon_snap = MonitorSnapshot(
            target_username="monitored_user",
            account_username="user_alpha",
            plays=5000,
            likes=200,
            comments=30,
            reel_position=1,
            caption_snippet="Monitored reel",
        )
        session.add(mon_snap)

        await session.commit()

        return {
            "devices": [dev_a, dev_b],
            "accounts": [acc_alpha, acc_beta, acc_gamma],
            "videos": [vid_1, vid_2, vid_3, vid_4, vid_5],
            "post_logs": [log_1, log_2, log_3],
            "engagement_session": eng_session,
            "engagement_actions": [eng_action_1, eng_action_2],
            "engagement_targets": [eng_target_1, eng_target_2],
            "insights_snapshots": [snap_1, snap_2],
            "monitor_target": mon_target,
            "monitor_snapshot": mon_snap,
        }


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Return Authorization headers with a valid JWT for test requests."""
    token = create_jwt_token(
        {"sub": "admin", "role": "admin"},
        TEST_JWT_SECRET,
        expires_hours=2,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def app_with_db(
    test_config: VPSConfig,
    db_engine: AsyncEngine,
    seed_db: dict[str, Any],
) -> FastAPI:
    """FastAPI test app with a real in-memory DB (seeded) injected.

    Bypasses the normal lifespan (which creates a file-based DB) and instead
    uses the in-memory engine/session from db_engine/seed_db fixtures.
    """
    from fastapi import FastAPI

    app = create_app(config=test_config)

    # Inject the in-memory engine/session factory onto app.state
    # so that the dependencies (get_db_session) use the seeded DB.
    app.state.db_engine = db_engine
    app.state.db_session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False,
    )

    return app


@pytest_asyncio.fixture
async def client(app_with_db: Any) -> AsyncGenerator[AsyncClient, None]:
    """httpx AsyncClient wired to the seeded test app."""
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        try:
            yield c
        finally:
            try:
                from server.api.studio import cancel_background_tasks
            except ModuleNotFoundError:
                return

            await cancel_background_tasks(app_with_db)


# ---------------------------------------------------------------------------
# WebSocket fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_manager() -> DeviceConnectionManager:
    """Fresh DeviceConnectionManager instance."""
    return DeviceConnectionManager()


@pytest.fixture
def mock_websocket() -> AsyncMock:
    """AsyncMock that behaves like a FastAPI WebSocket."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock()
    # Give it a fake client attribute
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    return ws


def make_mock_websocket() -> AsyncMock:
    """Factory to create multiple distinct WebSocket mocks."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    return ws


# ---------------------------------------------------------------------------
# FastAPI / HTTP fixtures (basic, no seeded DB — for old tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def test_app(test_config: VPSConfig) -> Any:
    """Create a FastAPI app with test configuration."""
    return create_app(config=test_config)


@pytest_asyncio.fixture
async def test_client(test_app: Any) -> AsyncGenerator[AsyncClient, None]:
    """httpx AsyncClient wired to the test FastAPI app (lifespan handled)."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
