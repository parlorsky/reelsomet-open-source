"""Tests for server.ws.handler: device WebSocket endpoint logic."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from server.config import VPSConfig
from server.licensing import LicenseInfo
from server.models import Device
from server.ws.manager import DeviceConnectionManager
from tests.conftest import TEST_JWT_SECRET, make_mock_websocket


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_license_info(max_devices: int = 2, is_expired: bool = False) -> LicenseInfo:
    """Create a LicenseInfo with the given max_devices."""
    return LicenseInfo(
        hwid="AABBCCDDEEFF",
        expires="2099-12-31",
        max_devices=max_devices,
        tier="pro",
        is_expired=is_expired,
        days_remaining=999,
    )


def _make_ws_with_app(
    config: VPSConfig,
    manager: DeviceConnectionManager,
    *,
    session_factory: Any = None,
) -> AsyncMock:
    """Create a mock WebSocket whose .app.state holds config and ws_manager."""
    ws = make_mock_websocket()
    app_state = SimpleNamespace(
        config=config,
        ws_manager=manager,
    )
    if session_factory is not None:
        setattr(app_state, "db_session_factory", session_factory)
    ws.app = MagicMock()
    ws.app.state = app_state
    return ws


# ---------------------------------------------------------------------------
# Max-devices enforcement
# ---------------------------------------------------------------------------

class TestWsMaxDevices:

    @pytest.mark.asyncio
    @patch("server.ws.handler.get_license_info")
    @patch("server.ws.handler.is_licensed", return_value=True)
    @patch("server.ws.handler.verify_device_token")
    async def test_ws_rejects_over_max_devices(
        self,
        mock_verify: MagicMock,
        mock_is_licensed: MagicMock,
        mock_get_info: MagicMock,
    ) -> None:
        """When max_devices=1 and 1 device is already online, a new device is rejected."""
        from server.ws.handler import device_websocket

        config = VPSConfig(
            jwt_secret=TEST_JWT_SECRET,
            device_tokens={1: "token-dev-1", 2: "token-dev-2"},
        )
        manager = DeviceConnectionManager()

        # Connect device 1 first
        ws1 = make_mock_websocket()
        await manager.connect(1, ws1)
        assert manager.get_online_device_ids() == [1]

        # License allows only 1 device
        mock_get_info.return_value = _make_license_info(max_devices=1)
        mock_verify.return_value = 2  # token is valid for device 2

        ws2 = _make_ws_with_app(config, manager)

        # Set up receive_text to raise immediately (we won't reach the loop)
        ws2.receive_text.side_effect = Exception("should not reach message loop")

        await device_websocket(ws2, token="token-dev-2", device_id=2)

        # Device 2 should have been closed with policy violation
        ws2.close.assert_called_once()
        close_code = ws2.close.call_args[1].get("code") or ws2.close.call_args[0][0]
        assert close_code == 1008  # WS_1008_POLICY_VIOLATION

        # Device 2 should NOT have been accepted or connected
        ws2.accept.assert_not_called()
        assert 2 not in manager.get_online_device_ids()

    @pytest.mark.asyncio
    @patch("server.ws.handler.get_license_info")
    @patch("server.ws.handler.is_licensed", return_value=True)
    @patch("server.ws.handler.verify_device_token")
    async def test_ws_allows_reconnect_within_limit(
        self,
        mock_verify: MagicMock,
        mock_is_licensed: MagicMock,
        mock_get_info: MagicMock,
    ) -> None:
        """A device that is already online can reconnect even at max_devices limit."""
        from starlette.websockets import WebSocketDisconnect

        from server.ws.handler import device_websocket

        config = VPSConfig(
            jwt_secret=TEST_JWT_SECRET,
            device_tokens={1: "token-dev-1"},
        )
        manager = DeviceConnectionManager()

        # Device 1 is already connected
        ws1_old = make_mock_websocket()
        await manager.connect(1, ws1_old)

        mock_get_info.return_value = _make_license_info(max_devices=1)
        mock_verify.return_value = 1  # same device reconnecting

        ws1_new = _make_ws_with_app(config, manager)
        # Simulate immediate disconnect after accept
        ws1_new.receive_text.side_effect = WebSocketDisconnect(code=1000)

        await device_websocket(ws1_new, token="token-dev-1", device_id=1)

        # Should have been accepted (reconnect is allowed)
        ws1_new.accept.assert_called_once()

    @pytest.mark.asyncio
    @patch("server.ws.handler.get_license_info")
    @patch("server.ws.handler.is_licensed", return_value=True)
    @patch("server.ws.handler.verify_device_token")
    async def test_ws_allows_under_limit(
        self,
        mock_verify: MagicMock,
        mock_is_licensed: MagicMock,
        mock_get_info: MagicMock,
    ) -> None:
        """A new device is accepted when under max_devices limit."""
        from starlette.websockets import WebSocketDisconnect

        from server.ws.handler import device_websocket

        config = VPSConfig(
            jwt_secret=TEST_JWT_SECRET,
            device_tokens={1: "token-dev-1", 2: "token-dev-2"},
        )
        manager = DeviceConnectionManager()

        # No devices connected yet, limit is 2
        mock_get_info.return_value = _make_license_info(max_devices=2)
        mock_verify.return_value = 1

        ws1 = _make_ws_with_app(config, manager)
        ws1.receive_text.side_effect = WebSocketDisconnect(code=1000)

        await device_websocket(ws1, token="token-dev-1", device_id=1)

        # Should have been accepted
        ws1.accept.assert_called_once()

    @pytest.mark.asyncio
    @patch("server.ws.handler.get_license_info")
    @patch("server.ws.handler.is_licensed", return_value=True)
    @patch("server.ws.handler.verify_device_token")
    async def test_ws_disconnect_is_scoped_to_current_socket(
        self,
        mock_verify: MagicMock,
        mock_is_licensed: MagicMock,
        mock_get_info: MagicMock,
    ) -> None:
        """The endpoint passes its socket to disconnect so stale closes cannot drop a reconnect."""
        from starlette.websockets import WebSocketDisconnect

        from server.ws.handler import device_websocket

        config = VPSConfig(
            jwt_secret=TEST_JWT_SECRET,
            device_tokens={1: "token-dev-1"},
        )
        manager = MagicMock(spec=DeviceConnectionManager)
        manager.get_online_device_ids.return_value = []
        manager.connect = AsyncMock()
        manager.handle_message = AsyncMock()
        manager.disconnect = AsyncMock(return_value=True)

        mock_get_info.return_value = _make_license_info(max_devices=2)
        mock_verify.return_value = 1

        ws = _make_ws_with_app(config, manager)
        ws.receive_text.side_effect = WebSocketDisconnect(code=1000)

        await device_websocket(ws, token="token-dev-1", device_id=1)

        ws.accept.assert_called_once()
        manager.connect.assert_awaited_once_with(1, ws)
        manager.disconnect.assert_awaited_once_with(1, ws)


class TestInactiveDeviceHandshake:

    @pytest.mark.asyncio
    @patch("server.ws.handler.is_licensed", return_value=True)
    @patch("server.ws.handler.verify_device_token")
    async def test_ws_rejects_inactive_device_before_accept(
        self,
        mock_verify: MagicMock,
        mock_is_licensed: MagicMock,
        db_engine: AsyncEngine,
        seed_db: dict[str, Any],
    ) -> None:
        """Soft-deleted devices are rejected at the WS handshake entry point."""
        from server.ws.handler import device_websocket

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

        config = VPSConfig(
            jwt_secret=TEST_JWT_SECRET,
            device_tokens={dev_id: "token-dev"},
        )
        manager = DeviceConnectionManager()
        mock_verify.return_value = dev_id

        ws = _make_ws_with_app(
            config,
            manager,
            session_factory=session_factory,
        )
        ws.receive_text.side_effect = Exception("should not reach message loop")

        await device_websocket(ws, token="token-dev", device_id=dev_id)

        ws.accept.assert_not_called()
        ws.close.assert_called_once()
        close_code = ws.close.call_args[1].get("code") or ws.close.call_args[0][0]
        assert close_code == 1008
        assert manager.is_online(dev_id) is False
