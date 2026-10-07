"""Devices API: CRUD operations and connectivity checks."""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig, save_config
from server.dependencies import (
    get_admin_broadcaster,
    get_bridge,
    get_config,
    get_db_session,
    get_ws_manager,
    require_auth,
)
from server.models import AccountDevice, Device
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

router = APIRouter(prefix="/api/devices", tags=["devices"])


def _normalize_required_text(value: str, field_name: str) -> str:
    """Trim a required text field and reject blank values."""
    normalized = value.strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be empty",
        )
    return normalized


def _validate_port(value: int) -> int:
    """Reject invalid TCP port values before persisting a device endpoint."""
    if value < 1 or value > 65535:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Port must be between 1 and 65535",
        )
    return value


class DeviceCreate(BaseModel):
    device_id: str
    name: str = ""
    ip_address: str = ""
    port: int = 8080


class DeviceSelfUpdate(BaseModel):
    apk_url: str | None = None
    sha256: str | None = None
    request_id: str | None = None


class DeviceForceRestart(BaseModel):
    reason: str = "manual"


class DeviceScreenKeycode(BaseModel):
    key: str


class DeviceScreenInput(BaseModel):
    kind: str = "tap"
    x: float
    y: float
    x2: float | None = None
    y2: float | None = None
    duration_ms: int | None = None


class DeviceScreenText(BaseModel):
    text: str


class DeviceDebugStageVideoMediaStore(BaseModel):
    phone_video_id: int | None = None
    video_id: int | None = None
    username: str | None = None
    filename: str | None = None
    limit: int = 20


class DeviceRedditDebugStageMedia(BaseModel):
    filename: str
    url: str
    batch_id: str = "debug_reddit_stage"
    limit: int = 20


class DevicePhoneQueueClear(BaseModel):
    account_username: str | None = None


_ACTIVE_PHONE_VIDEO_STATUSES = {"PENDING", "SCHEDULED", "POSTING", "QUEUED"}


def _build_public_url(config: VPSConfig) -> str:
    """Build the phone-reachable public URL for this VPS."""
    if not config.domain:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="server.domain is not configured",
        )
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
        return f"http://{config.domain}:8443"
    return f"https://{config.domain}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _require_online_device(
    *,
    device_pk: int,
    session: AsyncSession,
    ws: DeviceConnectionManager,
) -> Device:
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")
    return device


def _phone_video_id(video: dict[str, Any]) -> int | None:
    raw = video.get("id") or video.get("videoId") or video.get("phoneVideoId")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _phone_video_account(video: dict[str, Any]) -> str:
    raw = (
        video.get("accountUsername")
        or video.get("account_username")
        or video.get("username")
        or ""
    )
    return str(raw).strip()


@router.get("")
async def list_devices(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
) -> list[dict[str, Any]]:
    """Return all active devices."""
    result = await session.execute(
        select(Device).where(Device.is_active == True).order_by(Device.id),  # noqa: E712
    )
    devices = result.scalars().all()

    # Pre-fetch account counts per device — only active accounts
    from sqlalchemy import func as sa_func
    from server.models import Account
    acct_counts_result = await session.execute(
        select(AccountDevice.device_id, sa_func.count(AccountDevice.id))
        .join(Account, Account.username == AccountDevice.account_username)
        .where(Account.is_active == True)  # noqa: E712
        .group_by(AccountDevice.device_id),
    )
    acct_counts: dict[int, int] = {row[0]: row[1] for row in acct_counts_result}

    out = []
    for d in devices:
        is_online = ws.is_online(d.id)
        ws_info = ws.get_device_info(d.id) if is_online else {}
        model = ws_info.get("model") or d.device_model
        battery = ws_info.get("batteryLevel")
        last_seen_val = d.last_seen_at.isoformat() if d.last_seen_at else None
        data = {
            "id": d.id,
            "device_id": d.device_id,
            "adb_id": d.device_id,
            "name": d.name,
            "ip_address": d.ip_address,
            "ip": d.ip_address,
            "port": d.port,
            "device_model": model,
            "model": model,
            "android_version": ws_info.get("androidVersion") or d.android_version,
            "app_version": ws_info.get("appVersion"),
            "is_active": d.is_active,
            "status": "online" if is_online else d.status,
            "is_online": is_online,
            "battery_level": battery,
            "battery": battery,
            "active_mode": ws_info.get("activeMode"),
            "accessibility_connected": ws_info.get("accessibilityConnected"),
            "last_seen": last_seen_val,
            "accounts_count": acct_counts.get(d.id, 0),
        }
        out.append(data)
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def add_device(
    body: DeviceCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Register a new device."""
    normalized_device_id = _normalize_required_text(body.device_id, "Device ID")
    normalized_name = _normalize_required_text(body.name, "Name")
    normalized_ip_address = _normalize_required_text(body.ip_address, "IP address")
    normalized_port = _validate_port(body.port)

    existing = (await session.execute(
        select(Device).where(Device.device_id == normalized_device_id),
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device already exists")

    device = Device(
        device_id=normalized_device_id,
        name=normalized_name,
        ip_address=normalized_ip_address,
        port=normalized_port,
    )
    session.add(device)
    await session.flush()
    # Commit before WS broadcast so admin clients never receive an
    # "added" event for a device that gets rolled back (Codex iter 9
    # bug hunt 2026-04-14, same pattern as accounts.py).
    await session.commit()
    await broadcaster.broadcast("device:inventory", {
        "device_id": device.id,
        "action": "added",
    })
    return {
        "id": device.id,
        "device_id": device.device_id,
        "name": device.name,
        "ip_address": device.ip_address,
        "port": device.port,
        "is_active": device.is_active,
        "status": device.status,
    }


@router.get("/{device_pk}")
async def get_device(
    device_pk: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
) -> dict[str, Any]:
    """Return a single device by primary key."""
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    return {
        "id": device.id,
        "device_id": device.device_id,
        "name": device.name,
        "ip_address": device.ip_address,
        "port": device.port,
        "device_model": device.device_model,
        "android_version": device.android_version,
        "is_active": device.is_active,
        "status": device.status,
        "is_online": ws.is_online(device.id),
    }


@router.delete("/{device_pk}")
async def delete_device(
    device_pk: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    broadcaster: AdminBroadcaster = Depends(get_admin_broadcaster),
) -> dict[str, Any]:
    """Soft-delete a device (set is_active=False)."""
    device = (await session.execute(
        select(Device).where(Device.id == device_pk),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    linked_accounts = (await session.execute(
        select(AccountDevice).where(AccountDevice.device_id == device.id),
    )).scalars().all()
    unlinked_count = len(linked_accounts)
    for link in linked_accounts:
        await session.delete(link)

    device.is_active = False
    await session.flush()
    # Commit before broadcast (Codex iter 9 bug hunt 2026-04-14).
    await session.commit()
    await broadcaster.broadcast("device:inventory", {
        "device_id": device.id,
        "action": "removed",
        "unlinked_accounts": unlinked_count,
    })
    return {"id": device.id, "is_active": False, "unlinked_accounts": unlinked_count}


@router.post("/{device_pk}/ping")
async def ping_device(
    device_pk: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Ping a device via WebSocket and measure round-trip latency."""
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")

    if not ws.is_online(device_pk):
        return {"device_id": device_pk, "online": False}

    t0 = time.monotonic()
    ok = await bridge.ping(device_pk)
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if not ok:
        return {"device_id": device_pk, "online": False}
    return {"device_id": device_pk, "online": True, "latency_ms": elapsed_ms}


@router.get("/{device_pk}/phone-videos")
async def phone_videos(
    device_pk: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    all_videos: bool = Query(False, alias="all"),
) -> dict[str, Any]:
    """Return the phone-local video queue via WebSocket."""
    await _require_online_device(device_pk=device_pk, session=session, ws=ws)
    result = (
        await bridge.get_all_videos(device_pk)
        if all_videos
        else await bridge.get_pending_videos(device_pk)
    )
    return {
        "device_id": device_pk,
        "all": all_videos,
        "videos": result.get("videos", []),
    }


@router.post("/{device_pk}/phone-videos/clear")
async def clear_phone_videos(
    device_pk: int,
    body: DevicePhoneQueueClear | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Cancel active phone-local videos, optionally for one account only."""
    await _require_online_device(device_pk=device_pk, session=session, ws=ws)
    account_filter = (body.account_username if body else None)
    account_filter = account_filter.strip() if account_filter else None

    result = await bridge.get_pending_videos(device_pk)
    videos = result.get("videos", [])
    cancelled: list[int] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for video in videos:
        if not isinstance(video, dict):
            continue
        phone_video_id = _phone_video_id(video)
        if phone_video_id is None:
            skipped.append({"reason": "missing_video_id", "video": video})
            continue
        video_account = _phone_video_account(video)
        if account_filter and video_account != account_filter:
            skipped.append({"id": phone_video_id, "reason": "account_filter"})
            continue
        video_status = str(video.get("status", "")).upper()
        if video_status not in _ACTIVE_PHONE_VIDEO_STATUSES:
            skipped.append({"id": phone_video_id, "reason": "inactive_status", "status": video_status})
            continue
        try:
            cancel_result = await bridge.cancel_video(device_pk, phone_video_id)
        except Exception as exc:
            errors.append({"id": phone_video_id, "error": str(exc)})
            continue
        if cancel_result.get("status") in {None, "ok", "cancelled", "aborted"}:
            cancelled.append(phone_video_id)
        else:
            errors.append({
                "id": phone_video_id,
                "error": cancel_result.get("error") or cancel_result,
            })

    return {
        "device_id": device_pk,
        "account_username": account_filter,
        "seen": len(videos),
        "cancelled": cancelled,
        "skipped": skipped,
        "errors": errors,
    }


@router.post("/{device_pk}/force-restart")
async def force_restart_device(
    device_pk: int,
    body: DeviceForceRestart | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Ask an online phone app to kill and respawn its own process."""
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")

    reason = (body.reason if body else "manual").strip() or "manual"
    await bridge.send_app_force_restart(device_pk, reason)
    return {"device_id": device_pk, "sent": True, "reason": reason}


@router.post("/{device_pk}/screen-keycode")
async def screen_keycode_device(
    device_pk: int,
    body: DeviceScreenKeycode,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Send a remote accessibility key action such as back or home."""
    key = body.key.strip().lower()
    if key not in {"back", "home", "recents", "recent_apps", "notifications", "quick_settings", "power_dialog", "lock_screen"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported key")

    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")

    result = await bridge.screen_keycode(device_pk, key)
    return {"device_id": device_pk, "key": key, "device_result": result}


@router.post("/{device_pk}/screen-input")
async def screen_input_device(
    device_pk: int,
    body: DeviceScreenInput,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Send a remote tap, long-press, or swipe accessibility gesture."""
    kind = body.kind.strip().lower()
    if kind not in {"tap", "longpress", "long_press", "swipe"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported input kind")
    if kind == "swipe" and (body.x2 is None or body.y2 is None):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Swipe requires x2 and y2")

    await _require_online_device(device_pk=device_pk, session=session, ws=ws)

    payload: dict[str, Any] = {
        "kind": kind,
        "x": body.x,
        "y": body.y,
    }
    if body.x2 is not None:
        payload["x2"] = body.x2
    if body.y2 is not None:
        payload["y2"] = body.y2
    if body.duration_ms is not None:
        payload["durationMs"] = body.duration_ms

    result = await bridge.screen_input(device_pk, payload)
    return {"device_id": device_pk, "input": payload, "device_result": result}


@router.post("/{device_pk}/screen-text")
async def screen_text_device(
    device_pk: int,
    body: DeviceScreenText,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Set text in the currently focused editable field on the phone."""
    if not body.text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Text cannot be empty")

    await _require_online_device(device_pk=device_pk, session=session, ws=ws)

    result = await bridge.screen_text(device_pk, body.text)
    return {"device_id": device_pk, "text": body.text, "device_result": result}


@router.post("/{device_pk}/self-update")
async def self_update_device(
    device_pk: int,
    body: DeviceSelfUpdate | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Send the current phone APK to an online device via WebSocket."""
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")

    body = body or DeviceSelfUpdate()
    apk_url = body.apk_url or f"{_build_public_url(config)}/api/apk/download"
    apk_path = Path(config.data_dir).parent / "app" / "apk" / "reelsomet.apk"
    sha256 = body.sha256 or _sha256_file(apk_path)
    request_id = body.request_id or f"self-update-{device_pk}-{int(time.time())}"

    result = await bridge.self_update(
        device_pk,
        apk_url=apk_url,
        sha256=sha256,
        request_id=request_id,
    )
    return {
        "device_id": device_pk,
        "apk_url": apk_url,
        "sha256": sha256,
        "request_id": request_id,
        "device_result": result,
    }


@router.get("/{device_pk}/debug-a11y-tree")
async def debug_a11y_tree(
    device_pk: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Return the current accessibility tree from an online device."""
    device = (await session.execute(
        select(Device).where(
            Device.id == device_pk,
            Device.is_active == True,  # noqa: E712
        ),
    )).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Device not found")
    if not ws.is_online(device_pk):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Device is offline")
    return await bridge.debug_a11y_tree(device_pk)


@router.get("/{device_pk}/debug-media-store")
async def debug_media_store(
    device_pk: int,
    limit: int = Query(20, ge=1, le=100),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Return Reelsomet-staged MediaStore rows from an online phone."""
    await _require_online_device(device_pk=device_pk, session=session, ws=ws)
    return await bridge.debug_media_store(device_pk, limit=limit)


@router.post("/{device_pk}/debug-stage-video-media-store")
async def debug_stage_video_media_store(
    device_pk: int,
    body: DeviceDebugStageVideoMediaStore,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Stage one phone-local video into MediaStore without opening Instagram."""
    await _require_online_device(device_pk=device_pk, session=session, ws=ws)
    payload: dict[str, Any] = {"limit": body.limit}
    phone_video_id = body.phone_video_id or body.video_id
    if phone_video_id is not None:
        payload["phoneVideoId"] = phone_video_id
    if body.username:
        payload["username"] = body.username
    if body.filename:
        payload["filename"] = body.filename
    return await bridge.debug_stage_video_media_store(device_pk, payload)


@router.post("/{device_pk}/reddit/debug-stage-media")
async def reddit_debug_stage_media(
    device_pk: int,
    body: DeviceRedditDebugStageMedia,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Stage one Reddit image into MediaStore without opening Reddit or posting."""
    await _require_online_device(device_pk=device_pk, session=session, ws=ws)
    return await bridge.reddit_debug_stage_media(
        device_pk,
        {
            "filename": body.filename,
            "url": body.url,
            "batchId": body.batch_id,
            "limit": body.limit,
        },
    )


@router.post("/token/generate")
async def generate_device_token(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict:
    """Generate a new device token and add it to config."""
    max_id = max(config.device_tokens.keys()) if config.device_tokens else 0
    new_id = max_id + 1
    new_token = secrets.token_hex(16)
    config.device_tokens[new_id] = new_token
    save_config(config)
    return {"device_id": new_id, "token": new_token}
