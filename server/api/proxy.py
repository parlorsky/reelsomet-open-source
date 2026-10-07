"""Proxy API: manage mobile proxy assignment per device."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_bridge, get_config, get_db_session, get_ws_manager, require_auth
from server.models import Device
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proxy", tags=["proxy"])


class ProxyAssignment(BaseModel):
    device_id: int
    proxy_port: int  # VPS relay port (9001-9010)


class ProxyConfig(BaseModel):
    upstream_host: str = "us.decodo.com"
    upstream_user: str = ""
    upstream_pass: str = ""
    base_port: int = 10001
    relay_base_port: int = 9001
    endpoints: int = 10


@router.get("/status")
async def proxy_status(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Show proxy assignment status for all devices."""
    devices = (await session.execute(
        select(Device).where(Device.is_active == True).order_by(Device.id),  # noqa: E712
    )).scalars().all()

    result = []
    for d in devices:
        is_online = ws.is_online(d.id)
        result.append({
            "device_id": d.id,
            "device_name": d.name or f"Device {d.id}",
            "is_online": is_online,
            "proxy_port": getattr(d, '_proxy_port', None),
        })

    return {
        "vps_ip": config.domain,
        "relay_ports": list(range(9001, 9011)),
        "devices": result,
    }


@router.post("/assign")
async def assign_proxy(
    body: ProxyAssignment,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Assign a proxy port to a device and apply it."""
    device = await session.get(Device, body.device_id)
    if device is None or not device.is_active:
        raise HTTPException(404, "Device not found")
    if not ws.is_online(body.device_id):
        raise HTTPException(400, f"Device {body.device_id} is offline")

    host = config.domain
    port = body.proxy_port

    try:
        result = await bridge.set_proxy(body.device_id, host, port)
    except Exception as exc:
        raise HTTPException(500, f"Failed to set proxy: {exc}")

    logger.info("Proxy assigned: device %d -> %s:%d", body.device_id, host, port)
    return {
        "device_id": body.device_id,
        "proxy": f"{host}:{port}",
        "result": result,
    }


@router.post("/clear/{device_id}")
async def clear_proxy(
    device_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Remove proxy from a device."""
    device = await session.get(Device, device_id)
    if device is None or not device.is_active:
        raise HTTPException(404, "Device not found")
    if not ws.is_online(device_id):
        raise HTTPException(400, f"Device {device_id} is offline")

    try:
        result = await bridge.clear_proxy(device_id)
    except Exception as exc:
        raise HTTPException(500, f"Failed to clear proxy: {exc}")

    logger.info("Proxy cleared for device %d", device_id)
    return {"device_id": device_id, "result": result}
