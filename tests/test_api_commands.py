"""Tests for WS command-sending wired into REST API endpoints.

Each test mocks the DeviceBridge via FastAPI dependency_overrides so no
real phone connection is needed.
"""
from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.dependencies import get_bridge
from server.models import Account, Device
from server.ws.bridge import DeviceBridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bridge_mock() -> AsyncMock:
    """Return an AsyncMock shaped like DeviceBridge."""
    bridge = AsyncMock(spec=DeviceBridge)
    bridge.ping = AsyncMock(return_value=True)
    bridge.send_schedule = AsyncMock(return_value={"ok": True})
    bridge.start_engagement = AsyncMock(return_value={"ok": True})
    bridge.abort_engagement = AsyncMock(return_value={"ok": True})
    bridge.start_insights = AsyncMock(return_value={"ok": True})
    bridge.start_monitoring = AsyncMock(return_value={"ok": True})
    return bridge


async def _register_fake_ws(app: Any, device_id: int) -> None:
    """Register a fake WS connection so the manager reports the device as online."""
    ws_manager = app.state.ws_manager
    fake_ws = AsyncMock()
    fake_ws.close = AsyncMock()
    fake_ws.send_text = AsyncMock()
    await ws_manager.connect(device_id, fake_ws)


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------

class TestPingCommand:

    @pytest.mark.asyncio
    async def test_ping_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/devices/{pk}/ping on online device returns latency."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(f"/api/devices/{dev.id}/ping", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["online"] is True
        assert "latency_ms" in body
        assert isinstance(body["latency_ms"], int)
        bridge_mock.ping.assert_awaited_once_with(dev.id)

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_ping_offline_device(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/devices/{pk}/ping on offline device returns online=False."""
        dev = seed_db["devices"][0]
        resp = await client.post(f"/api/devices/{dev.id}/ping", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["online"] is False
        assert "latency_ms" not in body


# ---------------------------------------------------------------------------
# Engagement start / abort
# ---------------------------------------------------------------------------

class TestEngagementStartCommand:

    @pytest.mark.asyncio
    async def test_engagement_start_sends_ws_command(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/engagement/{username}/start sends engagement cmd via bridge."""
        # user_gamma is linked to dev_b (seed_db)
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running"
        assert "session_id" in body

        # Verify bridge was called with correct device_id and Android-format payload
        bridge_mock.start_engagement.assert_awaited_once()
        call_args = bridge_mock.start_engagement.call_args
        assert call_args[0][0] == dev.id  # first positional arg = device_id
        payload = call_args[0][1]
        assert payload["accountUsername"] == "user_gamma"
        assert "channels" in payload
        assert "actionProbabilities" in payload

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_start_device_offline(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/engagement/{username}/start returns 409 when device is offline."""
        resp = await client.post(
            "/api/engagement/user_gamma/start",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "offline" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_engagement_start_rejects_inactive_account(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Manual engagement starts reject inactive accounts before device dispatch."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is inactive"
        bridge_mock.start_engagement.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_start_rejects_disabled_account(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Manual engagement starts respect the per-account enabled flag."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.engagement_enabled = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Engagement is disabled"
        bridge_mock.start_engagement.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_start_rejects_paused_account(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Manual engagement starts respect the pause switch before device dispatch."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.is_paused = True
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is paused"
        bridge_mock.start_engagement.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_start_rejects_inactive_device_link(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Manual engagement starts reject stale links to inactive devices."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, dev.id)
            assert device is not None
            device.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert "offline" in resp.json()["detail"].lower()
        bridge_mock.start_engagement.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_start_preserves_zero_budget_and_probabilities(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/engagement/{username}/start keeps explicit zero account settings."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_gamma"),
            )).scalar_one()
            account.engagement_daily_budget = 0
            account.engagement_like_prob = 0.0
            account.engagement_comment_prob = 0.0
            account.engagement_reply_prob = 0.0
            account.engagement_share_prob = 0.0
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        payload = bridge_mock.start_engagement.call_args[0][1]
        assert payload["dailyBudgetMinutes"] == 0
        assert payload["actionProbabilities"] == {
            "likeProbability": 0.0,
            "commentProbability": 0.0,
            "replyProbability": 0.0,
            "shareProbability": 0.0,
        }

        await app_with_db.state.ws_manager.disconnect(dev.id)


class TestEngagementAbortCommand:

    @pytest.mark.asyncio
    async def test_engagement_abort_sends_command(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/engagement/{username}/abort sends abort cmd via bridge."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            # First start a session so there's something to abort
            start_resp = await client.post(
                "/api/engagement/user_gamma/start",
                headers=auth_headers,
            )
            assert start_resp.status_code == 200

            # Now abort
            resp = await client.post(
                "/api/engagement/user_gamma/abort",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "aborted"
        bridge_mock.abort_engagement.assert_awaited_once_with(dev.id)

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_engagement_abort_without_running_session_still_sends_device_abort(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Abort can clear stale phone-side engagement even without a DB running row."""
        dev = seed_db["devices"][1]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/engagement/user_gamma/abort",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "aborted"
        assert body["session_id"] is None
        assert body["orphan_device_abort"] is True
        bridge_mock.abort_engagement.assert_awaited_once_with(dev.id)

        await app_with_db.state.ws_manager.disconnect(dev.id)


# ---------------------------------------------------------------------------
# Insights collect
# ---------------------------------------------------------------------------

class TestInsightsCollectCommand:

    @pytest.mark.asyncio
    async def test_insights_collect_sends_command(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/insights/{username}/collect sends insights cmd via bridge."""
        # user_alpha is linked to dev_a
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/insights/user_alpha/collect",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == dev.id
        assert "collection started" in body["message"].lower()

        bridge_mock.start_insights.assert_awaited_once()
        call_args = bridge_mock.start_insights.call_args
        assert call_args[0][0] == dev.id
        payload = call_args[0][1]
        # Android contract: nested accounts[] array, not flat username
        assert "accounts" in payload
        assert payload["accounts"][0]["username"] == "user_alpha"
        assert "maxReelsPerAccount" in payload

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_insights_collect_device_offline(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/insights/{username}/collect returns 409 when device offline."""
        resp = await client.post(
            "/api/insights/user_alpha/collect",
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "offline" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_insights_collect_account_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """POST /api/insights/{username}/collect returns 404 for unknown account."""
        resp = await client.post(
            "/api/insights/nonexistent_user/collect",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_insights_collect_rejects_inactive_account(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/insights/{username}/collect rejects inactive accounts before dispatch."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/insights/user_alpha/collect",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert resp.json()["detail"] == "Account is inactive"
        bridge_mock.start_insights.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_insights_collect_rejects_inactive_device_link(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Insights collection rejects stale links to inactive devices."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, dev.id)
            assert device is not None
            device.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/insights/user_alpha/collect",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 409
        assert "offline" in resp.json()["detail"].lower()
        bridge_mock.start_insights.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)


# ---------------------------------------------------------------------------
# Monitor run-now
# ---------------------------------------------------------------------------

class TestMonitorRunNowCommand:

    @pytest.mark.asyncio
    async def test_monitor_run_now_sends_command(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/monitor/run-now dispatches monitoring to online devices."""
        # user_alpha has monitor_target -> linked to dev_a
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post("/api/monitor/run-now", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert dev.id in body["devices_dispatched"]

        bridge_mock.start_monitoring.assert_awaited_once()
        call_args = bridge_mock.start_monitoring.call_args
        assert call_args[0][0] == dev.id
        payload = call_args[0][1]
        # Android contract: accountUsername (camelCase), targets with camelCase keys
        assert payload["accountUsername"] == "user_alpha"
        assert len(payload["targets"]) == 1
        assert payload["targets"][0]["targetUsername"] == "monitored_user"

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_monitor_run_now_no_online_devices(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/monitor/run-now with no online devices skips all."""
        resp = await client.post("/api/monitor/run-now", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["devices_dispatched"] == []
        assert len(body["accounts_skipped"]) > 0

    @pytest.mark.asyncio
    async def test_monitor_run_now_skips_inactive_accounts(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """run-now ignores monitor targets whose backing account was deactivated."""
        from server.models import Account

        async with app_with_db.state.db_session_factory() as session:
            account = (await session.execute(
                select(Account).where(Account.username == "user_alpha"),
            )).scalar_one()
            account.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post("/api/monitor/run-now", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["devices_dispatched"] == []
        assert body["accounts_skipped"] == []
        bridge_mock.start_monitoring.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_monitor_run_now_skips_inactive_device_links(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """run-now skips targets whose only linked device was soft-deleted."""
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        async with app_with_db.state.db_session_factory() as session:
            device = await session.get(Device, dev.id)
            assert device is not None
            device.is_active = False
            await session.commit()

        bridge_mock = _make_bridge_mock()
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post("/api/monitor/run-now", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["devices_dispatched"] == []
        assert body["accounts_skipped"] == ["user_alpha"]
        bridge_mock.start_monitoring.assert_not_awaited()
        await app_with_db.state.ws_manager.disconnect(dev.id)


# ---------------------------------------------------------------------------
# Upload auto-schedule
# ---------------------------------------------------------------------------

class TestUploadAutoSchedule:

    @pytest.mark.asyncio
    async def test_upload_auto_schedules_on_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/upload auto-pushes schedule when device is online."""
        # user_alpha linked to dev_a
        dev = seed_db["devices"][0]
        await _register_fake_ws(app_with_db, dev.id)

        bridge_mock = _make_bridge_mock()
        fake_video = b"\x00" * 512

        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                "/api/queue/upload",
                files={"file": ("auto_sched.mp4", io.BytesIO(fake_video), "video/mp4")},
                data={"account_username": "user_alpha", "caption": "Test"},
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 201
        body = resp.json()
        assert body["scheduled"] is True
        assert body["account_username"] == "user_alpha"

        bridge_mock.send_schedule.assert_awaited_once()
        call_args = bridge_mock.send_schedule.call_args
        assert call_args[0][0] == dev.id
        payload = call_args[0][1]
        # Android contract: nested accounts[].videos[] structure
        assert "accounts" in payload
        assert len(payload["accounts"]) == 1
        assert payload["accounts"][0]["username"] == "user_alpha"
        assert len(payload["accounts"][0]["videos"]) == 1
        assert payload["accounts"][0]["videos"][0]["filename"] == "auto_sched.mp4"

        await app_with_db.state.ws_manager.disconnect(dev.id)

    @pytest.mark.asyncio
    async def test_upload_no_schedule_when_offline(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/queue/upload does not schedule when device is offline."""
        fake_video = b"\x00" * 256

        resp = await client.post(
            "/api/queue/upload",
            files={"file": ("offline.mp4", io.BytesIO(fake_video), "video/mp4")},
            data={"account_username": "user_alpha", "caption": "Offline"},
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["scheduled"] is False
