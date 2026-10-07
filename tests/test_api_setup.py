"""Tests for server.api.setup: first-run setup wizard API."""
from __future__ import annotations

from typing import Any, AsyncGenerator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.app import create_app
from server.auth import hash_password, verify_password
from server.config import VPSConfig

from tests.conftest import TEST_JWT_SECRET


# ---------------------------------------------------------------------------
# Fixtures: unconfigured app (no admin password)
# ---------------------------------------------------------------------------

@pytest.fixture
def unconfigured_config(tmp_path: Path) -> VPSConfig:
    """VPSConfig with NO admin password set (needs setup)."""
    db_path = str(tmp_path / "data" / "test-farm.db")
    config_path = tmp_path / "config.yaml"
    # Create an empty config file so save_config() has a place to write
    config_path.touch()
    return VPSConfig(
        host="127.0.0.1",
        port=9999,
        database_path=db_path,
        data_dir=str(tmp_path / "data"),
        static_dir=str(tmp_path / "static"),
        admin_password_hash="",  # <-- not configured
        jwt_secret=TEST_JWT_SECRET,
        jwt_expire_hours=2,
        setup_token="setup-token-test",
        _config_path=str(config_path),
    )


@pytest.fixture
def configured_config(tmp_path: Path) -> VPSConfig:
    """VPSConfig WITH an admin password already set (setup complete)."""
    db_path = str(tmp_path / "data" / "test-farm.db")
    config_path = tmp_path / "config.yaml"
    config_path.touch()
    return VPSConfig(
        host="127.0.0.1",
        port=9999,
        database_path=db_path,
        data_dir=str(tmp_path / "data"),
        static_dir=str(tmp_path / "static"),
        admin_password_hash=hash_password("existing-password"),
        jwt_secret=TEST_JWT_SECRET,
        jwt_expire_hours=2,
        telegram_bot_token="123456:ABC",
        telegram_admin_chat_ids=[999],
        device_tokens={1: "existing-token"},
        _config_path=str(config_path),
    )


@pytest_asyncio.fixture
async def unconfigured_client(
    unconfigured_config: VPSConfig,
    db_engine: AsyncEngine,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client for an app that has NOT completed setup."""
    app = create_app(config=unconfigured_config)
    app.state.db_engine = db_engine
    app.state.db_session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture
async def configured_client(
    configured_config: VPSConfig,
    db_engine: AsyncEngine,
) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client for an app that HAS already completed setup."""
    app = create_app(config=configured_config)
    app.state.db_engine = db_engine
    app.state.db_session_factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/setup/status
# ---------------------------------------------------------------------------

class TestSetupStatus:

    @pytest.mark.asyncio
    async def test_setup_status_needs_setup(
        self, unconfigured_client: AsyncClient,
    ) -> None:
        """When no admin password is set, needs_setup is true."""
        resp = await unconfigured_client.get("/api/setup/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["needs_setup"] is True
        assert body["has_password"] is False
        assert body["has_telegram"] is False
        assert body["device_count"] == 0

    @pytest.mark.asyncio
    async def test_setup_status_already_configured(
        self, configured_client: AsyncClient,
    ) -> None:
        """When admin password exists, needs_setup is false."""
        resp = await configured_client.get("/api/setup/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["needs_setup"] is False
        assert body["has_password"] is True
        assert body["has_telegram"] is True
        assert body["device_count"] == 1


# ---------------------------------------------------------------------------
# POST /api/setup/complete
# ---------------------------------------------------------------------------

class TestSetupComplete:

    @pytest.mark.asyncio
    async def test_setup_complete(
        self, unconfigured_client: AsyncClient, unconfigured_config: VPSConfig,
    ) -> None:
        """Successful first-run setup sets password and returns tokens."""
        resp = await unconfigured_client.post(
            "/api/setup/complete",
            json={"admin_password": "my-secure-password"},
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert len(body["device_token"]) > 20
        assert len(body["access_token"]) > 20

        # Verify password was actually hashed and stored
        assert unconfigured_config.admin_password_hash != ""
        assert verify_password("my-secure-password", unconfigured_config.admin_password_hash)

        # Verify device token was stored
        assert 1 in unconfigured_config.device_tokens
        assert unconfigured_config.device_tokens[1] == body["device_token"]
        assert unconfigured_config.setup_token == ""

    @pytest.mark.asyncio
    async def test_setup_complete_already_configured(
        self, configured_client: AsyncClient,
    ) -> None:
        """Setup returns 409 if admin password is already set."""
        resp = await configured_client.post(
            "/api/setup/complete",
            json={"admin_password": "new-password-attempt"},
        )
        assert resp.status_code == 409
        assert "already" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_setup_complete_missing_password(
        self, unconfigured_client: AsyncClient, unconfigured_config: VPSConfig,
    ) -> None:
        """Setup returns 422 if admin_password is missing from body."""
        resp = await unconfigured_client.post(
            "/api/setup/complete",
            json={},
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_setup_complete_short_password(
        self, unconfigured_client: AsyncClient, unconfigured_config: VPSConfig,
    ) -> None:
        """Setup returns 422 if password is shorter than 6 characters."""
        resp = await unconfigured_client.post(
            "/api/setup/complete",
            json={"admin_password": "abc"},
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_setup_includes_telegram(
        self, unconfigured_client: AsyncClient, unconfigured_config: VPSConfig,
    ) -> None:
        """Setup saves optional Telegram config when provided."""
        resp = await unconfigured_client.post(
            "/api/setup/complete",
            json={
                "admin_password": "telegram-test-pw",
                "telegram_token": "99999:XYZABC",
                "telegram_chat_id": 12345678,
            },
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

        # Verify telegram config was stored
        assert unconfigured_config.telegram_bot_token == "99999:XYZABC"
        assert unconfigured_config.telegram_admin_chat_ids == [12345678]

    @pytest.mark.asyncio
    async def test_setup_prevents_re_setup(
        self, unconfigured_client: AsyncClient, unconfigured_config: VPSConfig,
    ) -> None:
        """After completing setup once, a second attempt returns 409."""
        # First setup
        resp1 = await unconfigured_client.post(
            "/api/setup/complete",
            json={"admin_password": "first-setup-pw"},
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp1.status_code == 200

        # Second attempt
        resp2 = await unconfigured_client.post(
            "/api/setup/complete",
            json={"admin_password": "second-attempt"},
            headers={"X-Setup-Token": unconfigured_config.setup_token},
        )
        assert resp2.status_code == 409

    @pytest.mark.asyncio
    async def test_setup_complete_requires_setup_token(
        self, unconfigured_client: AsyncClient,
    ) -> None:
        """Setup completion is rejected without the one-time setup token."""
        resp = await unconfigured_client.post(
            "/api/setup/complete",
            json={"admin_password": "first-setup-pw"},
        )
        assert resp.status_code == 403
        assert "setup token" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Login redirect hint (auth_router integration)
# ---------------------------------------------------------------------------

class TestLoginSetupHint:

    @pytest.mark.asyncio
    async def test_login_returns_needs_setup_when_unconfigured(
        self, unconfigured_client: AsyncClient,
    ) -> None:
        """Login endpoint returns needs_setup hint when no password is set."""
        resp = await unconfigured_client.post(
            "/api/auth/login",
            json={"password": "anything"},
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "not_configured"
        assert body["needs_setup"] is True
