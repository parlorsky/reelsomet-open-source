"""Tests for server.api.proxy: proxy assignment commands."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from server.dependencies import get_bridge
from server.ws.bridge import DeviceBridge


def _make_bridge_mock() -> AsyncMock:
    """Return an AsyncMock shaped like DeviceBridge."""
    bridge = AsyncMock(spec=DeviceBridge)
    bridge.set_proxy = AsyncMock(return_value={"ok": True})
    bridge.clear_proxy = AsyncMock(return_value={"ok": True})
    return bridge


async def _register_fake_ws(app: Any, device_id: int) -> None:
    """Register a fake WS connection so the manager reports the device as online."""
    ws_manager = app.state.ws_manager
    fake_ws = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_ws.send_text = AsyncMock()
    await ws_manager.connect(device_id, fake_ws)


class TestProxyCommands:

    @pytest.mark.asyncio
    async def test_assign_proxy_rejects_inactive_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Soft-deleted devices cannot receive proxy assignments by stale websocket."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        delete_resp = await client.delete(f"/api/devices/{dev.id}", headers=auth_headers)
        assert delete_resp.status_code == 200

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/proxy/assign",
                json={"device_id": dev.id, "proxy_port": 9001},
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"
        bridge_mock.set_proxy.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_clear_proxy_rejects_inactive_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Soft-deleted devices cannot clear proxies by stale websocket."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        delete_resp = await client.delete(f"/api/devices/{dev.id}", headers=auth_headers)
        assert delete_resp.status_code == 200

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(f"/api/proxy/clear/{dev.id}", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Device not found"
        bridge_mock.clear_proxy.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)
