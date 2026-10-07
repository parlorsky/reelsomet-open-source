"""WebSocket sub-package: protocol, connection manager, handler, bridge, admin."""
from __future__ import annotations

from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.admin_handler import admin_ws_router
from server.ws.bridge import DeviceBridge
from server.ws.handler import ws_router
from server.ws.manager import DeviceConnectionManager
from server.ws.protocol import MessageType, WSMessage, create_message, deserialize, serialize

__all__ = [
    "AdminBroadcaster",
    "DeviceBridge",
    "DeviceConnectionManager",
    "MessageType",
    "WSMessage",
    "admin_ws_router",
    "create_message",
    "deserialize",
    "serialize",
    "ws_router",
]
