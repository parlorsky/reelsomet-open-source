"""FastAPI WebSocket endpoint for browser admin clients."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from server.auth import decode_jwt_token
from server.config import VPSConfig
from server.ws.admin_broadcaster import AdminBroadcaster

logger = logging.getLogger(__name__)

admin_ws_router = APIRouter()


@admin_ws_router.websocket("/ws/admin")
async def admin_websocket(
    websocket: WebSocket,
    token: Annotated[str, Query(...)],
) -> None:
    """WebSocket endpoint for browser admin clients.

    Query parameters:
        token: JWT auth token (same as used for REST API auth).

    The server pushes events to connected browsers; browsers do not send
    data upstream (any incoming frames are silently consumed to keep the
    connection alive).
    """
    config: VPSConfig = websocket.app.state.config
    broadcaster: AdminBroadcaster = websocket.app.state.admin_broadcaster

    # --- Authenticate via JWT ---
    payload = decode_jwt_token(token, config.jwt_secret)
    if payload is None:
        logger.warning(
            "Admin WS auth failed from %s",
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await broadcaster.connect(websocket)

    try:
        # Keep the connection open.  We don't expect meaningful upstream
        # messages, but we must read to detect disconnection.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("Admin browser disconnected normally")
    except Exception as exc:
        logger.error("Admin WS error: %s", exc, exc_info=True)
    finally:
        await broadcaster.disconnect(websocket)
