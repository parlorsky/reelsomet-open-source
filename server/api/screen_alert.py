"""Internal device-originated screen alert endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth import verify_device_token
from server.config import VPSConfig
from server.dependencies import get_config, get_db_session
from server.models import Device

router = APIRouter(prefix="/api/internal", tags=["internal"])


class ScreenAlertRequest(BaseModel):
    message: str


async def get_current_device(
    request: Request,
    config: VPSConfig = Depends(get_config),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    verified_id = verify_device_token(auth_header[7:], config)
    if verified_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    device = (await session.execute(
        select(Device).where(Device.id == verified_id),
    )).scalar_one_or_none()
    device_name = (
        (device.name if device and device.name else None)
        or (device.device_model if device and device.device_model else None)
        or f"Device {verified_id}"
    )
    return {"id": verified_id, "name": device_name}


@router.post("/screen-alert")
async def screen_alert(
    body: ScreenAlertRequest,
    request: Request,
    device: dict[str, Any] = Depends(get_current_device),
) -> dict[str, bool]:
    telegram_bot = getattr(request.app.state, "telegram_bot", None)
    if telegram_bot is not None:
        text = f"[REMOTE ALERT] Device {device['id']} ({device['name']}): {body.message}"
        await telegram_bot.send_notification(text, parse_mode=None)
    return {"ok": True}
