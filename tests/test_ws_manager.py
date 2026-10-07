"""Tests for server.ws.manager: DeviceConnectionManager."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import (
    MessageType,
    WSMessage,
    create_message,
    serialize,
)

# Re-usable helper
from tests.conftest import make_mock_websocket


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

class TestConnectionLifecycle:

    @pytest.mark.asyncio
    async def test_connect_stores_websocket(self) -> None:
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)
        assert mgr.is_online(1)
        assert mgr.get_online_device_ids() == [1]

    @pytest.mark.asyncio
    async def test_disconnect_removes_websocket(self) -> None:
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)
        await mgr.disconnect(1)
        assert not mgr.is_online(1)
        assert mgr.get_online_device_ids() == []

    @pytest.mark.asyncio
    async def test_reconnect_closes_old_socket(self) -> None:
        """Connecting the same device_id replaces and closes the old WebSocket."""
        mgr = DeviceConnectionManager()
        ws_old = make_mock_websocket()
        ws_new = make_mock_websocket()
        await mgr.connect(1, ws_old)
        await mgr.connect(1, ws_new)
        # Old socket should have been closed
        ws_old.close.assert_awaited_once()
        # New socket is the active one
        assert mgr.is_online(1)

    @pytest.mark.asyncio
    async def test_disconnect_nonexistent_is_safe(self) -> None:
        """Disconnecting an unknown device_id does not raise."""
        mgr = DeviceConnectionManager()
        assert await mgr.disconnect(999) is False

    @pytest.mark.asyncio
    async def test_stale_disconnect_does_not_remove_new_socket(self) -> None:
        """A closing old socket must not remove a newer reconnect."""
        mgr = DeviceConnectionManager()
        ws_old = make_mock_websocket()
        ws_new = make_mock_websocket()
        await mgr.connect(1, ws_old)
        await mgr.connect(1, ws_new)

        disconnected = await mgr.disconnect(1, ws_old)

        assert disconnected is False
        assert mgr.is_online(1) is True
        assert mgr.get_online_device_ids() == [1]


# ---------------------------------------------------------------------------
# is_online / get_online_device_ids
# ---------------------------------------------------------------------------

class TestOnlineQueries:

    @pytest.mark.asyncio
    async def test_is_online_connected(self) -> None:
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(10, ws)
        assert mgr.is_online(10) is True

    @pytest.mark.asyncio
    async def test_is_online_disconnected(self) -> None:
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(10, ws)
        await mgr.disconnect(10)
        assert mgr.is_online(10) is False

    def test_is_online_never_connected(self) -> None:
        mgr = DeviceConnectionManager()
        assert mgr.is_online(42) is False

    @pytest.mark.asyncio
    async def test_get_online_device_ids(self) -> None:
        mgr = DeviceConnectionManager()
        for i in [3, 7, 11]:
            await mgr.connect(i, make_mock_websocket())
        ids = mgr.get_online_device_ids()
        assert sorted(ids) == [3, 7, 11]


# ---------------------------------------------------------------------------
# send_command + response routing
# ---------------------------------------------------------------------------

class TestSendCommand:

    @pytest.mark.asyncio
    async def test_send_command_and_receive_response(self) -> None:
        """send_command resolves when a matching RESP_OK arrives."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        # Capture what message id was sent, then simulate a response
        sent_text = None

        async def capture_send(text: str) -> None:
            nonlocal sent_text
            sent_text = text

        ws.send_text = AsyncMock(side_effect=capture_send)

        async def respond_after_send() -> dict:
            result = await mgr.send_command(1, MessageType.CMD_GET_STATUS, timeout=5.0)
            return result

        async def simulate_response() -> None:
            # Wait until the command is sent
            for _ in range(50):
                if sent_text is not None:
                    break
                await asyncio.sleep(0.01)
            assert sent_text is not None
            parsed = json.loads(sent_text)
            msg_id = parsed["id"]
            # Simulate device response
            resp = WSMessage(
                type=MessageType.RESP_OK,
                id="resp-123",
                payload={"battery": 85, "model": "Pixel"},
                reply_to=msg_id,
            )
            await mgr.handle_message(1, resp)

        result, _ = await asyncio.gather(respond_after_send(), simulate_response())
        assert result["battery"] == 85
        assert result["model"] == "Pixel"

    @pytest.mark.asyncio
    async def test_send_command_timeout(self) -> None:
        """send_command raises TimeoutError when no response arrives."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        with pytest.raises(asyncio.TimeoutError):
            await mgr.send_command(1, MessageType.CMD_PING, timeout=0.05)

    @pytest.mark.asyncio
    async def test_send_command_device_offline(self) -> None:
        """send_command raises ConnectionError for offline device."""
        mgr = DeviceConnectionManager()
        with pytest.raises(ConnectionError):
            await mgr.send_command(999, MessageType.CMD_PING)

    @pytest.mark.asyncio
    async def test_send_command_resp_error_raises(self) -> None:
        """RESP_ERROR resolves the future with a RuntimeError."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        sent_text = None

        async def capture(text: str) -> None:
            nonlocal sent_text
            sent_text = text

        ws.send_text = AsyncMock(side_effect=capture)

        async def send_cmd() -> dict:
            return await mgr.send_command(1, MessageType.CMD_GET_STATUS, timeout=5.0)

        async def send_error_response() -> None:
            for _ in range(50):
                if sent_text is not None:
                    break
                await asyncio.sleep(0.01)
            parsed = json.loads(sent_text)
            resp = WSMessage(
                type=MessageType.RESP_ERROR,
                payload={"error": "device busy"},
                reply_to=parsed["id"],
            )
            await mgr.handle_message(1, resp)

        with pytest.raises(RuntimeError, match="device busy"):
            await asyncio.gather(send_cmd(), send_error_response())


# ---------------------------------------------------------------------------
# handle_message routing
# ---------------------------------------------------------------------------

class TestHandleMessage:

    @pytest.mark.asyncio
    async def test_handle_message_routes_response(self) -> None:
        """RESP_OK with reply_to resolves the matching pending future."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        # Manually create a pending future
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        msg_id = "cmd-abc"
        mgr._pending[msg_id] = future

        resp = WSMessage(
            type=MessageType.RESP_OK,
            payload={"result": "success"},
            reply_to=msg_id,
        )
        await mgr.handle_message(1, resp)
        assert future.done()
        assert future.result() == {"result": "success"}

    @pytest.mark.asyncio
    async def test_handle_message_routes_event(self) -> None:
        """Event messages invoke the on_event callback."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        received_events: list[tuple[int, WSMessage]] = []

        async def on_event(device_id: int, msg: WSMessage) -> None:
            received_events.append((device_id, msg))

        mgr.on_event = on_event

        event_msg = WSMessage(
            type=MessageType.EVENT_POST_COMPLETE,
            payload={"post_id": 42},
        )
        await mgr.handle_message(1, event_msg)

        assert len(received_events) == 1
        assert received_events[0][0] == 1
        assert received_events[0][1].payload["post_id"] == 42

    @pytest.mark.asyncio
    async def test_handle_message_routes_request(self) -> None:
        """Request messages invoke on_request and send a RESP_OK back."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        async def on_request(device_id: int, msg: WSMessage) -> dict:
            return {"generated_text": "Hello from LLM"}

        mgr.on_request = on_request

        req_msg = WSMessage(
            type=MessageType.REQ_LLM_GENERATE,
            id="req-001",
            payload={"prompt": "test"},
        )
        await mgr.handle_message(1, req_msg)

        # Verify a response was sent back
        ws.send_text.assert_awaited()
        sent_raw = ws.send_text.call_args[0][0]
        sent_parsed = json.loads(sent_raw)
        assert sent_parsed["type"] == "resp.ok"
        assert sent_parsed["reply_to"] == "req-001"
        assert sent_parsed["payload"]["generated_text"] == "Hello from LLM"

    @pytest.mark.asyncio
    async def test_handle_message_request_error_sends_resp_error(self) -> None:
        """If on_request raises, a RESP_ERROR is sent back."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        async def on_request(device_id: int, msg: WSMessage) -> dict:
            raise ValueError("LLM service unavailable")

        mgr.on_request = on_request

        req_msg = WSMessage(
            type=MessageType.REQ_LLM_GENERATE,
            id="req-002",
            payload={},
        )
        await mgr.handle_message(1, req_msg)

        ws.send_text.assert_awaited()
        sent_parsed = json.loads(ws.send_text.call_args[0][0])
        assert sent_parsed["type"] == "resp.error"
        assert "LLM service unavailable" in sent_parsed["payload"]["error"]

    @pytest.mark.asyncio
    async def test_handle_pong_is_noop(self) -> None:
        """PONG messages are silently consumed."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        pong_msg = WSMessage(type=MessageType.PONG)
        # Should not raise
        await mgr.handle_message(1, pong_msg)

    @pytest.mark.asyncio
    async def test_handle_device_status_caches_info(self) -> None:
        """EVENT_DEVICE_STATUS updates cached device info."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        status_msg = WSMessage(
            type=MessageType.EVENT_DEVICE_STATUS,
            payload={"battery": 72, "model": "Samsung"},
        )
        await mgr.handle_message(1, status_msg)

        info = mgr.get_device_info(1)
        assert info["battery"] == 72
        assert info["model"] == "Samsung"


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

class TestBroadcast:

    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self) -> None:
        """broadcast sends the same message to all connected devices."""
        mgr = DeviceConnectionManager()
        ws1 = make_mock_websocket()
        ws2 = make_mock_websocket()
        ws3 = make_mock_websocket()
        await mgr.connect(1, ws1)
        await mgr.connect(2, ws2)
        await mgr.connect(3, ws3)

        await mgr.broadcast(MessageType.PING, payload={"ts": 123})

        for ws in [ws1, ws2, ws3]:
            ws.send_text.assert_awaited_once()
            sent = json.loads(ws.send_text.call_args[0][0])
            assert sent["type"] == "ping"

    @pytest.mark.asyncio
    async def test_broadcast_tolerates_failures(self) -> None:
        """If one device fails, broadcast still sends to the rest."""
        mgr = DeviceConnectionManager()
        ws_good = make_mock_websocket()
        ws_bad = make_mock_websocket()
        ws_bad.send_text = AsyncMock(side_effect=RuntimeError("connection lost"))
        await mgr.connect(1, ws_good)
        await mgr.connect(2, ws_bad)

        # Should not raise
        await mgr.broadcast(MessageType.PING)
        ws_good.send_text.assert_awaited_once()


# ---------------------------------------------------------------------------
# Concurrent commands
# ---------------------------------------------------------------------------

class TestConcurrent:

    @pytest.mark.asyncio
    async def test_concurrent_commands(self) -> None:
        """Multiple send_command calls in parallel each get their own response."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        captured_ids: list[str] = []

        async def capture_and_store(text: str) -> None:
            parsed = json.loads(text)
            captured_ids.append(parsed["id"])

        ws.send_text = AsyncMock(side_effect=capture_and_store)

        async def send_and_collect(index: int) -> dict:
            return await mgr.send_command(
                1,
                MessageType.CMD_GET_STATUS,
                payload={"index": index},
                timeout=5.0,
            )

        async def respond_to_all() -> None:
            """Wait for all 3 commands to be sent, then respond to each."""
            for _ in range(100):
                if len(captured_ids) == 3:
                    break
                await asyncio.sleep(0.01)
            assert len(captured_ids) == 3
            for idx, msg_id in enumerate(captured_ids):
                resp = WSMessage(
                    type=MessageType.RESP_OK,
                    payload={"index": idx},
                    reply_to=msg_id,
                )
                await mgr.handle_message(1, resp)

        results = await asyncio.gather(
            send_and_collect(0),
            send_and_collect(1),
            send_and_collect(2),
            respond_to_all(),
        )
        # First 3 results are the dicts, last is None (respond_to_all)
        payloads = results[:3]
        indices = sorted(p["index"] for p in payloads)
        assert indices == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_disconnect_cancels_pending(self) -> None:
        """Disconnecting a device fails only that device's pending futures."""
        mgr = DeviceConnectionManager()
        ws1 = make_mock_websocket()
        ws2 = make_mock_websocket()
        await mgr.connect(1, ws1)
        await mgr.connect(2, ws2)

        loop = asyncio.get_running_loop()

        # Create a pending future for device 1
        future1 = loop.create_future()
        mgr._pending["id-dev1"] = future1
        mgr._pending_device["id-dev1"] = 1

        # Create a pending future for device 2
        future2 = loop.create_future()
        mgr._pending["id-dev2"] = future2
        mgr._pending_device["id-dev2"] = 2

        # Disconnect device 1 — only its future should be cancelled
        await mgr.disconnect(1)

        assert future1.done()
        with pytest.raises(ConnectionError):
            future1.result()

        # Device 2's future should NOT be affected
        assert not future2.done()


class TestPingDevice:
    """Regression for Codex bug hunt 2026-04-14 iteration 3.

    Phone responds to app-level PING with a PONG message that includes
    `replyTo=<ping_id>`. The old VPS code unconditionally early-returned
    on any PONG without resolving the pending future, causing every
    `ping_device(...)` call to burn its full timeout and return False.
    """

    @pytest.mark.asyncio
    async def test_pong_resolves_ping_future(self) -> None:
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        # Kick off the ping in a task so we can deliver the PONG
        ping_task = asyncio.create_task(mgr.ping_device(1, timeout=2.0))
        # Yield control so ping_device registers its future in _pending
        await asyncio.sleep(0)

        # Grab the ping msg id that was registered
        assert len(mgr._pending) == 1
        ping_id = next(iter(mgr._pending.keys()))

        # Simulate the phone's PONG reply
        pong = WSMessage(
            id="pong-msg-id",
            type=MessageType.PONG,
            reply_to=ping_id,
            payload={"timestamp": 12345},
        )
        await mgr.handle_message(1, pong)

        # ping_device should now resolve to True immediately
        result = await asyncio.wait_for(ping_task, timeout=1.0)
        assert result is True
        # And _pending must be empty (no leak)
        assert len(mgr._pending) == 0
        assert len(mgr._pending_device) == 0

    @pytest.mark.asyncio
    async def test_ping_times_out_cleanly_if_no_pong(self) -> None:
        """Without a PONG reply, ping_device returns False within timeout."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        result = await mgr.ping_device(1, timeout=0.2)
        assert result is False
        # _pending and _pending_device must not leak even on timeout
        assert len(mgr._pending) == 0
        assert len(mgr._pending_device) == 0

    @pytest.mark.asyncio
    async def test_stray_pong_with_unknown_reply_to_does_nothing(self) -> None:
        """A PONG for an unrecognized id must not crash or leak state."""
        mgr = DeviceConnectionManager()
        ws = make_mock_websocket()
        await mgr.connect(1, ws)

        # Fire a PONG referencing a non-existent ping id
        stray = WSMessage(
            id="stray",
            type=MessageType.PONG,
            reply_to="nonexistent-id",
            payload={},
        )
        # Should not raise
        await mgr.handle_message(1, stray)
        assert len(mgr._pending) == 0
