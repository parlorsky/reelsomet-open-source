"""Tests for admin WebSocket endpoint and broadcaster."""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import create_app
from server.auth import create_jwt_token
from server.config import VPSConfig
from server.log_handler import WebSocketLogHandler
from server.ws.admin_broadcaster import AdminBroadcaster
from tests.conftest import TEST_JWT_SECRET


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_token() -> str:
    """Create a valid JWT token for admin WS auth."""
    return create_jwt_token(
        {"sub": "admin", "role": "admin"},
        TEST_JWT_SECRET,
        expires_hours=2,
    )


def _make_expired_token() -> str:
    """Create an expired JWT token."""
    return create_jwt_token(
        {"sub": "admin", "role": "admin"},
        TEST_JWT_SECRET,
        expires_hours=-1,  # already expired
    )


def _make_mock_ws() -> AsyncMock:
    """Create a mock WebSocket for unit-testing the broadcaster."""
    ws = AsyncMock()
    ws.send_text = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"
    return ws


# ---------------------------------------------------------------------------
# AdminBroadcaster unit tests
# ---------------------------------------------------------------------------

class TestAdminBroadcaster:

    @pytest.mark.asyncio
    async def test_connect_adds_to_set(self) -> None:
        broadcaster = AdminBroadcaster()
        ws = _make_mock_ws()
        await broadcaster.connect(ws)
        assert broadcaster.connection_count == 1

    @pytest.mark.asyncio
    async def test_disconnect_removes_from_set(self) -> None:
        broadcaster = AdminBroadcaster()
        ws = _make_mock_ws()
        await broadcaster.connect(ws)
        await broadcaster.disconnect(ws)
        assert broadcaster.connection_count == 0

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_is_safe(self) -> None:
        broadcaster = AdminBroadcaster()
        ws = _make_mock_ws()
        await broadcaster.disconnect(ws)  # should not raise
        assert broadcaster.connection_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self) -> None:
        broadcaster = AdminBroadcaster()
        ws1 = _make_mock_ws()
        ws2 = _make_mock_ws()
        ws3 = _make_mock_ws()
        await broadcaster.connect(ws1)
        await broadcaster.connect(ws2)
        await broadcaster.connect(ws3)

        await broadcaster.broadcast("device:status", {"device_id": 1, "battery": 80})

        for ws in [ws1, ws2, ws3]:
            ws.send_text.assert_awaited_once()
            parsed = json.loads(ws.send_text.call_args[0][0])
            assert parsed["type"] == "device:status"
            assert parsed["data"]["device_id"] == 1
            assert parsed["data"]["battery"] == 80
            assert "ts" in parsed

    @pytest.mark.asyncio
    async def test_broadcast_to_empty_set_is_noop(self) -> None:
        broadcaster = AdminBroadcaster()
        # Should not raise
        await broadcaster.broadcast("device:status", {"device_id": 1})

    @pytest.mark.asyncio
    async def test_broadcast_survives_dead_client(self) -> None:
        """One broken connection does not prevent delivery to others."""
        broadcaster = AdminBroadcaster()
        ws_good = _make_mock_ws()
        ws_dead = _make_mock_ws()
        ws_dead.send_text = AsyncMock(side_effect=RuntimeError("connection lost"))
        ws_also_good = _make_mock_ws()

        await broadcaster.connect(ws_good)
        await broadcaster.connect(ws_dead)
        await broadcaster.connect(ws_also_good)

        await broadcaster.broadcast("toast", {"message": "hello"})

        # Good clients received the message
        ws_good.send_text.assert_awaited_once()
        ws_also_good.send_text.assert_awaited_once()

        # Dead client was removed
        assert broadcaster.connection_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_clients(self) -> None:
        """All connected browsers receive the event with correct data."""
        broadcaster = AdminBroadcaster()
        clients = [_make_mock_ws() for _ in range(5)]
        for ws in clients:
            await broadcaster.connect(ws)

        await broadcaster.broadcast("queue:update", {"video_id": 42, "status": "posted"})

        for ws in clients:
            ws.send_text.assert_awaited_once()
            parsed = json.loads(ws.send_text.call_args[0][0])
            assert parsed["type"] == "queue:update"
            assert parsed["data"]["video_id"] == 42
            assert parsed["data"]["status"] == "posted"

    @pytest.mark.asyncio
    async def test_disconnect_cleanup(self) -> None:
        """Disconnected client is properly removed from the connection set."""
        broadcaster = AdminBroadcaster()
        ws1 = _make_mock_ws()
        ws2 = _make_mock_ws()
        await broadcaster.connect(ws1)
        await broadcaster.connect(ws2)
        assert broadcaster.connection_count == 2

        await broadcaster.disconnect(ws1)
        assert broadcaster.connection_count == 1

        # Only ws2 should receive the broadcast
        await broadcaster.broadcast("toast", {"message": "test"})
        ws1.send_text.assert_not_awaited()
        ws2.send_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration tests using Starlette TestClient for WebSocket
# ---------------------------------------------------------------------------

class TestAdminWSEndpoint:

    @pytest.fixture
    def test_config(self, tmp_path) -> VPSConfig:
        db_path = str(tmp_path / "data" / "test-farm.db")
        return VPSConfig(
            host="127.0.0.1",
            port=9999,
            database_path=db_path,
            data_dir=str(tmp_path / "data"),
            static_dir=str(tmp_path / "static"),
            jwt_secret=TEST_JWT_SECRET,
            jwt_expire_hours=2,
        )

    @pytest.fixture
    def app(self, test_config):
        return create_app(config=test_config)

    def test_admin_ws_connect_with_valid_token(self, app) -> None:
        """A browser with a valid JWT can connect to /ws/admin."""
        token = _make_valid_token()
        client = TestClient(app)
        with client.websocket_connect(f"/ws/admin?token={token}") as ws:
            # Connection accepted — broadcaster should have 1 connection
            broadcaster: AdminBroadcaster = app.state.admin_broadcaster
            assert broadcaster.connection_count == 1
        # After disconnect, count should be 0 (eventually)

    def test_admin_ws_reject_invalid_token(self, app) -> None:
        """A browser with an invalid JWT is rejected with policy violation."""
        client = TestClient(app)
        with pytest.raises(Exception):
            # Starlette raises on rejected WS (close code 1008)
            with client.websocket_connect("/ws/admin?token=invalid-garbage-token"):
                pass

    def test_admin_ws_reject_expired_token(self, app) -> None:
        """A browser with an expired JWT is rejected."""
        token = _make_expired_token()
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/admin?token={token}"):
                pass

    def test_admin_ws_reject_no_token(self, app) -> None:
        """A browser without a token query param is rejected."""
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/admin"):
                pass

    def test_broadcast_device_status(self, app) -> None:
        """An admin browser receives device status broadcasts."""
        token = _make_valid_token()
        broadcaster: AdminBroadcaster = app.state.admin_broadcaster

        client = TestClient(app)
        with client.websocket_connect(f"/ws/admin?token={token}") as ws:
            # Use a background thread to broadcast since TestClient WS is synchronous
            import threading

            def do_broadcast():
                import asyncio as aio
                loop = aio.new_event_loop()
                loop.run_until_complete(broadcaster.broadcast(
                    "device:status",
                    {"device_id": 1, "is_online": True, "battery": 85},
                ))
                loop.close()

            t = threading.Thread(target=do_broadcast)
            t.start()
            t.join(timeout=5)

            data = ws.receive_json()
            assert data["type"] == "device:status"
            assert data["data"]["device_id"] == 1
            assert data["data"]["is_online"] is True
            assert data["data"]["battery"] == 85


# ---------------------------------------------------------------------------
# WebSocketLogHandler tests
# ---------------------------------------------------------------------------

class TestWebSocketLogHandler:

    @pytest.mark.asyncio
    async def test_log_handler_forwards_entries(self) -> None:
        """Log entries are broadcast as log:entry events to admin browsers."""
        broadcaster = AdminBroadcaster()
        ws = _make_mock_ws()
        await broadcaster.connect(ws)

        handler = WebSocketLogHandler(broadcaster, level=logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))

        test_logger = logging.getLogger("test.admin_ws")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            test_logger.info("Test log message for admin WS")
            # The handler schedules a task — we need to let it run
            await asyncio.sleep(0.05)

            ws.send_text.assert_awaited()
            sent_raw = ws.send_text.call_args[0][0]
            parsed = json.loads(sent_raw)
            assert parsed["type"] == "log:entry"
            assert "Test log message for admin WS" in parsed["data"]["message"]
            assert parsed["data"]["level"] == "INFO"
            assert parsed["data"]["logger"] == "test.admin_ws"
        finally:
            test_logger.removeHandler(handler)

    @pytest.mark.asyncio
    async def test_log_handler_skips_when_no_connections(self) -> None:
        """Handler does nothing when no admin browsers are connected."""
        broadcaster = AdminBroadcaster()
        assert broadcaster.connection_count == 0

        handler = WebSocketLogHandler(broadcaster)
        handler.setFormatter(logging.Formatter("%(message)s"))

        test_logger = logging.getLogger("test.admin_ws.skip")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            # Should not raise even with no connections
            test_logger.info("This goes nowhere")
            await asyncio.sleep(0.05)
        finally:
            test_logger.removeHandler(handler)

    @pytest.mark.asyncio
    async def test_log_handler_below_level_skipped(self) -> None:
        """Records below the handler's level are not forwarded."""
        broadcaster = AdminBroadcaster()
        ws = _make_mock_ws()
        await broadcaster.connect(ws)

        handler = WebSocketLogHandler(broadcaster, level=logging.WARNING)
        handler.setFormatter(logging.Formatter("%(message)s"))

        test_logger = logging.getLogger("test.admin_ws.level")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.DEBUG)

        try:
            test_logger.info("This should be skipped")
            await asyncio.sleep(0.05)
            ws.send_text.assert_not_awaited()

            test_logger.warning("This should be forwarded")
            await asyncio.sleep(0.05)
            ws.send_text.assert_awaited()
        finally:
            test_logger.removeHandler(handler)
