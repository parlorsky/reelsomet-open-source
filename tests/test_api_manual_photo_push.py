"""Tests for manual photo batch pushes to phones."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from server.dependencies import get_bridge
from server.ws.bridge import DeviceBridge


class TestManualPhotoPush:

    @pytest.mark.asyncio
    async def test_create_batch_ghosts_uploads_and_records_metadata(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_ghost(input_path: str, output_path: str, *_args: Any, **_kwargs: Any) -> Path:
            out = Path(output_path)
            out.write_bytes(Path(input_path).read_bytes() + b"-ghost")
            return out

        monkeypatch.setattr("server.ghost.ghost_media_safe", fake_ghost)

        resp = await client.post(
            "/api/manual-photo-push/batches",
            headers=auth_headers,
            data={"batch_id": "lab_batch"},
            files=[
                ("files", ("first.jpg", b"raw-a", "image/jpeg")),
                ("files", ("second.png", b"raw-b", "image/png")),
            ],
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["batch_id"] == "lab_batch"
        assert body["status"] == "ready"
        assert body["item_count"] == 2
        assert body["ghosted_count"] == 2
        assert body["failed_count"] == 0
        assert body["items"][0]["before"]["sha256"] != body["items"][0]["after"]["sha256"]

        batch_dir = Path(app_with_db.state.config.data_dir) / "manual_photo_push" / "lab_batch"
        assert (batch_dir / "manifest.json").is_file()
        assert sorted(p.name for p in (batch_dir / "ghosted").iterdir()) == [
            "000_first_ghost.jpg",
            "001_second_ghost.jpg",
        ]

        get_resp = await client.get("/api/manual-photo-push/batches/lab_batch", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["batch_id"] == "lab_batch"

    @pytest.mark.asyncio
    async def test_push_batch_can_use_common_ghosted_dir(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        batch_dir = (
            Path(app_with_db.state.config.data_dir)
            / "manual_photo_push"
            / "lab_common"
            / "ghosted"
        )
        batch_dir.mkdir(parents=True)
        (batch_dir / "a.jpg").write_bytes(b"a")

        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.push_image_assets = AsyncMock(return_value={
            "status": "ok",
            "downloadedCount": 1,
            "totalCount": 1,
        })
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/manual-photo-push/devices/{dev_id}",
                headers=auth_headers,
                json={"batch_id": "lab_common"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)
            await ws_manager.disconnect(dev_id)

        assert resp.status_code == 200
        assert resp.json()["asset_count"] == 1
        copied = batch_dir / f"device_{dev_id}" / "a.jpg"
        assert copied.is_file()

    @pytest.mark.asyncio
    async def test_push_batch_sends_sorted_assets_to_online_device(
        self,
        app_with_db: Any,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id
        batch_dir = (
            Path(app_with_db.state.config.data_dir)
            / "manual_photo_push"
            / "2.7_phset"
            / "ghosted"
            / f"device_{dev_id}"
        )
        batch_dir.mkdir(parents=True)
        (batch_dir / "b.jpg").write_bytes(b"b")
        (batch_dir / "a.jpg").write_bytes(b"a")

        ws_manager = app_with_db.state.ws_manager
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock()
        fake_ws.send_text = AsyncMock()
        await ws_manager.connect(dev_id, fake_ws)

        bridge_mock = AsyncMock(spec=DeviceBridge)
        bridge_mock.push_image_assets = AsyncMock(return_value={
            "status": "ok",
            "downloadedCount": 2,
            "totalCount": 2,
        })
        app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
        try:
            resp = await client.post(
                f"/api/manual-photo-push/devices/{dev_id}",
                headers=auth_headers,
                json={"batch_id": "2.7_phset"},
            )
        finally:
            app_with_db.dependency_overrides.pop(get_bridge, None)
            await ws_manager.disconnect(dev_id)

        assert resp.status_code == 200
        body = resp.json()
        assert body["device_id"] == dev_id
        assert body["asset_count"] == 2

        bridge_mock.push_image_assets.assert_awaited_once()
        args, kwargs = bridge_mock.push_image_assets.await_args
        assert args[0] == dev_id
        assert kwargs["batch_id"] == "2.7_phset"
        assert [asset["filename"] for asset in kwargs["assets"]] == ["a.jpg", "b.jpg"]
        assert [asset["index"] for asset in kwargs["assets"]] == [0, 1]
        assert all("/api/manual-photo-push/assets/2.7_phset/" in asset["url"] for asset in kwargs["assets"])

    @pytest.mark.asyncio
    async def test_push_batch_rejects_path_traversal(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id

        resp = await client.post(
            f"/api/manual-photo-push/devices/{dev_id}",
            headers=auth_headers,
            json={"batch_id": "../secret"},
        )

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_push_batch_requires_online_device(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        seed_db: dict[str, Any],
    ) -> None:
        dev_id = seed_db["devices"][0].id

        resp = await client.post(
            f"/api/manual-photo-push/devices/{dev_id}",
            headers=auth_headers,
            json={"batch_id": "2.7_phset"},
        )

        assert resp.status_code == 409
