from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from server.dependencies import get_bridge
from server.models import PinterestAccount, PinterestAsset, PinterestBoard, PinterestPin, PinterestPostAttempt
from server.ws.bridge import DeviceBridge
from server.ws.protocol import MessageType


@pytest.fixture(autouse=True)
def mock_pinterest_ghost():
    def fake_ghost(path: str | Path, output_path: str | Path | None = None, *args: Any, **kwargs: Any) -> Path:
        source = Path(path)
        target = Path(output_path) if output_path is not None else source
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes() + b"-ghosted")
        return target

    with patch("server.ghost.ghost_media_safe", side_effect=fake_ghost) as mock_ghost:
        yield mock_ghost


def _manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "import_id": "pin_import_001",
        "platform": "pinterest",
        "accounts": [{"key": "main", "username": "demo_creator"}],
        "boards": [
            {
                "key": "mirror",
                "account": "main",
                "name": "Mirror Selfies",
                "description": "Mirror ideas.",
                "visibility": "public",
            }
        ],
        "pins": [
            {
                "external_id": "pin_001",
                "account": "main",
                "board": "mirror",
                "file": "a.jpg",
                "title": "Mirror pose",
                "description": "Clean pose idea.",
            }
        ],
    }


@pytest.mark.asyncio
async def test_pinterest_import_creates_isolated_inventory(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["import_id"] == "pin_import_001"
    assert body["created_pins_count"] == 1
    assert body["created_boards_count"] == 1
    assert body["created_assets_count"] == 1

    accounts_resp = await client.get("/api/pinterest/accounts", headers=auth_headers)
    assert accounts_resp.status_code == 200
    accounts = accounts_resp.json()
    assert accounts[0]["username"] == "demo_creator"
    assert accounts[0]["scheduler"]["enabled"] is False

    boards_resp = await client.get("/api/pinterest/boards", headers=auth_headers)
    assert boards_resp.status_code == 200
    boards = boards_resp.json()
    assert boards[0]["name"] == "Mirror Selfies"
    assert boards[0]["status"] == "needs_create"

    pins_resp = await client.get("/api/pinterest/pins", headers=auth_headers)
    assert pins_resp.status_code == 200
    pins = pins_resp.json()
    assert pins[0]["external_id"] == "pin_001"
    assert pins[0]["title"] == "Mirror pose"
    assert "link" not in pins[0]

    imports_resp = await client.get("/api/pinterest/imports", headers=auth_headers)
    assert imports_resp.status_code == 200
    imports = imports_resp.json()
    assert imports[0]["import_id"] == "pin_import_001"
    assert imports[0]["pins_count"] == 1

    events_resp = await client.get("/api/pinterest/events", headers=auth_headers)
    assert events_resp.status_code == 200
    assert events_resp.json() == []


@pytest.mark.asyncio
async def test_pinterest_import_ghosts_uploaded_assets_before_queueing(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
    mock_pinterest_ghost,
) -> None:
    ghosted_bytes = b"raw-upload-bytes-ghosted"
    resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"raw-upload-bytes", "image/jpeg")),
        ],
    )

    assert resp.status_code == 200
    mock_pinterest_ghost.assert_called_once()

    async with app_with_db.state.db_session_factory() as session:
        asset = (await session.execute(select(PinterestAsset))).scalars().one()

    assert Path(asset.storage_path).read_bytes() == ghosted_bytes
    assert asset.media_hash == hashlib.sha256(ghosted_bytes).hexdigest()
    assert asset.file_size_bytes == len(ghosted_bytes)
    assert asset.status == "ready"


@pytest.mark.asyncio
async def test_pinterest_scheduler_patch_is_account_scoped(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    import_resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert import_resp.status_code == 200

    account_id = (await client.get("/api/pinterest/accounts", headers=auth_headers)).json()[0]["id"]
    patch_resp = await client.patch(
        f"/api/pinterest/accounts/{account_id}/scheduler",
        headers=auth_headers,
        json={
            "enabled": True,
            "target_pins_per_day": 12,
            "min_gap_minutes": 45,
            "posting_windows": [{"start": "09:00", "end": "12:00"}],
        },
    )

    assert patch_resp.status_code == 200
    scheduler = patch_resp.json()
    assert scheduler["enabled"] is True
    assert scheduler["target_pins_per_day"] == 12
    assert scheduler["posting_windows"] == [{"start": "09:00", "end": "12:00"}]


@pytest.mark.asyncio
async def test_pinterest_health_endpoint_dispatches_phone_command(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock(
        return_value={"status": "ok", "success": True, "result": "healthy"}
    )

    resp = await client.post(
        "/api/pinterest/devices/1/health-check",
        headers=auth_headers,
        json={
            "task_id": "pinterest_health_test",
            "trace_id": "ptrace_health_test",
            "account_username": "demo_creator",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_result"]["result"] == "healthy"
    app_with_db.state.ws_manager.send_command.assert_awaited_once()
    args, kwargs = app_with_db.state.ws_manager.send_command.await_args
    assert args[0] == 1
    assert args[1] == MessageType.CMD_PINTEREST_HEALTH_CHECK
    assert args[2]["account"]["username"] == "demo_creator"
    assert "link" not in json.dumps(args[2])


@pytest.mark.asyncio
async def test_pinterest_health_endpoint_returns_504_on_phone_timeout(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock(side_effect=TimeoutError)

    resp = await client.post(
        "/api/pinterest/devices/1/health-check",
        headers=auth_headers,
        json={"task_id": "pinterest_health_timeout"},
    )

    assert resp.status_code == 504
    assert resp.json()["detail"] == "device_command_timeout"


@pytest.mark.asyncio
async def test_pinterest_health_endpoint_returns_device_error_payload(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock(side_effect=RuntimeError("pinterest_home_not_visible"))

    resp = await client.post(
        "/api/pinterest/devices/1/health-check",
        headers=auth_headers,
        json={"task_id": "pinterest_health_device_error"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_result"] == {
        "status": "error",
        "success": False,
        "error": "pinterest_home_not_visible",
        "message": "pinterest_home_not_visible",
    }


@pytest.mark.asyncio
async def test_pinterest_ensure_board_endpoint_dispatches_phone_command_and_records_attempt(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    import_resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert import_resp.status_code == 200
    board_id = (await client.get("/api/pinterest/boards", headers=auth_headers)).json()[0]["id"]

    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock(
        return_value={"status": "ok", "success": True, "result": "board_active"}
    )

    resp = await client.post(
        f"/api/pinterest/devices/1/boards/{board_id}/ensure",
        headers=auth_headers,
        json={"task_id": "pinterest_board_test", "trace_id": "ptrace_board_test"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_result"]["success"] is True
    assert body["payload"]["board"]["name"] == "Mirror Selfies"
    assert "link" not in json.dumps(body["payload"])

    app_with_db.state.ws_manager.send_command.assert_awaited_once()
    args, _kwargs = app_with_db.state.ws_manager.send_command.await_args
    assert args[0] == 1
    assert args[1] == MessageType.CMD_PINTEREST_ENSURE_BOARD
    assert args[2]["task_id"] == "pinterest_board_test"
    assert args[2]["trace_id"] == "ptrace_board_test"
    assert args[2]["board"]["visibility"] == "public"

    async with app_with_db.state.db_session_factory() as session:
        board = await session.get(PinterestBoard, board_id)
        attempts = (
            await session.execute(
                select(PinterestPostAttempt).where(PinterestPostAttempt.task_id == "pinterest_board_test")
            )
        ).scalars().all()

    assert board is not None
    assert board.status == "active"
    assert board.last_error_code is None
    assert len(attempts) == 1
    assert attempts[0].status == "success"
    assert attempts[0].task_type == "pinterest.ensure_board"
    assert attempts[0].board_id == board_id


@pytest.mark.asyncio
async def test_pinterest_publish_pin_endpoint_dispatches_phone_command_and_records_attempt(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    import_resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert import_resp.status_code == 200
    pin_id = (await client.get("/api/pinterest/pins", headers=auth_headers)).json()[0]["id"]

    async with app_with_db.state.db_session_factory() as session:
        board = (await session.execute(select(PinterestBoard))).scalars().one()
        board.status = "active"
        await session.commit()

    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock(
        return_value={"status": "ok", "success": True, "result": "posted"}
    )

    resp = await client.post(
        f"/api/pinterest/devices/1/pins/{pin_id}/publish",
        headers=auth_headers,
        json={"task_id": "pinterest_pin_test", "trace_id": "ptrace_pin_test"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_result"]["result"] == "posted"
    assert body["payload"]["pin"]["title"] == "Mirror pose"
    assert body["payload"]["media"]["phone_storage_path"] == "/storage/emulated/0/Pictures/Reelsomet/a_ghost.jpg"
    assert body["payload"]["policy"] == {"profile_only": True, "fill_destination": False}
    assert "link" not in json.dumps(body["payload"])

    app_with_db.state.ws_manager.send_command.assert_awaited_once()
    args, _kwargs = app_with_db.state.ws_manager.send_command.await_args
    assert args[0] == 1
    assert args[1] == MessageType.CMD_PINTEREST_PUBLISH_PIN
    assert args[2]["task_id"] == "pinterest_pin_test"
    assert args[2]["trace_id"] == "ptrace_pin_test"

    async with app_with_db.state.db_session_factory() as session:
        pin = await session.get(PinterestPin, pin_id)
        attempts = (
            await session.execute(
                select(PinterestPostAttempt).where(PinterestPostAttempt.task_id == "pinterest_pin_test")
            )
        ).scalars().all()

    assert pin is not None
    assert pin.status == "posted"
    assert pin.attempt_count == 1
    assert pin.last_error_code is None
    assert len(attempts) == 1
    assert attempts[0].status == "success"
    assert attempts[0].task_type == "pinterest.publish_pin"
    assert attempts[0].pin_id == pin_id


@pytest.mark.asyncio
async def test_pinterest_stage_assets_endpoint_pushes_signed_assets_and_marks_staged(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
    seed_db: dict[str, Any],
) -> None:
    import_resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert import_resp.status_code == 200
    dev_id = seed_db["devices"][0].id
    async with app_with_db.state.db_session_factory() as session:
        account = (await session.execute(select(PinterestBoard))).scalars().one()
        pinterest_account = await session.get(PinterestAccount, account.account_id)
        assert pinterest_account is not None
        pinterest_account.device_id = dev_id
        await session.commit()

    bridge_mock = AsyncMock(spec=DeviceBridge)
    bridge_mock.push_image_assets = AsyncMock(return_value={
        "status": "ok",
        "success": True,
        "downloadedCount": 1,
        "totalCount": 1,
    })
    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.dependency_overrides[get_bridge] = lambda: bridge_mock
    try:
        resp = await client.post(
            f"/api/pinterest/devices/{dev_id}/assets/stage",
            headers=auth_headers,
            json={},
        )
    finally:
        app_with_db.dependency_overrides.pop(get_bridge, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["asset_count"] == 1
    assert body["device_result"]["downloadedCount"] == 1

    bridge_mock.push_image_assets.assert_awaited_once()
    args, kwargs = bridge_mock.push_image_assets.await_args
    assert args[0] == dev_id
    assert kwargs["batch_id"].startswith("pinterest_assets_")
    staged_asset = kwargs["assets"][0]
    assert staged_asset["filename"] == "a_ghost.jpg"
    assert f"/api/pinterest/assets/" in staged_asset["url"]
    assert "/download/a_ghost.jpg?" in staged_asset["url"]

    async with app_with_db.state.db_session_factory() as session:
        asset = (await session.execute(select(PinterestAsset))).scalars().one()
        assert asset.phone_staged_at is not None
        assert asset.last_error is None


@pytest.mark.asyncio
async def test_pinterest_publish_pin_endpoint_rejects_asset_not_staged_on_phone(
    client: AsyncClient,
    auth_headers: dict[str, str],
    app_with_db,
) -> None:
    import_resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(_manifest()), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )
    assert import_resp.status_code == 200
    pin_id = (await client.get("/api/pinterest/pins", headers=auth_headers)).json()[0]["id"]

    async with app_with_db.state.db_session_factory() as session:
        asset = (await session.execute(select(PinterestAsset))).scalars().one()
        asset.phone_storage_path = None
        await session.commit()

    app_with_db.state.ws_manager.is_online = MagicMock(return_value=True)
    app_with_db.state.ws_manager.send_command = AsyncMock()

    resp = await client.post(
        f"/api/pinterest/devices/1/pins/{pin_id}/publish",
        headers=auth_headers,
        json={"task_id": "pinterest_pin_not_staged"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Pinterest asset is not staged on phone"
    app_with_db.state.ws_manager.send_command.assert_not_called()


@pytest.mark.asyncio
async def test_pinterest_import_rejects_urls_in_manifest(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    payload = _manifest()
    payload["pins"][0]["description"] = "follow https://example.com"

    resp = await client.post(
        "/api/pinterest/imports",
        headers=auth_headers,
        files=[
            ("manifest", (None, json.dumps(payload), "application/json")),
            ("files", ("a.jpg", b"fake-image-bytes", "image/jpeg")),
        ],
    )

    assert resp.status_code == 400
