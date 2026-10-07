"""FastAPI WebSocket endpoints for Phase 2 screen relay."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from server.auth import decode_jwt_token, verify_device_token
from server.config import VPSConfig
from server.license_gate import is_licensed

logger = logging.getLogger(__name__)

screen_ws_router = APIRouter()


@screen_ws_router.websocket("/ws/screen/upload/{device_id}")
async def screen_upload_websocket(
    websocket: WebSocket,
    device_id: int,
    token: str = Query(...),
) -> None:
    """Phone uploader endpoint for binary screen frames."""
    config: VPSConfig = websocket.app.state.config
    relay = websocket.app.state.screen_relay

    if not is_licensed(config):
        logger.warning(
            "Screen uploader rejected (no license) for device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    verified_id = verify_device_token(token, config)
    if verified_id is None or verified_id != device_id:
        logger.warning(
            "Screen uploader auth failed for device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await relay.register_uploader(device_id, websocket)
    try:
        await relay.pump_uploader(device_id, websocket)
    except WebSocketDisconnect:
        logger.info("Screen uploader %d disconnected normally", device_id)
    except Exception as exc:
        logger.error("Screen uploader %d error: %s", device_id, exc, exc_info=True)
    finally:
        await relay.unregister_uploader(device_id)


@screen_ws_router.websocket("/ws/screen/{device_id}")
async def screen_viewer_websocket(
    websocket: WebSocket,
    device_id: int,
    token: str = Query(...),
) -> None:
    """Browser viewer endpoint for relayed screen frames."""
    config: VPSConfig = websocket.app.state.config
    relay = websocket.app.state.screen_relay

    if not is_licensed(config):
        logger.warning(
            "Screen viewer rejected (no license) for device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    payload = decode_jwt_token(token, config.jwt_secret)
    if payload is None:
        logger.warning(
            "Screen viewer auth failed for device_id=%d from %s",
            device_id,
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await relay.add_viewer(device_id, websocket)
    await websocket.send_json(relay.current_capture_state_payload(device_id))
    try:
        await relay.pump_viewer(device_id, websocket)
    except WebSocketDisconnect:
        logger.info("Screen viewer for device %d disconnected normally", device_id)
    except Exception as exc:
        logger.error("Screen viewer for device %d error: %s", device_id, exc, exc_info=True)
    finally:
        await relay.remove_viewer(device_id, websocket)
