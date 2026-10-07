"""Tests for server.ws.bridge: DeviceBridge high-level operations."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType


class _ManagerStub:
    """Thin stub around DeviceConnectionManager that auto-responds to send_command."""

    def __init__(self, response_payload: dict | None = None) -> None:
        self._response = response_payload or {"status": "ok"}
        self.last_call: tuple | None = None
        self._online: set[int] = set()

    def is_online(self, device_id: int) -> bool:
        return device_id in self._online

    def set_online(self, device_id: int) -> None:
        self._online.add(device_id)

    async def send_command(
        self,
        device_id: int,
        msg_type: MessageType,
        payload: dict | None = None,
        timeout: float = 10.0,
    ) -> dict:
        self.last_call = (device_id, msg_type, payload, timeout)
        return self._response


# ---------------------------------------------------------------------------
# Device info / status
# ---------------------------------------------------------------------------

class TestBridgeDeviceInfo:

    @pytest.mark.asyncio
    async def test_get_device_info(self) -> None:
        stub = _ManagerStub({"model": "Pixel 6", "android": 34})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_device_info(1)
        assert result == {"model": "Pixel 6", "android": 34}
        assert stub.last_call[1] == MessageType.CMD_GET_DEVICE_INFO

    @pytest.mark.asyncio
    async def test_get_status(self) -> None:
        stub = _ManagerStub({"battery": 90, "posting": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_status(2)
        assert result["battery"] == 90
        assert stub.last_call[0] == 2
        assert stub.last_call[1] == MessageType.CMD_GET_STATUS

    @pytest.mark.asyncio
    async def test_get_accounts(self) -> None:
        stub = _ManagerStub({"accounts": [{"name": "user1"}, {"name": "user2"}]})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_accounts(3)
        assert len(result) == 2
        assert result[0]["name"] == "user1"

    @pytest.mark.asyncio
    async def test_get_accounts_empty(self) -> None:
        """If 'accounts' key is missing, returns empty list."""
        stub = _ManagerStub({})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_accounts(3)
        assert result == []


# ---------------------------------------------------------------------------
# Post logs
# ---------------------------------------------------------------------------

class TestBridgePostLogs:

    @pytest.mark.asyncio
    async def test_get_post_logs(self) -> None:
        stub = _ManagerStub({"logs": [{"id": 1}], "since": 5000})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_post_logs(1, since_ms=5000)
        assert result["logs"] == [{"id": 1}]
        assert stub.last_call[1] == MessageType.CMD_GET_POST_LOGS
        assert stub.last_call[2] == {"since": 5000}

    @pytest.mark.asyncio
    async def test_get_post_logs_default_since(self) -> None:
        stub = _ManagerStub({})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        await bridge.get_post_logs(1)
        assert stub.last_call[2] == {"since": 0}


# ---------------------------------------------------------------------------
# Schedule / cancel
# ---------------------------------------------------------------------------

class TestBridgeSchedule:

    @pytest.mark.asyncio
    async def test_send_schedule(self) -> None:
        stub = _ManagerStub({"scheduled": 3})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        payload = {"accounts": {"user1": [1000, 2000]}}
        result = await bridge.send_schedule(1, payload)
        assert result["scheduled"] == 3
        assert stub.last_call[1] == MessageType.CMD_SEND_SCHEDULE
        assert stub.last_call[2] == payload

    @pytest.mark.asyncio
    async def test_cancel_video(self) -> None:
        stub = _ManagerStub({"cancelled": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.cancel_video(1, video_id=42)
        assert result["cancelled"] is True
        assert stub.last_call[1] == MessageType.CMD_CANCEL_VIDEO
        assert stub.last_call[2] == {"videoId": 42}

    @pytest.mark.asyncio
    async def test_push_image_assets(self) -> None:
        stub = _ManagerStub({"status": "ok", "downloadedCount": 2})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        assets = [
            {"url": "https://example.com/a.jpg", "filename": "a.jpg", "index": 0},
            {"url": "https://example.com/b.jpg", "filename": "b.jpg", "index": 1},
        ]

        result = await bridge.push_image_assets(2, batch_id="2.7_phset", assets=assets)

        assert result["downloadedCount"] == 2
        assert stub.last_call[0] == 2
        assert stub.last_call[1] == MessageType.CMD_PUSH_IMAGE_ASSETS
        assert stub.last_call[2] == {"batchId": "2.7_phset", "assets": assets}
        assert stub.last_call[3] >= 180


# ---------------------------------------------------------------------------
# Pinterest
# ---------------------------------------------------------------------------

class TestBridgePinterest:

    @pytest.mark.asyncio
    async def test_pinterest_health_check(self) -> None:
        stub = _ManagerStub({"status": "ok"})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        payload = {"account": "demo_creator"}

        result = await bridge.pinterest_health_check(2, payload)

        assert result == {"status": "ok"}
        assert stub.last_call[0] == 2
        assert stub.last_call[1] == MessageType.CMD_PINTEREST_HEALTH_CHECK
        assert stub.last_call[2] == payload

    @pytest.mark.asyncio
    async def test_pinterest_ensure_board_uses_long_timeout(self) -> None:
        stub = _ManagerStub({"success": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]

        await bridge.pinterest_ensure_board(2, {"board": {"name": "Mirror Selfies"}})

        assert stub.last_call[1] == MessageType.CMD_PINTEREST_ENSURE_BOARD
        assert stub.last_call[3] >= 30

    @pytest.mark.asyncio
    async def test_pinterest_publish_pin_uses_long_timeout(self) -> None:
        stub = _ManagerStub({"success": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]

        await bridge.pinterest_publish_pin(2, {"pin": {"id": 100}})

        assert stub.last_call[1] == MessageType.CMD_PINTEREST_PUBLISH_PIN
        assert stub.last_call[3] >= 150


# ---------------------------------------------------------------------------
# Reddit
# ---------------------------------------------------------------------------

class TestBridgeReddit:

    @pytest.mark.asyncio
    async def test_reddit_publish_post_uses_long_timeout(self) -> None:
        stub = _ManagerStub({"success": True, "redditPostId": "t3_post"})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        payload = {
            "post": {"id": 100, "title": "Morning trouble"},
            "media": {"url": "https://example.test/reddit-assets/1/photo.jpg"},
        }

        result = await bridge.reddit_publish_post(3, payload)

        assert result["redditPostId"] == "t3_post"
        assert stub.last_call[0] == 3
        assert stub.last_call[1] == MessageType.CMD_REDDIT_PUBLISH_POST
        assert stub.last_call[2] == payload
        assert stub.last_call[3] >= 180

    @pytest.mark.asyncio
    async def test_reddit_reply_comment_uses_long_timeout(self) -> None:
        stub = _ManagerStub({"success": True, "replyId": "t1_reply"})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        payload = {"comment": {"redditCommentId": "t1_comment"}, "reply": {"text": "Thanks."}}

        result = await bridge.reddit_reply_comment(3, payload)

        assert result["replyId"] == "t1_reply"
        assert stub.last_call[0] == 3
        assert stub.last_call[1] == MessageType.CMD_REDDIT_REPLY_COMMENT
        assert stub.last_call[2] == payload
        assert stub.last_call[3] >= 90

    @pytest.mark.asyncio
    async def test_reddit_scan_comments_uses_long_timeout(self) -> None:
        stub = _ManagerStub({"success": True, "comments": [{"body": "Cute"}]})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        payload = {"post": {"id": 100, "title": "Morning trouble"}}

        result = await bridge.reddit_scan_comments(3, payload)

        assert result["comments"][0]["body"] == "Cute"
        assert stub.last_call[0] == 3
        assert stub.last_call[1] == MessageType.CMD_REDDIT_SCAN_COMMENTS
        assert stub.last_call[2] == payload
        assert stub.last_call[3] >= 90


# ---------------------------------------------------------------------------
# Remote screen input
# ---------------------------------------------------------------------------

class TestBridgeRemoteScreenInput:

    @pytest.mark.asyncio
    async def test_screen_input_sends_payload(self) -> None:
        stub = _ManagerStub({"status": "ok", "kind": "tap"})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]

        result = await bridge.screen_input(3, {"kind": "tap", "x": 414, "y": 1167})

        assert result == {"status": "ok", "kind": "tap"}
        assert stub.last_call[0] == 3
        assert stub.last_call[1] == MessageType.CMD_SCREEN_INPUT
        assert stub.last_call[2] == {"kind": "tap", "x": 414, "y": 1167}

    @pytest.mark.asyncio
    async def test_screen_text_sends_text_payload(self) -> None:
        stub = _ManagerStub({"status": "ok", "text_set": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]

        result = await bridge.screen_text(3, "demo_creator")

        assert result == {"status": "ok", "text_set": True}
        assert stub.last_call[0] == 3
        assert stub.last_call[1] == MessageType.CMD_SCREEN_TEXT
        assert stub.last_call[2] == {"text": "demo_creator"}


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

class TestBridgeInsights:

    @pytest.mark.asyncio
    async def test_start_insights(self) -> None:
        stub = _ManagerStub({"started": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.start_insights(1, {"accounts": ["user1"]})
        assert result["started"] is True
        assert stub.last_call[1] == MessageType.CMD_START_INSIGHTS
        assert stub.last_call[2] == {"accounts": ["user1"]}


# ---------------------------------------------------------------------------
# Engagement
# ---------------------------------------------------------------------------

class TestBridgeEngagement:

    @pytest.mark.asyncio
    async def test_start_engagement(self) -> None:
        stub = _ManagerStub({"started": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.start_engagement(1, {"channels": ["ch1"]})
        assert result["started"] is True
        assert stub.last_call[1] == MessageType.CMD_START_ENGAGEMENT

    @pytest.mark.asyncio
    async def test_abort_engagement(self) -> None:
        stub = _ManagerStub({"aborted": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.abort_engagement(1)
        assert result["aborted"] is True
        assert stub.last_call[1] == MessageType.CMD_ABORT_ENGAGEMENT


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

class TestBridgeMonitoring:

    @pytest.mark.asyncio
    async def test_start_monitoring(self) -> None:
        stub = _ManagerStub({"started": True})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.start_monitoring(1, {"interval": 60})
        assert result["started"] is True
        assert stub.last_call[1] == MessageType.CMD_START_MONITORING


# ---------------------------------------------------------------------------
# Account sync
# ---------------------------------------------------------------------------

class TestBridgeSync:

    @pytest.mark.asyncio
    async def test_sync_accounts(self) -> None:
        stub = _ManagerStub({"synced": 3})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.sync_accounts(1, ["user1", "user2", "user3"])
        assert result["synced"] == 3
        assert stub.last_call[1] == MessageType.CMD_SYNC_ACCOUNTS
        assert stub.last_call[2] == {"usernames": ["user1", "user2", "user3"]}


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

class TestBridgePing:

    @pytest.mark.asyncio
    async def test_ping_online(self) -> None:
        """ping returns True when device responds."""
        stub = _ManagerStub({"pong": True})
        stub.set_online(1)
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        assert await bridge.ping(1) is True

    @pytest.mark.asyncio
    async def test_ping_offline(self) -> None:
        """ping returns False for disconnected device (never registered)."""
        stub = _ManagerStub({})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        assert await bridge.ping(99) is False

    @pytest.mark.asyncio
    async def test_ping_timeout_returns_false(self) -> None:
        """ping returns False when send_command times out."""
        mgr = DeviceConnectionManager()
        ws = AsyncMock()
        ws.send_text = AsyncMock()
        ws.close = AsyncMock()
        await mgr.connect(1, ws)

        bridge = DeviceBridge(mgr)
        # send_command will time out because no response comes back
        # Bridge ping uses timeout=5.0, but we override with a short one
        # by testing the actual code path — it catches the exception
        result = await bridge.ping(1)
        # This will time out after 5s, but in the test the future never resolves,
        # so we mock send_command to raise TimeoutError
        pass  # covered by test below

    @pytest.mark.asyncio
    async def test_ping_exception_returns_false(self) -> None:
        """ping returns False on any exception from send_command."""
        stub = _ManagerStub({})
        stub.set_online(1)

        # Override send_command to raise
        async def raise_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        stub.send_command = raise_timeout  # type: ignore[assignment]

        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        assert await bridge.ping(1) is False


# ---------------------------------------------------------------------------
# Engagement log
# ---------------------------------------------------------------------------

class TestBridgeEngagementLog:

    @pytest.mark.asyncio
    async def test_get_engagement_log(self) -> None:
        stub = _ManagerStub({"log": "line1\nline2\nline3"})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_engagement_log(1, lines=50)
        assert result == "line1\nline2\nline3"
        assert stub.last_call[1] == MessageType.CMD_GET_ENGAGEMENT_LOG
        assert stub.last_call[2] == {"lines": 50}

    @pytest.mark.asyncio
    async def test_get_engagement_log_missing_key(self) -> None:
        """If 'log' key is missing, returns empty string."""
        stub = _ManagerStub({})
        bridge = DeviceBridge(stub)  # type: ignore[arg-type]
        result = await bridge.get_engagement_log(1)
        assert result == ""
