"""Tests for server.api.system — system management API."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from server.auth import create_jwt_token
from tests.conftest import TEST_JWT_SECRET


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_headers() -> dict[str, str]:
    token = create_jwt_token(
        {"sub": "admin", "role": "admin"},
        TEST_JWT_SECRET,
        expires_hours=2,
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /api/system/info
# ---------------------------------------------------------------------------


class TestSystemInfo:
    @pytest.mark.asyncio
    async def test_system_info(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Authenticated request returns version, uptime, python."""
        resp = await client.get("/api/system/info", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "uptime_seconds" in data
        assert "python_version" in data
        assert "platform" in data
        assert isinstance(data["uptime_seconds"], int)

    @pytest.mark.asyncio
    async def test_system_info_unauthorized(self, client: AsyncClient) -> None:
        """Request without auth returns 401."""
        resp = await client.get("/api/system/info")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/system/update/check
# ---------------------------------------------------------------------------


class TestUpdateCheck:
    @pytest.mark.asyncio
    async def test_update_check_no_updates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """When no updates available, returns available=False."""
        mock_result = {"available": False, "commits": 0, "summary": "Already up to date"}
        with patch("server.api.system.check_for_updates", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.get("/api/system/update/check", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["commits"] == 0

    @pytest.mark.asyncio
    async def test_update_check_with_updates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """When updates available, returns available=True with commit summary."""
        mock_result = {
            "available": True,
            "commits": 3,
            "summary": "abc1234 Fix bug\ndef5678 Add feature",
        }
        with patch("server.api.system.check_for_updates", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.get("/api/system/update/check", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["commits"] == 3
        assert "Fix bug" in data["summary"]

    @pytest.mark.asyncio
    async def test_update_check_unauthorized(self, client: AsyncClient) -> None:
        """Request without auth returns 401."""
        resp = await client.get("/api/system/update/check")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/system/update/apply
# ---------------------------------------------------------------------------


class TestUpdateApply:
    @pytest.mark.asyncio
    async def test_update_apply_success(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Successful update returns success=True."""
        mock_result = {
            "success": True,
            "message": "Update applied: git pull, pip install, restart scheduled",
            "restart_scheduled": True,
        }
        with patch("server.api.system.apply_update", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post("/api/system/update/apply", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert "git pull" in data["message"]

    @pytest.mark.asyncio
    async def test_update_apply_unauthorized(self, client: AsyncClient) -> None:
        """Request without auth returns 401."""
        resp = await client.post("/api/system/update/apply")
        assert resp.status_code == 401
