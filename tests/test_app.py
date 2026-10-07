"""Tests for server.app: FastAPI application factory and endpoints."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncEngine, async_sessionmaker

from server.app import _MAX_BODY_SIZE, _wire_device_events, create_app
from server.auth import create_jwt_token
from server.config import VPSConfig
from server.models import Account, Device
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType, WSMessage


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------

class TestAppCreation:

    def test_app_creates_successfully(self, test_config: VPSConfig) -> None:
        """create_app returns a FastAPI instance without errors."""
        app = create_app(config=test_config)
        assert app is not None
        assert app.title == "Reelsomet VPS"

    def test_app_creates_with_defaults(self) -> None:
        """create_app with no config uses VPSConfig defaults."""
        app = create_app()
        assert app.state.config.host == "127.0.0.1"
        assert app.state.config.port == 8000

    def test_app_stores_config_on_state(self, test_config: VPSConfig) -> None:
        """The config is accessible via app.state.config."""
        app = create_app(config=test_config)
        assert app.state.config is test_config
        assert app.state.config.jwt_secret == "test-jwt-secret-value-for-tests"


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_endpoint(self, test_client: AsyncClient) -> None:
        """GET /api/health returns 200 with only status field."""
        resp = await test_client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        # Health endpoint should NOT expose version or device count
        assert "version" not in body
        assert "devices_online" not in body

    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, test_client: AsyncClient) -> None:
        """Health endpoint does not require authentication."""
        # No Authorization header
        resp = await test_client.get("/api/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

class TestCORS:

    @pytest.mark.asyncio
    async def test_cors_headers_present(self, test_client: AsyncClient) -> None:
        """CORS preflight includes configured domain origins."""
        # test_config has domain="test.example.com", so CORS allows that origin
        resp = await test_client.options(
            "/api/health",
            headers={
                "Origin": "https://test.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers
        origin = resp.headers["access-control-allow-origin"]
        assert origin == "https://test.example.com"

    @pytest.mark.asyncio
    async def test_cors_rejects_unknown_origin(self, test_client: AsyncClient) -> None:
        """CORS does not allow requests from unknown origins."""
        resp = await test_client.options(
            "/api/health",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        # FastAPI/Starlette CORS returns 400 for disallowed origins
        origin = resp.headers.get("access-control-allow-origin")
        assert origin != "http://evil.com"

    @pytest.mark.asyncio
    async def test_cors_on_get(self, test_client: AsyncClient) -> None:
        """Regular GET responses include CORS headers for allowed origins."""
        resp = await test_client.get(
            "/api/health",
            headers={"Origin": "https://test.example.com"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") == "https://test.example.com"


# ---------------------------------------------------------------------------
# Upload size middleware
# ---------------------------------------------------------------------------

class TestUploadSizeMiddleware:

    @pytest.mark.asyncio
    async def test_invalid_content_length_returns_400(self, test_client: AsyncClient) -> None:
        """Malformed Content-Length headers should fail cleanly instead of crashing the app."""
        resp = await test_client.get("/api/health", headers={"Content-Length": "abc"})

        assert resp.status_code == 400
        assert resp.json() == {"detail": "Invalid Content-Length header"}

    @pytest.mark.asyncio
    async def test_oversized_content_length_returns_413(self, test_client: AsyncClient) -> None:
        """Oversized requests should still be rejected with the documented 413 response."""
        resp = await test_client.get(
            "/api/health",
            headers={"Content-Length": str(_MAX_BODY_SIZE + 1)},
        )

        assert resp.status_code == 413
        assert resp.json() == {"detail": "Request body too large (max 500 MB)"}


# ---------------------------------------------------------------------------
# Static files / SPA fallback
# ---------------------------------------------------------------------------

class TestStaticFiles:

    @pytest.mark.asyncio
    async def test_no_static_dir_still_works(self, test_client: AsyncClient) -> None:
        """Without a static directory, the app still serves API endpoints."""
        resp = await test_client.get("/api/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_static_assets_served_when_dir_exists(self, tmp_path: Path) -> None:
        """When static dir with assets/ exists, /assets/* routes serve files."""
        static_dir = tmp_path / "static"
        assets_dir = static_dir / "assets"
        assets_dir.mkdir(parents=True)
        js_file = assets_dir / "app.js"
        js_file.write_text("console.log('hello')")

        db_path = str(tmp_path / "data" / "test.db")
        config = VPSConfig(
            database_path=db_path,
            static_dir=str(static_dir),
            jwt_secret="test-secret",
        )
        app = create_app(config=config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/assets/app.js")
            assert resp.status_code == 200
            assert "hello" in resp.text

    @pytest.mark.asyncio
    async def test_static_file_served_directly(self, tmp_path: Path) -> None:
        """Assets directory files are served directly."""
        static_dir = tmp_path / "static"
        assets_dir = static_dir / "assets"
        assets_dir.mkdir(parents=True)
        (assets_dir / "style.css").write_text("body { color: red; }")

        db_path = str(tmp_path / "data" / "test.db")
        config = VPSConfig(
            database_path=db_path,
            static_dir=str(static_dir),
            jwt_secret="test-secret",
        )
        app = create_app(config=config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/assets/style.css")
            assert resp.status_code == 200
            assert "color: red" in resp.text


# ---------------------------------------------------------------------------
# Auth required on protected patterns (via ws_router / handler)
# ---------------------------------------------------------------------------

class TestAuthIntegration:

    @pytest.mark.asyncio
    async def test_api_docs_disabled_in_production(self, test_client: AsyncClient) -> None:
        """OpenAPI docs at /api/docs are disabled by default (production mode)."""
        resp = await test_client.get("/api/docs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_api_docs_accessible_in_debug(self, tmp_path: Path) -> None:
        """OpenAPI docs at /api/docs are accessible when REELSOMET_DEBUG=1."""
        import os
        old_val = os.environ.get("REELSOMET_DEBUG")
        os.environ["REELSOMET_DEBUG"] = "1"
        try:
            config = VPSConfig(
                database_path=str(tmp_path / "data" / "test.db"),
                static_dir=str(tmp_path / "static"),
                jwt_secret="test-secret",
            )
            app = create_app(config=config)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await client.get("/api/docs")
                assert resp.status_code == 200
        finally:
            if old_val is None:
                os.environ.pop("REELSOMET_DEBUG", None)
            else:
                os.environ["REELSOMET_DEBUG"] = old_val


class TestLiveProfileStats:

    @pytest.mark.asyncio
    async def test_profile_stats_event_broadcasts_account_update(
        self,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """Profile stat scrapes push an account:update event for the admin UI."""
        del seed_db  # fixture seeds the shared in-memory database
        session_factory = async_sessionmaker(
            db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        manager = DeviceConnectionManager()
        broadcaster = AsyncMock()
        broadcaster.broadcast = AsyncMock()

        _wire_device_events(
            manager,
            broadcaster,
            session_factory=session_factory,
        )

        await manager.handle_message(
            1,
            WSMessage(
                type=MessageType.EVENT_PROFILE_STATS,
                payload={
                    "username": "user_alpha",
                    "followers": 1234,
                    "following": 77,
                    "posts": 55,
                },
            ),
        )

        broadcaster.broadcast.assert_awaited_once_with(
            "account:update",
            {
                "username": "user_alpha",
                "followers": 1234,
                "following": 77,
                "posts_count": 55,
            },
        )

        async with session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()

        assert account.followers == 1234
        assert account.following == 77
        assert account.posts_count == 55


class TestLoginStatusRebroadcast:

    @pytest.mark.asyncio
    async def test_login_status_event_is_rebroadcast_to_admin(
        self,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """event.login_status from phone is forwarded to admin WS browsers.

        The AccountsPage Login tab subscribes to ``event.login_status`` and
        expects live progress from the on-phone LoginStateMachine. Without
        the rebroadcast branch in ``on_event`` the subscription never
        fires. See docs/T5-backend-followup.md for the gap this closes.
        """
        del seed_db  # fixture provides devices the manager will accept
        session_factory = async_sessionmaker(
            db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        manager = DeviceConnectionManager()
        broadcaster = AsyncMock()
        broadcaster.broadcast = AsyncMock()

        _wire_device_events(
            manager,
            broadcaster,
            session_factory=session_factory,
        )

        await manager.handle_message(
            1,
            WSMessage(
                type=MessageType.EVENT_LOGIN_STATUS,
                payload={
                    "username": "user_alpha",
                    "status": "success",
                },
            ),
        )

        broadcaster.broadcast.assert_any_await(
            "event.login_status",
            {
                "username": "user_alpha",
                "status": "success",
                "device_id": 1,
            },
        )

    @pytest.mark.asyncio
    async def test_login_status_event_forwards_failure_payload(
        self,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """Failure metadata (status=failed, error=...) is passed through untouched."""
        del seed_db
        session_factory = async_sessionmaker(
            db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        manager = DeviceConnectionManager()
        broadcaster = AsyncMock()
        broadcaster.broadcast = AsyncMock()

        _wire_device_events(
            manager,
            broadcaster,
            session_factory=session_factory,
        )

        await manager.handle_message(
            1,
            WSMessage(
                type=MessageType.EVENT_LOGIN_STATUS,
                payload={
                    "username": "user_alpha",
                    "status": "failed",
                    "error": "2FA code incorrect",
                },
            ),
        )

        broadcaster.broadcast.assert_any_await(
            "event.login_status",
            {
                "username": "user_alpha",
                "status": "failed",
                "error": "2FA code incorrect",
                "device_id": 1,
            },
        )


class TestInactiveDeviceLifecycle:

    @pytest.mark.asyncio
    async def test_inactive_device_connect_is_rejected(
        self,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """Deleted devices no longer register a live websocket connection."""
        dev_id = seed_db["devices"][0].id
        session_factory = async_sessionmaker(
            db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            device = await session.get(Device, dev_id)
            assert device is not None
            device.is_active = False
            await session.commit()

        manager = DeviceConnectionManager()
        broadcaster = AsyncMock()
        broadcaster.broadcast = AsyncMock()
        _wire_device_events(
            manager,
            broadcaster,
            session_factory=session_factory,
        )

        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        await manager.connect(dev_id, fake_ws)

        assert manager.is_online(dev_id) is False
        fake_ws.close.assert_awaited_once()
        broadcaster.broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inactive_device_hello_is_ignored(
        self,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """Deleted devices no longer update DB state or admin feeds from hello events."""
        dev_id = seed_db["devices"][0].id
        session_factory = async_sessionmaker(
            db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        async with session_factory() as session:
            device = await session.get(Device, dev_id)
            assert device is not None
            device.is_active = False
            device.status = "offline"
            device.device_model = "Original Model"
            device.android_version = "14"
            await session.commit()

        manager = DeviceConnectionManager()
        broadcaster = AsyncMock()
        broadcaster.broadcast = AsyncMock()
        _wire_device_events(
            manager,
            broadcaster,
            session_factory=session_factory,
        )

        await manager.handle_message(
            dev_id,
            WSMessage(
                type=MessageType.DEVICE_HELLO,
                payload={
                    "model": "New Model",
                    "androidVersion": "15",
                    "appVersion": "9.9.9",
                },
            ),
        )

        broadcaster.broadcast.assert_not_awaited()

        async with session_factory() as session:
            device = await session.get(Device, dev_id)
            assert device is not None
            assert device.is_active is False
            assert device.status == "offline"
            assert device.device_model == "Original Model"
            assert device.android_version == "14"


# ---------------------------------------------------------------------------
# APK download endpoint
# ---------------------------------------------------------------------------

class TestApkDownload:

    @pytest.mark.asyncio
    async def test_apk_download_endpoint(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """GET /api/apk/download returns the APK file when it exists."""
        # Create directory structure: data_dir/../app/apk/reelsomet.apk
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        apk_dir = tmp_path / "app" / "apk"
        apk_dir.mkdir(parents=True)
        apk_file = apk_dir / "reelsomet.apk"
        apk_content = b"FAKE_APK_CONTENT_FOR_TEST"
        apk_file.write_bytes(apk_content)

        config = VPSConfig(
            database_path=str(data_dir / "test.db"),
            data_dir=str(data_dir),
            static_dir=str(tmp_path / "static"),
            jwt_secret="test-secret",
        )
        monkeypatch.setattr("server.app.__file__", str(tmp_path / "app" / "server" / "app.py"))
        app = create_app(config=config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/apk/download")
            assert resp.status_code == 200
            assert resp.content == apk_content
            assert "application/vnd.android.package-archive" in resp.headers.get("content-type", "")

    @pytest.mark.asyncio
    async def test_apk_download_not_found(self, tmp_path: Path) -> None:
        """GET /api/apk/download returns 404 when the APK file does not exist."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config = VPSConfig(
            database_path=str(data_dir / "test.db"),
            data_dir=str(data_dir),
            static_dir=str(tmp_path / "static"),
            jwt_secret="test-secret",
        )
        app = create_app(config=config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get("/api/apk/download")
            assert resp.status_code == 404
