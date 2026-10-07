"""FastAPI WebSocket endpoint for device connections."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from server.auth import verify_device_token
from server.config import VPSConfig
from server.license_gate import get_license_info, is_licensed
from server.models import Device
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType, create_message, deserialize, serialize

logger = logging.getLogger(__name__)

ws_router = APIRouter()


async def _device_is_inactive(websocket: WebSocket, device_id: int) -> bool:
    """Return whether the device exists in DB and has been soft-deleted."""
    session_factory = getattr(websocket.app.state, "db_session_factory", None)
    if session_factory is None:
        return False

    async with session_factory() as session:
        device = (await session.execute(
            select(Device).where(Device.id == device_id),
        )).scalar_one_or_none()

    return device is not None and not device.is_active


async def _send_reconcile_and_close(
    websocket: WebSocket,
    correct_device_id: int,
) -> None:
    """Accept the WS upgrade only long enough to push a reconcile frame, then close.

    Used when the URL device_id claim is wrong but the token is valid.  The
    phone receives `cmd.reconcile_device_id`, rewrites its local prefs, and
    reconnects with the correct id.  Security invariant: this path is ONLY
    reachable after `verify_device_token` has resolved the canonical id from
    the token; the URL claim is never trusted.
    """
    try:
        await websocket.accept()
        frame = {
            "type": "cmd.reconcile_device_id",
            "payload": {"correct_device_id": int(correct_device_id)},
        }
        await websocket.send_text(json.dumps(frame, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001 — best-effort notification
        logger.warning("Failed to send reconcile frame: %s", exc)
    finally:
        try:
            # 1011 = internal error / "policy" — signals the client to
            # treat this as a clean reconnect, not a fatal auth failure.
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:  # noqa: BLE001
            pass


@ws_router.websocket("/ws/device")
async def device_websocket(
    websocket: WebSocket,
    token: Annotated[str, Query(...)],
    device_id: Annotated[int, Query(...)],
) -> None:
    """WebSocket endpoint for phone device connections.

    Query parameters:
        token: JWT device auth token (signed with server jwt_secret).
        device_id: integer device identifier.
    """
    config: VPSConfig = websocket.app.state.config
    manager: DeviceConnectionManager = websocket.app.state.ws_manager

    # --- License gate ---
    if not is_licensed(config):
        logger.warning(
            "WS device connection rejected (no license) from %s",
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # --- Authenticate ---
    # Token verification is the canonical source of truth for device identity.
    # The URL `device_id` claim is treated only as a hint; if it disagrees with
    # the token's resolved id we send a reconcile frame so the phone updates
    # its local prefs and reconnects with the correct id (no manual edit
    # required, see 2026-04-30 Realme incident).
    verified_id = verify_device_token(token, config)
    if verified_id is None:
        logger.warning(
            "WS auth failed (invalid token) for claimed device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if verified_id != device_id:
        logger.warning(
            "WS device_id mismatch: token resolves to %d, URL claims %d (from %s) "
            "— sending reconcile frame",
            verified_id,
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await _send_reconcile_and_close(websocket, verified_id)
        return

    if await _device_is_inactive(websocket, device_id):
        logger.warning(
            "WS connection rejected for inactive device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # --- Max devices gate ---
    info = get_license_info(config)
    if info and info.max_devices:
        online_ids = manager.get_online_device_ids()
        # Allow reconnects (device already tracked) but reject new devices over limit
        if device_id not in online_ids and len(online_ids) >= info.max_devices:
            logger.warning(
                "Device %d rejected: max_devices=%d reached",
                device_id, info.max_devices,
            )
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    await websocket.accept()
    await manager.connect(device_id, websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = deserialize(raw)
            except (ValueError, KeyError) as exc:
                logger.warning("Bad message from device %d: %s — %s", device_id, exc, raw[:200])
                error_reply = create_message(
                    MessageType.RESP_ERROR,
                    {"error": f"malformed message: {exc}"},
                )
                await websocket.send_text(serialize(error_reply))
                continue
            await manager.handle_message(device_id, message)
    except WebSocketDisconnect:
        logger.info("Device %d disconnected normally", device_id)
    except Exception as exc:
        logger.error("Device %d connection error: %s", device_id, exc, exc_info=True)
    finally:
        await manager.disconnect(device_id, websocket)
