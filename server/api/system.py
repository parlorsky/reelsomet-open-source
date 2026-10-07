"""System management API: version info, update check/apply."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from server.dependencies import require_auth
from server.updater import apply_update, check_for_updates, get_system_info

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/info")
async def system_info(
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Return server version, uptime, Python version, and platform."""
    return get_system_info()


@router.post("/update/check")
@router.get("/update/check")
async def update_check(
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Check whether new commits are available upstream.

    Returns ``{"available": bool, "commits": int, "summary": str}``.
    """
    return await check_for_updates()


@router.post("/update/apply")
async def update_apply(
    _user: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Pull latest code, rebuild protected modules, and restart the service.

    The server will restart approximately 3 seconds after a successful response.
    Returns ``{"success": bool, "message": str, "restart_scheduled": bool}``.
    """
    return await apply_update()
