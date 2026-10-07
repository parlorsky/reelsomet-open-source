"""FastAPI dependencies for injection into route handlers."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth import get_current_user
from server.config import VPSConfig
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


def get_config(request: Request) -> VPSConfig:
    """Retrieve the VPSConfig instance stored in app.state."""
    return request.app.state.config


def get_ws_manager(request: Request) -> DeviceConnectionManager:
    """Retrieve the WebSocket connection manager from app.state."""
    return request.app.state.ws_manager


def get_admin_broadcaster(request: Request) -> AdminBroadcaster:
    """Retrieve the AdminBroadcaster instance from app.state."""
    return request.app.state.admin_broadcaster


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """Yield an async SQLAlchemy session from the engine stored in app.state.

    The session is committed on success and rolled back on exception.
    Usage::

        @router.get("/items")
        async def list_items(session: AsyncSession = Depends(get_db_session)):
            ...
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_bridge(request: Request) -> DeviceBridge:
    """Retrieve a DeviceBridge wrapping the WebSocket connection manager."""
    return DeviceBridge(request.app.state.ws_manager)


async def require_auth(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Convenience alias: ensure the request has a valid JWT."""
    return user
