"""Tests for server.api.devices: CRUD, ping, token generation."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from server.dependencies import get_bridge
from server.ws.bridge import DeviceBridge


class TestListDevices:

    @pytest.mark.asyncio
    async def test_list_devices(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/devices returns seeded devices."""
        resp = await client.get("/api/devices", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2

        device_ids = {d["device_id"] for d in body}
        assert "DEV-A-SERIAL" in device_ids
        assert "DEV-B-SERIAL" in device_ids

        # Verify fields
        dev_a = next(d for d in body if d["device_id"] == "DEV-A-SERIAL")
        assert dev_a["name"] == "Test Honor"
        assert dev_a["device_model"] == "FNE-NX9"
        assert dev_a["is_active"] is True
        # No WS connections in test, so is_online should be False
        assert dev_a["is_online"] is False

    @pytest.mark.asyncio
    async def test_list_devices_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/devices without token returns 401."""
        resp = await client.get("/api/devices")
        assert resp.status_code == 401


class TestAddDevice:

    @pytest.mark.asyncio
    async def test_add_device(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices creates a new device with 201."""
        resp = await client.post(
            "/api/devices",
            json={
                "device_id": "NEW-DEV-SERIAL",
                "name": "New Test Device",
                "ip_address": "192.168.1.99",
                "port": 9090,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["device_id"] == "NEW-DEV-SERIAL"
        assert body["name"] == "New Test Device"
        assert body["ip_address"] == "192.168.1.99"
        assert body["port"] == 9090
        assert body["is_active"] is True
        assert "id" in body

    @pytest.mark.asyncio
    async def test_add_device_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
    ) -> None:
        """Creating a device broadcasts a device:inventory event for live admin UIs."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        resp = await client.post(
            "/api/devices",
            json={
                "device_id": "NEW-DEV-BROADCAST",
                "name": "Broadcast Device",
                "ip_address": "192.168.1.99",
                "port": 9090,
            },
            headers=auth_headers,
        )

        assert resp.status_code == 201
        body = resp.json()
        broadcaster.broadcast.assert_awaited_once_with(
            "device:inventory",
            {
                "device_id": body["id"],
                "action": "added",
            },
        )

    @pytest.mark.asyncio
    async def test_add_device_trims_required_fields(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices trims required text fields before persisting."""
        resp = await client.post(
            "/api/devices",
            json={
                "device_id": "  NEW-DEV-SERIAL  ",
                "name": "  New Test Device  ",
                "ip_address": "  192.168.1.99  ",
                "port": 9090,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["device_id"] == "NEW-DEV-SERIAL"
        assert body["name"] == "New Test Device"
        assert body["ip_address"] == "192.168.1.99"

    @pytest.mark.asyncio
    async def test_add_device_rejects_blank_required_fields(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices rejects blank required text values after trimming."""
        resp = await client.post(
            "/api/devices",
            json={
                "device_id": "   ",
                "name": "Test Device",
                "ip_address": "192.168.1.99",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Device ID cannot be empty"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("port", [0, 65536])
    async def test_add_device_rejects_invalid_port(
        self, client: AsyncClient, auth_headers: dict[str, str], port: int,
    ) -> None:
        """POST /api/devices rejects impossible TCP port values."""
        resp = await client.post(
            "/api/devices",
            json={
                "device_id": f"NEW-DEV-{port}",
                "name": "Test Device",
                "ip_address": "192.168.1.99",
                "port": port,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Port must be between 1 and 65535"

    @pytest.mark.asyncio
    async def test_add_device_duplicate_normalized_device_id(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices rejects duplicates after device_id normalization."""
        resp = await client.post(
            "/api/devices",
            json={"device_id": "  DEV-A-SERIAL  ", "name": "Duplicate", "ip_address": "192.168.1.99"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_add_device_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices with duplicate device_id returns 409."""
        resp = await client.post(
            "/api/devices",
            json={"device_id": "DEV-A-SERIAL", "name": "Duplicate", "ip_address": "192.168.1.99"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestGetDevice:

    @pytest.mark.asyncio
    async def test_get_device(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """GET /api/devices/{pk} returns a specific device."""
        dev_id = seed_db["devices"][0].id
        resp = await client.get(f"/api/devices/{dev_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == dev_id
        assert body["device_id"] == "DEV-A-SERIAL"
        assert body["name"] == "Test Honor"

    @pytest.mark.asyncio
    async def test_get_device_not_found(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/devices/999 returns 404."""
        resp = await client.get("/api/devices/999", headers=auth_headers)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_get_device_inactive_returns_not_found(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Soft-deleted devices are no longer retrievable by ID."""
        dev_id = seed_db["devices"][0].id
        delete_resp = await client.delete(f"/api/devices/{dev_id}", headers=auth_headers)
        assert delete_resp.status_code == 200

        resp = await client.get(f"/api/devices/{dev_id}", headers=auth_headers)
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestDeleteDevice:

    @pytest.mark.asyncio
    async def test_delete_device(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """DELETE /api/devices/{pk} soft-deletes the device."""
        dev_id = seed_db["devices"][1].id
        resp = await client.delete(f"/api/devices/{dev_id}", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == dev_id
        assert body["is_active"] is False
        assert body["unlinked_accounts"] == 1

        # Verify it no longer appears in the active list
        list_resp = await client.get("/api/devices", headers=auth_headers)
        active_ids = {d["id"] for d in list_resp.json()}
        assert dev_id not in active_ids

    @pytest.mark.asyncio
    async def test_delete_device_broadcasts_inventory_update(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        app_with_db: Any,
        seed_db: dict[str, Any],
    ) -> None:
        """Deleting a device broadcasts a device:inventory event for live admin UIs."""
        broadcaster = app_with_db.state.admin_broadcaster
        broadcaster.broadcast = AsyncMock()

        dev_id = seed_db["devices"][1].id
        resp = await client.delete(f"/api/devices/{dev_id}", headers=auth_headers)

        assert resp.status_code == 200
        broadcaster.broadcast.assert_awaited_once_with(
            "device:inventory",
            {
                "device_id": dev_id,
                "action": "removed",
                "unlinked_accounts": 1,
            },
        )

    @pytest.mark.asyncio
    async def test_delete_device_unlinks_associated_accounts(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Deleting a device removes its account links so accounts stop showing the ghost device."""
        dev_id = seed_db["devices"][0].id
        device_name = seed_db["devices"][0].name

        resp = await client.delete(f"/api/devices/{dev_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["unlinked_accounts"] == 2

        accounts_resp = await client.get("/api/accounts", headers=auth_headers)
        assert accounts_resp.status_code == 200
        accounts = {item["username"]: item for item in accounts_resp.json()}
        assert accounts["user_alpha"]["device_name"] == ""
        assert accounts["user_beta"]["device_name"] == ""
        assert accounts["user_gamma"]["device_name"] != device_name


class TestPingDevice:

    @pytest.mark.asyncio
    async def test_ping_device_online(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/devices/{pk}/ping returns True with latency when WS connected."""
        dev_id = seed_db["devices"][0].id
        # Register a fake WS connection
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        # Mock the bridge so ping doesn't actually await a WS response
        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.ping = AsyncMock(return_value=True)
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(f"/api/devices/{dev_id}/ping", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        body = resp.json()
        assert body["online"] is True
        assert "latency_ms" in body
        bridge_mock.ping.assert_awaited_once_with(dev_id)

        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_ping_device_offline(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """POST /api/devices/{pk}/ping returns False when not connected."""
        dev_id = seed_db["devices"][0].id
        resp = await client.post(f"/api/devices/{dev_id}/ping", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["online"] is False

    @pytest.mark.asyncio
    async def test_ping_device_inactive_returns_not_found(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        """Soft-deleted devices are no longer pingable by ID."""
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        delete_resp = await client.delete(f"/api/devices/{dev_id}", headers=auth_headers)
        assert delete_resp.status_code == 200

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.ping = AsyncMock(return_value=True)
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(f"/api/devices/{dev_id}/ping", headers=auth_headers)
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()
        bridge_mock.ping.assert_not_awaited()
        await ws_manager.disconnect(dev_id)


class TestForceRestartDevice:

    @pytest.mark.asyncio
    async def test_force_restart_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.send_app_force_restart = AsyncMock(return_value=None)
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/devices/{dev_id}/force-restart",
                headers=auth_headers,
                json={"reason": "self_update_stuck"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": dev_id,
            "sent": True,
            "reason": "self_update_stuck",
        }
        bridge_mock.send_app_force_restart.assert_awaited_once_with(dev_id, "self_update_stuck")
        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_force_restart_offline_device_returns_conflict(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        resp = await client.post(f"/api/devices/{dev_id}/force-restart", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["detail"] == "Device is offline"


class TestPhoneQueueOps:

    @pytest.mark.asyncio
    async def test_phone_videos_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock()
        bridge_mock.get_all_videos = AsyncMock(
            return_value={"videos": [{"id": 7, "accountUsername": "user_alpha"}]},
        )
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.get(
                f"/api/devices/{dev_id}/phone-videos?all=true",
                headers=auth_headers,
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": dev_id,
            "all": True,
            "videos": [{"id": 7, "accountUsername": "user_alpha"}],
        }
        bridge_mock.get_all_videos.assert_awaited_once_with(dev_id)
        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_clear_phone_queue_filters_account_and_cancels_active_videos(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock()
        bridge_mock.get_pending_videos = AsyncMock(return_value={
            "videos": [
                {"id": 11, "accountUsername": "user_alpha", "status": "PENDING"},
                {"id": 12, "accountUsername": "other_user", "status": "SCHEDULED"},
                {"id": 13, "accountUsername": "user_alpha", "status": "POSTED"},
            ],
        })
        bridge_mock.cancel_video = AsyncMock(return_value={"status": "ok"})
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/devices/{dev_id}/phone-videos/clear",
                headers=auth_headers,
                json={"account_username": "user_alpha"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json()["cancelled"] == [11]
        bridge_mock.cancel_video.assert_awaited_once_with(dev_id, 11)
        await ws_manager.disconnect(dev_id)


class TestScreenKeycodeDevice:

    @pytest.mark.asyncio
    async def test_screen_keycode_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.screen_keycode = AsyncMock(return_value={"status": "ok", "globalAction": 1})
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/devices/{dev_id}/screen-keycode",
                headers=auth_headers,
                json={"key": "BACK"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": dev_id,
            "key": "back",
            "device_result": {"status": "ok", "globalAction": 1},
        }
        bridge_mock.screen_keycode.assert_awaited_once_with(dev_id, "back")
        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_screen_keycode_rejects_unsupported_key(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        resp = await client.post(
            f"/api/devices/{dev_id}/screen-keycode",
            headers=auth_headers,
            json={"key": "power"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported key"


class TestRemoteScreenInputDevice:

    @pytest.mark.asyncio
    async def test_screen_input_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.screen_input = AsyncMock(return_value={"status": "ok", "kind": "tap"})
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/devices/{dev_id}/screen-input",
                headers=auth_headers,
                json={"kind": "tap", "x": 414, "y": 1167},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": dev_id,
            "input": {"kind": "tap", "x": 414, "y": 1167},
            "device_result": {"status": "ok", "kind": "tap"},
        }
        bridge_mock.screen_input.assert_awaited_once_with(
            dev_id,
            {"kind": "tap", "x": 414.0, "y": 1167.0},
        )
        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_screen_input_rejects_bad_kind(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        resp = await client.post(
            f"/api/devices/{dev_id}/screen-input",
            headers=auth_headers,
            json={"kind": "pinch", "x": 0.5, "y": 0.5},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Unsupported input kind"

    @pytest.mark.asyncio
    async def test_screen_input_requires_swipe_endpoint(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        resp = await client.post(
            f"/api/devices/{dev_id}/screen-input",
            headers=auth_headers,
            json={"kind": "swipe", "x": 0.5, "y": 0.8},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Swipe requires x2 and y2"

    @pytest.mark.asyncio
    async def test_screen_text_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.screen_text = AsyncMock(return_value={"status": "ok", "text_set": True})
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/devices/{dev_id}/screen-text",
                headers=auth_headers,
                json={"text": "demo_creator"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)

        assert resp.status_code == 200
        assert resp.json() == {
            "device_id": dev_id,
            "text": "demo_creator",
            "device_result": {"status": "ok", "text_set": True},
        }
        bridge_mock.screen_text.assert_awaited_once_with(dev_id, "demo_creator")
        await ws_manager.disconnect(dev_id)

    @pytest.mark.asyncio
    async def test_screen_text_rejects_empty_text(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        resp = await client.post(
            f"/api/devices/{dev_id}/screen-text",
            headers=auth_headers,
            json={"text": ""},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Text cannot be empty"


class TestGenerateDeviceToken:

    @pytest.mark.asyncio
    @patch("server.api.devices.save_config")
    async def test_generate_device_token(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """POST /api/devices/token/generate returns a new token with auto-incremented device_id."""
        resp = await client.post("/api/devices/token/generate", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "device_id" in body
        assert "token" in body
        assert body["device_id"] == 1  # first token, empty dict -> max 0 + 1
        assert len(body["token"]) == 32  # secrets.token_hex(16) -> 32 hex chars
        assert mock_save.called

    @pytest.mark.asyncio
    @patch("server.api.devices.save_config")
    async def test_generate_second_token(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Second call returns device_id = previous + 1."""
        # First token
        resp1 = await client.post("/api/devices/token/generate", headers=auth_headers)
        assert resp1.status_code == 200
        first_id = resp1.json()["device_id"]

        # Second token
        resp2 = await client.post("/api/devices/token/generate", headers=auth_headers)
        assert resp2.status_code == 200
        second_id = resp2.json()["device_id"]

        assert second_id == first_id + 1
        # Both tokens should be unique
        assert resp1.json()["token"] != resp2.json()["token"]
