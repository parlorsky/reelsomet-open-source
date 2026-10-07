"""Logging handler that forwards log entries to admin browsers via WebSocket."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.ws.admin_broadcaster import AdminBroadcaster


class WebSocketLogHandler(logging.Handler):
    """Forwards log records to connected admin browsers via WebSocket.

    The handler schedules a broadcast on the running event loop, so it is
    safe to call from both sync and async code.  If no event loop is running
    (e.g. during startup), the record is silently dropped.

    Parameters
    ----------
    broadcaster:
        The ``AdminBroadcaster`` instance that manages browser connections.
    level:
        Minimum log level to forward (default: ``logging.INFO``).
    """

    def __init__(self, broadcaster: AdminBroadcaster, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._broadcaster = broadcaster

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record and schedule a broadcast to admin browsers."""
        if self._broadcaster.connection_count == 0:
            return

        try:
            entry: dict[str, Any] = {
                "logger": record.name,
                "level": record.levelname,
                "message": self.format(record),
                "timestamp": record.created,
            }
        except Exception:
            self.handleError(record)
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running — drop silently
            return

        loop.create_task(self._safe_broadcast(entry))

    async def _safe_broadcast(self, entry: dict[str, Any]) -> None:
        """Broadcast with exception swallowing so log handler never crashes."""
        try:
            await self._broadcaster.broadcast("log:entry", entry)
        except Exception:
            # Logging inside a log handler can recurse; just swallow.
            pass
