"""Tests for server.api.ig_login: login trigger routes."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.dependencies import get_bridge
from server.models import Account, AccountDevice, Device
from server.ws.bridge import DeviceBridge


def _make_bridge_mock() -> AsyncMock:
    """Return an AsyncMock shaped like DeviceBridge."""
    bridge = AsyncMock(spec=DeviceBridge)
    bridge.instagram_login = AsyncMock(return_value={"ok": True})
    return bridge


async def _register_fake_ws(app: Any, device_id: int) -> None:
    """Register a fake WS connection so the manager reports the device as online."""
    ws_manager = app.state.ws_manager
    fake_ws = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_ws.send_text = AsyncMock()
    await ws_manager.connect(device_id, fake_ws)


class TestInstagramLogin:

    @pytest.mark.asyncio
    async def test_bulk_import_skips_inactive_device_assignment(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Bulk import does not create accounts against inactive devices."""
        dev = seed_db["devices"][0]

        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, dev.id)
            assert device is not None
            device.is_active = False
            await session.commit()

        resp = await client.post(
            "/api/ig/import",
            json={
                "accounts": [
                    {
                        "username": "imported_user",
                        "password": "secret-pass",
                    },
                ],
                "device_id": dev.id,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["created"] == 0
        assert body["updated"] == 0
        assert body["total"] == 1
        assert body["errors"] == ["imported_user: Device not found"]

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "imported_user"),
            )).scalar_one_or_none()
        assert account is None

    @pytest.mark.asyncio
    async def test_bulk_import_reassigns_existing_account_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Bulk import updates an existing account's primary device assignment."""
        target_device = seed_db["devices"][1]

        resp = await client.post(
            "/api/ig/import",
            json={
                "accounts": [
                    {
                        "username": "user_alpha",
                        "password": "updated-pass",
                        "device_id": target_device.id,
                    },
                ],
            },
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["created"] == 0
        assert body["updated"] == 1
        assert body["errors"] == []
        assert body["total"] == 1

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            links = (await session.execute(
                select(AccountDevice).where(AccountDevice.account_username == "user_alpha"),
            )).scalars().all()

        assert account.ig_password == "updated-pass"
        assert len(links) == 1
        assert links[0].device_id == target_device.id
        assert links[0].is_primary is True

    @pytest.mark.asyncio
    async def test_trigger_login_rejects_inactive_device_link(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Single-account login ignores stale links to inactive devices."""
        account_id = seed_db["accounts"][0].id
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = await session.get(Account, account_id)
            device = await session.get(Device, dev.id)
            assert account is not None
            assert device is not None
            account.ig_password = "secret-pass"
            device.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(f"/api/ig/login/{account_id}", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Account not linked to any device"
        bridge_mock.instagram_login.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_trigger_login_all_skips_inactive_device_link(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Bulk login skips accounts whose only linked device is inactive."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = await session.get(Account, seed_db["accounts"][0].id)
            device = await session.get(Device, dev.id)
            assert account is not None
            assert device is not None
            account.ig_password = "secret-pass"
            device.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post("/api/ig/login-all", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"] == [
            {
                "username": "user_alpha",
                "status": "skipped",
                "reason": "device offline",
            },
        ]
        bridge_mock.instagram_login.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)
