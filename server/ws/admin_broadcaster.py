"""Admin WebSocket broadcaster: manages browser connections and pushes events."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class AdminBroadcaster:
    """Manages browser WebSocket connections and broadcasts server events.

    Thread-safe via asyncio.Lock.  Dead clients are silently removed during
    broadcast — one broken connection never blocks delivery to others.

    Event wire format (JSON text frame)::

        {
            "type": "device:status",
            "ts": 1712345678000,
            "data": { ... }
        }
    """

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket) -> None:
        """Register a browser admin connection."""
        async with self._lock:
            self._connections.add(websocket)
        logger.info(
            "Admin browser connected (%s), total=%d",
            websocket.client.host if websocket.client else "unknown",
            len(self._connections),
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a browser admin connection."""
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(
            "Admin browser disconnected, total=%d",
            len(self._connections),
        )

    @property
    def connection_count(self) -> int:
        """Number of currently connected admin browsers."""
        return len(self._connections)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    async def broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Send an event to all connected admin browsers.

        Parameters
        ----------
        event_type:
            Dot-colon event name, e.g. ``"device:status"``, ``"post:result"``.
        data:
            Arbitrary JSON-serializable payload.

        Dead clients are removed silently — one broken socket never prevents
        delivery to the remaining browsers.
        """
        if not self._connections:
            return

        message = json.dumps(
            {
                "type": event_type,
                "ts": int(time.time() * 1000),
                "data": data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        # Snapshot connections under the lock to iterate safely
        async with self._lock:
            snapshot = set(self._connections)

        dead: list[WebSocket] = []
        for ws in snapshot:
            try:
                await ws.send_text(message)
            except Exception:
                logger.debug("Removing dead admin connection")
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)
