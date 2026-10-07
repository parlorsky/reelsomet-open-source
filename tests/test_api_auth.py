"""Tests for server.api.auth_router: login, /me endpoint."""
from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from httpx import AsyncClient

from server.auth import create_jwt_token

from tests.conftest import TEST_ADMIN_PASSWORD, TEST_JWT_SECRET


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

class TestLogin:

    @pytest.mark.asyncio
    async def test_login_success(self, client: AsyncClient) -> None:
        """Valid admin password returns a JWT token."""
        resp = await client.post(
            "/api/auth/login",
            json={"password": TEST_ADMIN_PASSWORD},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert len(body["access_token"]) > 20  # JWT is a real token

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, client: AsyncClient) -> None:
        """Wrong password returns 401."""
        resp = await client.post(
            "/api/auth/login",
            json={"password": "wrong-password-99"},
        )
        assert resp.status_code == 401
        assert "Invalid password" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_login_no_body(self, client: AsyncClient) -> None:
        """Missing request body returns 422 (validation error)."""
        resp = await client.post("/api/auth/login")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /me
# ---------------------------------------------------------------------------

class TestMe:

    @pytest.mark.asyncio
    async def test_me_with_valid_token(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/auth/me with a valid token returns user info."""
        resp = await client.get("/api/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["sub"] == "admin"
        assert body["role"] == "admin"

    @pytest.mark.asyncio
    async def test_me_without_token(self, client: AsyncClient) -> None:
        """GET /api/auth/me without Authorization header returns 401."""
        resp = await client.get("/api/auth/me")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_me_with_expired_token(self, client: AsyncClient) -> None:
        """GET /api/auth/me with an expired token returns 401."""
        payload = {
            "sub": "admin",
            "role": "admin",
            "exp": int(time.time()) - 3600,  # expired 1 hour ago
            "iat": int(time.time()) - 7200,
        }
        expired_token = pyjwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_me_with_invalid_token(self, client: AsyncClient) -> None:
        """GET /api/auth/me with a garbage token returns 401."""
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer totally.invalid.token"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_me_with_wrong_secret_token(self, client: AsyncClient) -> None:
        """Token signed with wrong secret is rejected."""
        token = create_jwt_token({"sub": "admin", "role": "admin"}, "wrong-secret")
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
