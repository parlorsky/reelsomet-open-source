"""Helpers for device-backed cancel and abort operations."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import AccountDevice, Device, Video
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager


def command_succeeded(result: dict[str, Any] | None) -> bool:
    """Treat missing status as success for backward-compatible device payloads."""
    if result is None:
        return False
    status_value = result.get("status")
    return status_value in (None, "ok", "cancelled", "aborted")


async def resolve_primary_device_id(
    session: AsyncSession,
    account_username: str,
) -> int | None:
    """Return the primary active linked device for an account, if any."""
    return (
        await session.execute(
            select(AccountDevice.device_id)
            .join(Device, Device.id == AccountDevice.device_id)
            .where(AccountDevice.account_username == account_username)
            .where(Device.is_active == True)  # noqa: E712
            .order_by(AccountDevice.is_primary.desc())
            .limit(1),
        )
    ).scalar_one_or_none()


async def device_is_active(
    session: AsyncSession,
    device_id: int,
) -> bool:
    """Return whether a device row exists and is still active."""
    return (
        await session.execute(
            select(Device.id).where(
                Device.id == device_id,
                Device.is_active == True,  # noqa: E712
            ),
        )
    ).scalar_one_or_none() is not None


async def abort_engagement_on_device(
    *,
    account_username: str,
    session: AsyncSession,
    bridge: DeviceBridge,
    ws: DeviceConnectionManager,
    device_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Abort engagement on the device and return (device_id, error)."""
    resolved_device_id = device_id
    if resolved_device_id is not None:
        if not await device_is_active(session, resolved_device_id):
            return resolved_device_id, "Assigned device is inactive"
    else:
        resolved_device_id = await resolve_primary_device_id(session, account_username)
    if resolved_device_id is None:
        return None, "No active device linked to account"
    if not ws.is_online(resolved_device_id):
        return resolved_device_id, "Device offline"

    try:
        result = await bridge.abort_engagement(resolved_device_id)
    except Exception as exc:
        return resolved_device_id, f"Abort command failed: {exc}"

    if not command_succeeded(result):
        return resolved_device_id, result.get("error") or "Device rejected abort request"

    return resolved_device_id, None


async def cancel_video_on_device(
    *,
    video: Video,
    session: AsyncSession,
    bridge: DeviceBridge,
    ws: DeviceConnectionManager,
) -> tuple[int | None, str | None]:
    """Cancel a phone-side queued video and return (device_id, error)."""
    device_id = video.device_id
    if device_id is not None:
        if not await device_is_active(session, device_id):
            return device_id, "Assigned device is inactive"
    else:
        device_id = await resolve_primary_device_id(session, video.account_username)
    if device_id is None:
        return None, "No active device linked to account"
    if video.phone_video_id is None:
        return device_id, "Video has not been assigned a phone-side ID yet"
    if not ws.is_online(device_id):
        return device_id, "Device offline"

    try:
        result = await bridge.cancel_video(device_id, video.phone_video_id)
    except Exception as exc:
        return device_id, f"Cancel command failed: {exc}"

    if not command_succeeded(result):
        return device_id, result.get("error") or "Device rejected cancel request"

    return device_id, None
