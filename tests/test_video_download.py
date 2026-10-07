"""Tests for the video download pipeline: signed URLs, HTTPS endpoint, scheduler integration."""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.api.video_download import (
    _TOKEN_TTL_SECONDS,
    generate_download_url,
    verify_download_token,
)
from server.config import VPSConfig
from server.models import Account, AccountDevice, Base, Device, Video
from server.scheduler import FarmScheduler
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_JWT_SECRET = "test-jwt-secret-value-for-tests"


@pytest.fixture
def config(tmp_path: Path) -> VPSConfig:
    """VPSConfig with known secret and data dir."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return VPSConfig(
        host="127.0.0.1",
        port=9999,
        jwt_secret=TEST_JWT_SECRET,
        domain="",
        data_dir=str(data_dir),
        database_path=str(tmp_path / "farm.db"),
    )


@pytest.fixture
def config_with_domain(config: VPSConfig) -> VPSConfig:
    """Config with domain set (uses HTTPS URLs)."""
    config.domain = "vps.example.com"
    return config


# ---------------------------------------------------------------------------
# test_generate_download_url_format
# ---------------------------------------------------------------------------

class TestGenerateDownloadUrl:

    def test_format_without_domain(self, config: VPSConfig) -> None:
        """URL uses http://host:port when no domain is set."""
        url = generate_download_url(42, config)
        assert url.startswith("http://127.0.0.1:9999/api/videos/download/42?")
        assert "token=" in url
        assert "expires=" in url

    def test_format_with_domain(self, config_with_domain: VPSConfig) -> None:
        """URL uses https://domain when domain is set."""
        url = generate_download_url(42, config_with_domain)
        assert url.startswith("https://vps.example.com/api/videos/download/42?")
        assert "token=" in url
        assert "expires=" in url

    def test_expires_is_in_future(self, config: VPSConfig) -> None:
        """The expires param should be ~10 minutes from now."""
        url = generate_download_url(1, config)
        # Extract expires value
        parts = url.split("expires=")
        expires = int(parts[1].split("&")[0])
        now = int(time.time())
        assert expires > now
        assert expires <= now + _TOKEN_TTL_SECONDS + 2  # small tolerance

    def test_different_video_ids_produce_different_tokens(self, config: VPSConfig) -> None:
        """Two different video IDs should produce different tokens."""
        url1 = generate_download_url(1, config)
        url2 = generate_download_url(2, config)
        token1 = url1.split("token=")[1].split("&")[0]
        token2 = url2.split("token=")[1].split("&")[0]
        assert token1 != token2


# ---------------------------------------------------------------------------
# test_verify_download_token_*
# ---------------------------------------------------------------------------

class TestVerifyDownloadToken:

    def test_valid_token(self, config: VPSConfig) -> None:
        """A freshly generated token should verify successfully."""
        url = generate_download_url(42, config)
        token = url.split("token=")[1].split("&")[0]
        expires = int(url.split("expires=")[1].split("&")[0])
        assert verify_download_token(42, token, expires, config) is True

    def test_expired_token(self, config: VPSConfig) -> None:
        """An expired token should be rejected."""
        from server.api.video_download import _compute_hmac

        expires = int(time.time()) - 1  # already expired
        token = _compute_hmac(42, expires, config.jwt_secret)
        assert verify_download_token(42, token, expires, config) is False

    def test_tampered_token(self, config: VPSConfig) -> None:
        """A token signed for a different video_id should be rejected."""
        url = generate_download_url(42, config)
        token = url.split("token=")[1].split("&")[0]
        expires = int(url.split("expires=")[1].split("&")[0])
        # Verify against a different video_id
        assert verify_download_token(99, token, expires, config) is False

    def test_wrong_secret(self, config: VPSConfig) -> None:
        """A token signed with a different secret should be rejected."""
        url = generate_download_url(42, config)
        token = url.split("token=")[1].split("&")[0]
        expires = int(url.split("expires=")[1].split("&")[0])

        other_config = VPSConfig(jwt_secret="different-secret-key")
        assert verify_download_token(42, token, expires, other_config) is False


# ---------------------------------------------------------------------------
# test_download_endpoint_*
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def download_db(tmp_path: Path) -> tuple[async_sessionmaker, AsyncEngine]:
    """Create an in-memory DB with a test video."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


@pytest_asyncio.fixture
async def download_app(
    config: VPSConfig,
    download_db: tuple[async_sessionmaker, AsyncEngine],
    tmp_path: Path,
) -> Any:
    """FastAPI app configured for download tests with a video in the DB."""
    from server.app import create_app
    from server.ws.admin_broadcaster import AdminBroadcaster

    factory, engine = download_db

    # Create a test video file on disk
    video_dir = tmp_path / "data" / "videos" / "test_user"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_file = video_dir / "test_video.mp4"
    video_file.write_bytes(b"FAKE_MP4_CONTENT_1234567890")

    # Seed DB with account and video
    async with factory() as session:
        account = Account(username="test_user", is_active=True)
        session.add(account)
        await session.flush()

        video = Video(
            filename="test_video.mp4",
            original_path=str(video_file),
            account_username="test_user",
            status="pending",
        )
        session.add(video)
        await session.commit()

    app = create_app(config=config)
    app.state.db_engine = engine
    app.state.db_session_factory = factory
    app.state.admin_broadcaster = AdminBroadcaster()
    app.state.ws_manager = DeviceConnectionManager()

    return app


@pytest_asyncio.fixture
async def download_client(download_app: Any) -> AsyncClient:
    """HTTP client for download endpoint tests."""
    transport = ASGITransport(app=download_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


class TestDownloadEndpoint:

    @pytest.mark.asyncio
    async def test_streams_file(
        self, download_client: AsyncClient, config: VPSConfig,
    ) -> None:
        """Valid token returns the video file content."""
        url = generate_download_url(1, config)
        # Extract query params
        token = url.split("token=")[1].split("&")[0]
        expires = url.split("expires=")[1].split("&")[0]

        resp = await download_client.get(
            "/api/videos/download/1",
            params={"token": token, "expires": expires},
        )
        assert resp.status_code == 200
        assert resp.content == b"FAKE_MP4_CONTENT_1234567890"
        assert "video/mp4" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_expired_token_returns_403(
        self, download_client: AsyncClient, config: VPSConfig,
    ) -> None:
        """Expired token returns 403 Forbidden."""
        from server.api.video_download import _compute_hmac

        expires = int(time.time()) - 10  # already expired
        token = _compute_hmac(1, expires, config.jwt_secret)

        resp = await download_client.get(
            "/api/videos/download/1",
            params={"token": token, "expires": expires},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_video_id_returns_403(
        self, download_client: AsyncClient, config: VPSConfig,
    ) -> None:
        """Token signed for video_id=1 used on video_id=999 returns 403."""
        url = generate_download_url(1, config)
        token = url.split("token=")[1].split("&")[0]
        expires = url.split("expires=")[1].split("&")[0]

        # Use the token for video 1 but request video 999
        resp = await download_client.get(
            "/api/videos/download/999",
            params={"token": token, "expires": expires},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_file_returns_404(
        self,
        download_client: AsyncClient,
        config: VPSConfig,
        download_db: tuple[async_sessionmaker, AsyncEngine],
    ) -> None:
        """Video row exists but file on disk is gone → 404, not 500.

        Regression for the Codex Q2 TOCTOU fix 2026-04-14: the previous
        implementation wrapped ``FileResponse(...)`` in a dead
        ``try: except FileNotFoundError:``. Starlette defers the actual
        ``open()`` until the ASGI send loop, so the catch never fired.
        The fix switches to a manual ``open() + StreamingResponse`` that
        catches the race at handler time.
        """
        # Insert a Video row whose original_path points at a path that
        # does NOT exist. The `os.path.isfile` check at the top of the
        # handler returns False, so this exercises the "file not found
        # on disk" branch and verifies it's a clean 404.
        factory, _engine = download_db
        async with factory() as session:
            v = Video(
                id=4242,
                filename="ghost.mp4",
                original_path="/nonexistent/definitely/missing.mp4",
                account_username="user_alpha",
                status="pending",
            )
            session.add(v)
            await session.commit()

        url = generate_download_url(4242, config)
        token = url.split("token=")[1].split("&")[0]
        expires = url.split("expires=")[1].split("&")[0]

        resp = await download_client.get(
            "/api/videos/download/4242",
            params={"token": token, "expires": expires},
        )
        assert resp.status_code == 404
        # Must not be a 500 — that's what the old dead try/except
        # would produce on the first exception from Starlette.
        assert resp.status_code != 500

    @pytest.mark.asyncio
    async def test_content_length_header_set(
        self, download_client: AsyncClient, config: VPSConfig,
    ) -> None:
        """Content-Length header must reflect the actual file size.

        The phone uses this to drive its 5% progress ticks. A missing
        Content-Length would make `totalBytes` zero on the Android
        side and suppress all progress reporting.
        """
        url = generate_download_url(1, config)
        token = url.split("token=")[1].split("&")[0]
        expires = url.split("expires=")[1].split("&")[0]

        resp = await download_client.get(
            "/api/videos/download/1",
            params={"token": token, "expires": expires},
        )
        assert resp.status_code == 200
        expected_size = len(b"FAKE_MP4_CONTENT_1234567890")
        cl = resp.headers.get("content-length")
        assert cl is not None and int(cl) == expected_size


# ---------------------------------------------------------------------------
# Scheduler integration tests
# ---------------------------------------------------------------------------

class _BridgeStub:
    """Stub that records calls instead of sending WS messages."""

    def __init__(self) -> None:
        self.schedule_calls: list[tuple[int, dict]] = []
        self.download_calls: list[tuple[int, str, str, str, int]] = []

    async def send_schedule(self, device_id: int, payload: dict) -> dict:
        self.schedule_calls.append((device_id, payload))
        return {"status": "ok"}

    async def send_video_download(
        self,
        device_id: int,
        url: str,
        username: str,
        filename: str,
        video_id: int,
    ) -> None:
        self.download_calls.append((device_id, url, username, filename, video_id))


class _WsManagerStub:
    """Stub that tracks online devices."""

    def __init__(self) -> None:
        self._online: set[int] = set()

    def is_online(self, device_id: int) -> bool:
        return device_id in self._online

    def set_online(self, device_id: int) -> None:
        self._online.add(device_id)

    def get_online_device_ids(self) -> list[int]:
        return list(self._online)


@pytest_asyncio.fixture
async def scheduler_db() -> tuple[async_sessionmaker, AsyncEngine]:
    """In-memory DB for scheduler tests."""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory, engine


@pytest_asyncio.fixture
async def seeded_scheduler(
    scheduler_db: tuple[async_sessionmaker, AsyncEngine],
    config: VPSConfig,
) -> tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker]:
    """FarmScheduler with stub bridge/ws_manager and a seeded DB."""
    factory, engine = scheduler_db

    # Seed: 1 device, 1 account, account-device link
    async with factory() as session:
        device = Device(
            device_id="TEST-DEVICE",
            name="Test Phone",
            ip_address="10.0.0.1",
            status="online",
        )
        session.add(device)
        await session.flush()

        account = Account(username="test_user", is_active=True)
        session.add(account)
        await session.flush()

        link = AccountDevice(
            account_username="test_user",
            device_id=device.id,
            is_primary=True,
        )
        session.add(link)
        await session.commit()
        dev_id = device.id

    ws_stub = _WsManagerStub()
    ws_stub.set_online(dev_id)

    bridge_stub = _BridgeStub()

    broadcaster = AsyncMock()
    broadcaster.broadcast = AsyncMock()

    scheduler = FarmScheduler(
        session_factory=factory,
        ws_manager=ws_stub,  # type: ignore[arg-type]
        bridge=bridge_stub,  # type: ignore[arg-type]
        config=config,
        broadcaster=broadcaster,
    )

    return scheduler, bridge_stub, ws_stub, factory


class TestDispatchVideoSendsDownloadFirst:

    @pytest.mark.asyncio
    async def test_sends_download_when_not_uploaded(
        self,
        seeded_scheduler: tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker],
    ) -> None:
        """When uploaded_to_phone=False, _dispatch_video sends video.download, not schedule."""
        scheduler, bridge_stub, _, factory = seeded_scheduler

        # Create a pending video (not uploaded)
        async with factory() as session:
            video = Video(
                filename="reel_001.mp4",
                account_username="test_user",
                status="pending",
                scheduled_time=datetime.utcnow() + timedelta(minutes=5),
                uploaded_to_phone=False,
            )
            session.add(video)
            await session.flush()

            await scheduler._dispatch_video(session, video)
            await session.commit()

            assert video.status == "uploading"

        # Bridge should have received download, not schedule
        assert len(bridge_stub.download_calls) == 1
        assert len(bridge_stub.schedule_calls) == 0
        assert bridge_stub.download_calls[0][2] == "test_user"  # username
        assert bridge_stub.download_calls[0][3] == "reel_001.mp4"  # filename


class TestDispatchVideoAlreadyUploadedSendsSchedule:

    @pytest.mark.asyncio
    async def test_sends_schedule_when_already_uploaded(
        self,
        seeded_scheduler: tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker],
    ) -> None:
        """When uploaded_to_phone=True, _dispatch_video sends cmd.send_schedule directly."""
        scheduler, bridge_stub, _, factory = seeded_scheduler

        async with factory() as session:
            video = Video(
                filename="reel_002.mp4",
                account_username="test_user",
                status="pending",
                scheduled_time=datetime.utcnow() + timedelta(minutes=5),
                uploaded_to_phone=True,
            )
            session.add(video)
            await session.flush()

            await scheduler._dispatch_video(session, video)
            await session.commit()

            assert video.status == "scheduled"

        # Bridge should have received schedule, not download
        assert len(bridge_stub.schedule_calls) == 1
        assert len(bridge_stub.download_calls) == 0
        payload = bridge_stub.schedule_calls[0][1]
        assert payload["accounts"][0]["username"] == "test_user"
        assert payload["accounts"][0]["videos"][0]["filename"] == "reel_002.mp4"


class TestDownloadCompleteEventTriggersSchedule:

    @pytest.mark.asyncio
    async def test_success_triggers_schedule(
        self,
        seeded_scheduler: tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker],
    ) -> None:
        """video.download_complete with success=True marks uploaded_to_phone and dispatches schedule."""
        scheduler, bridge_stub, _, factory = seeded_scheduler

        # Create a video in "uploading" status
        async with factory() as session:
            video = Video(
                filename="reel_003.mp4",
                account_username="test_user",
                status="uploading",
                scheduled_time=datetime.utcnow() + timedelta(minutes=5),
                uploaded_to_phone=False,
            )
            session.add(video)
            await session.commit()
            vid_id = video.id

        # Simulate the download_complete event
        await scheduler.handle_download_complete(
            device_id=1,
            payload={
                "videoId": vid_id,
                "success": True,
                "path": "/storage/emulated/0/videos/test_user/reel_003.mp4",
            },
        )

        # Wait for background dispatch task to complete
        await asyncio.sleep(0.5)

        # Video should now be uploaded_to_phone=True and scheduled
        async with factory() as session:
            result = await session.execute(select(Video).where(Video.id == vid_id))
            video = result.scalar_one()
            assert video.uploaded_to_phone is True
            assert video.status == "scheduled"

        # Bridge should have sent schedule (not another download)
        assert len(bridge_stub.schedule_calls) == 1
        assert len(bridge_stub.download_calls) == 0

    @pytest.mark.asyncio
    async def test_failure_marks_video_failed(
        self,
        seeded_scheduler: tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker],
    ) -> None:
        """video.download_complete with success=False sets video status to failed."""
        scheduler, bridge_stub, _, factory = seeded_scheduler

        async with factory() as session:
            video = Video(
                filename="reel_004.mp4",
                account_username="test_user",
                status="uploading",
                uploaded_to_phone=False,
            )
            session.add(video)
            await session.commit()
            vid_id = video.id

        await scheduler.handle_download_complete(
            device_id=1,
            payload={
                "videoId": vid_id,
                "success": False,
                "error": "HTTP 404: Not Found",
            },
        )

        async with factory() as session:
            result = await session.execute(select(Video).where(Video.id == vid_id))
            video = result.scalar_one()
            assert video.status == "failed"
            assert video.upload_error == "HTTP 404: Not Found"
            assert video.uploaded_to_phone is False

        # No schedule should have been sent
        assert len(bridge_stub.schedule_calls) == 0

    @pytest.mark.asyncio
    async def test_unknown_video_id_logs_warning(
        self,
        seeded_scheduler: tuple[FarmScheduler, _BridgeStub, _WsManagerStub, async_sessionmaker],
    ) -> None:
        """video.download_complete for a non-existent video is handled gracefully."""
        scheduler, bridge_stub, _, factory = seeded_scheduler

        # Should not raise
        await scheduler.handle_download_complete(
            device_id=1,
            payload={
                "videoId": 9999,
                "success": True,
            },
        )

        # Nothing should have been sent
        assert len(bridge_stub.schedule_calls) == 0
        assert len(bridge_stub.download_calls) == 0
