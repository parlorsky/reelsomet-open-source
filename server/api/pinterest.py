"""Pinterest API: isolated accounts, boards, pins, imports, and scheduler settings."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, require_auth
from server.dependencies import get_bridge, get_ws_manager
from server.models import (
    Device,
    PinterestAccount,
    PinterestAsset,
    PinterestBoard,
    PinterestImport,
    PinterestPin,
    PinterestPostAttempt,
    PinterestSchedulerSettings,
    PinterestTaskEvent,
)
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager
from server.pinterest.import_service import (
    PinterestImportError,
    PinterestImportFile,
    import_manifest,
)
from server.pinterest.assets import (
    asset_media_type,
    stage_payload,
    validate_asset_file,
    verify_pinterest_asset_token,
)
from server.pinterest.manifest import ManifestValidationError, parse_pinterest_manifest

router = APIRouter(prefix="/api/pinterest", tags=["pinterest"])


class SchedulerPatch(BaseModel):
    enabled: bool | None = None
    timezone: str | None = None
    target_pins_per_day: int | None = Field(default=None, ge=0)
    min_pins_per_day: int | None = Field(default=None, ge=0)
    max_pins_per_day: int | None = Field(default=None, ge=0)
    posting_windows: list[dict[str, str]] | None = None
    min_gap_minutes: int | None = Field(default=None, ge=0)
    jitter_minutes: int | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=0)
    retry_delay_minutes: int | None = Field(default=None, ge=0)
    pause_after_failures: int | None = Field(default=None, ge=0)
    device_conflict_policy: str | None = None
    reelsomet_guard_minutes: int | None = Field(default=None, ge=0)
    safe_mode_enabled: bool | None = None


class PinterestDeviceCommand(BaseModel):
    account_username: str | None = None
    task_id: str | None = None
    trace_id: str | None = None


class PinterestTaskCommand(BaseModel):
    task_id: str | None = None
    trace_id: str | None = None


class PinterestStageAssetsCommand(BaseModel):
    account_id: int | None = None
    pin_ids: list[int] | None = None
    force: bool = False
    limit: int = Field(default=100, ge=1, le=200)


@router.get("/accounts")
async def list_pinterest_accounts(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PinterestAccount, PinterestSchedulerSettings)
            .outerjoin(PinterestSchedulerSettings, PinterestSchedulerSettings.account_id == PinterestAccount.id)
            .order_by(PinterestAccount.username)
        )
    ).all()
    return [_account_payload(account, settings) for account, settings in rows]


@router.patch("/accounts/{account_id}/scheduler")
async def patch_pinterest_scheduler(
    account_id: int,
    body: SchedulerPatch,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    account = await session.get(PinterestAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pinterest account not found")

    settings = (
        await session.execute(
            select(PinterestSchedulerSettings).where(PinterestSchedulerSettings.account_id == account_id)
        )
    ).scalar_one_or_none()
    if settings is None:
        settings = PinterestSchedulerSettings(account_id=account_id)
        session.add(settings)
        await session.flush()

    update = body.model_dump(exclude_unset=True)
    posting_windows = update.pop("posting_windows", None)
    for field_name, value in update.items():
        setattr(settings, field_name, value)
    if posting_windows is not None:
        settings.posting_windows_json = json.dumps(posting_windows, ensure_ascii=False)
    await session.flush()
    return _scheduler_payload(settings)


@router.get("/boards")
async def list_pinterest_boards(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PinterestBoard, PinterestAccount)
            .join(PinterestAccount, PinterestAccount.id == PinterestBoard.account_id)
            .order_by(PinterestAccount.username, PinterestBoard.name)
        )
    ).all()
    return [_board_payload(board, account) for board, account in rows]


@router.get("/pins")
async def list_pinterest_pins(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PinterestPin, PinterestAccount, PinterestBoard, PinterestAsset)
            .join(PinterestAccount, PinterestAccount.id == PinterestPin.account_id)
            .join(PinterestBoard, PinterestBoard.id == PinterestPin.board_id)
            .join(PinterestAsset, PinterestAsset.id == PinterestPin.asset_id)
            .order_by(PinterestPin.priority.desc(), PinterestPin.order_index, PinterestPin.created_at)
        )
    ).all()
    return [_pin_payload(pin, account, board, asset) for pin, account, board, asset in rows]


@router.get("/imports")
async def list_pinterest_imports(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PinterestImport).order_by(desc(PinterestImport.created_at), desc(PinterestImport.id))
        )
    ).scalars().all()
    return [await _import_payload(session, row) for row in rows]


@router.get("/events")
async def list_pinterest_events(
    trace_id: str | None = None,
    task_id: str | None = None,
    pin_id: int | None = None,
    board_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    stmt = select(PinterestTaskEvent)
    if trace_id:
        stmt = stmt.where(PinterestTaskEvent.trace_id == trace_id)
    if task_id:
        stmt = stmt.where(PinterestTaskEvent.task_id == task_id)
    if pin_id is not None:
        stmt = stmt.where(PinterestTaskEvent.pin_id == pin_id)
    if board_id is not None:
        stmt = stmt.where(PinterestTaskEvent.board_id == board_id)
    rows = (
        await session.execute(
            stmt.order_by(desc(PinterestTaskEvent.ts_ms), desc(PinterestTaskEvent.id)).limit(limit)
        )
    ).scalars().all()
    return [_event_payload(row) for row in rows]


@router.post("/devices/{device_id}/health-check")
async def run_pinterest_health_check(
    device_id: int,
    body: PinterestDeviceCommand | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    await _require_online_device(session, ws, device_id)
    payload = _device_command_payload(body, "pinterest_health")
    try:
        result = await bridge.pinterest_health_check(device_id, payload)
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "device_command_timeout") from exc
    except RuntimeError as exc:
        result = _device_error_payload(exc)
    return {"device_id": device_id, "payload": payload, "device_result": result}


@router.post("/devices/{device_id}/bootstrap-permissions")
async def run_pinterest_bootstrap_permissions(
    device_id: int,
    body: PinterestDeviceCommand | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    await _require_online_device(session, ws, device_id)
    payload = _device_command_payload(body, "pinterest_bootstrap")
    try:
        result = await bridge.pinterest_bootstrap_permissions(device_id, payload)
    except TimeoutError as exc:
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "device_command_timeout") from exc
    except RuntimeError as exc:
        result = _device_error_payload(exc)
    return {"device_id": device_id, "payload": payload, "device_result": result}


@router.post("/devices/{device_id}/assets/stage")
async def stage_pinterest_assets(
    device_id: int,
    body: PinterestStageAssetsCommand | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    await _require_online_device(session, ws, device_id)
    body = body or PinterestStageAssetsCommand()
    rows = await _select_assets_for_staging(session, device_id=device_id, body=body)
    assets = _dedupe_assets([asset for asset, _pin in rows])
    if not assets:
        return {
            "status": "ok",
            "device_id": device_id,
            "asset_count": 0,
            "batch_id": None,
            "device_result": {"status": "skipped", "success": True, "message": "no_assets_to_stage"},
        }

    batch_id = f"pinterest_assets_{int(time.time())}"
    payload_assets = [stage_payload(config, asset, idx) for idx, asset in enumerate(assets)]
    result = await bridge.push_image_assets(device_id, batch_id=batch_id, assets=payload_assets)
    now = _utcnow()
    if _device_result_success(result):
        for asset in assets:
            asset.phone_staged_at = now
            asset.last_error = None
    else:
        error_message = _device_error_message(result)
        for asset in assets:
            asset.last_error = error_message
    await session.commit()
    return {
        "status": "ok" if _device_result_success(result) else "failed",
        "device_id": device_id,
        "asset_count": len(assets),
        "batch_id": batch_id,
        "device_result": result,
    }


@router.post("/devices/{device_id}/boards/{board_id}/ensure")
async def run_pinterest_ensure_board(
    device_id: int,
    board_id: int,
    body: PinterestTaskCommand | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    await _require_online_device(session, ws, device_id)
    row = (
        await session.execute(
            select(PinterestBoard, PinterestAccount)
            .join(PinterestAccount, PinterestAccount.id == PinterestBoard.account_id)
            .where(PinterestBoard.id == board_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pinterest board not found")
    board, account = row

    task_id, trace_id = _task_ids(body, f"pinterest-board-{board.id}")
    now = _utcnow()
    attempt = PinterestPostAttempt(
        task_id=task_id,
        trace_id=trace_id,
        task_type="pinterest.ensure_board",
        status="running",
        account_id=account.id,
        board_id=board.id,
        device_id=device_id,
        started_at=now,
    )
    board.status = "creating"
    board.last_create_attempt_at = now
    session.add(attempt)
    await session.flush()
    await session.commit()

    payload = _board_command_payload(task_id=task_id, trace_id=trace_id, account=account, board=board)
    try:
        result = await bridge.pinterest_ensure_board(device_id, payload)
    except TimeoutError as exc:
        await _finish_board_attempt(session, attempt, board, _device_timeout_payload(), success=False)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "device_command_timeout") from exc
    except RuntimeError as exc:
        result = _device_error_payload(exc)
    await _finish_board_attempt(session, attempt, board, result, success=_device_result_success(result))
    return {"device_id": device_id, "payload": payload, "device_result": result}


@router.post("/devices/{device_id}/pins/{pin_id}/publish")
async def run_pinterest_publish_pin(
    device_id: int,
    pin_id: int,
    body: PinterestTaskCommand | None = None,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    await _require_online_device(session, ws, device_id)
    row = (
        await session.execute(
            select(PinterestPin, PinterestAccount, PinterestBoard, PinterestAsset)
            .join(PinterestAccount, PinterestAccount.id == PinterestPin.account_id)
            .join(PinterestBoard, PinterestBoard.id == PinterestPin.board_id)
            .join(PinterestAsset, PinterestAsset.id == PinterestPin.asset_id)
            .where(PinterestPin.id == pin_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pinterest pin not found")
    pin, account, board, asset = row
    if not asset.phone_storage_path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Pinterest asset is not staged on phone")

    task_id, trace_id = _task_ids(body, f"pinterest-pin-{pin.id}")
    now = _utcnow()
    attempt = PinterestPostAttempt(
        task_id=task_id,
        trace_id=trace_id,
        task_type="pinterest.publish_pin",
        status="running",
        account_id=account.id,
        board_id=board.id,
        pin_id=pin.id,
        device_id=device_id,
        started_at=now,
    )
    pin.status = "posting"
    pin.posting_started_at = now
    pin.attempt_count = int(pin.attempt_count or 0) + 1
    session.add(attempt)
    await session.flush()
    pin.last_attempt_id = attempt.id
    await session.commit()

    payload = _pin_command_payload(
        task_id=task_id,
        trace_id=trace_id,
        account=account,
        board=board,
        asset=asset,
        pin=pin,
    )
    try:
        result = await bridge.pinterest_publish_pin(device_id, payload)
    except TimeoutError as exc:
        await _finish_pin_attempt(session, attempt, pin, _device_timeout_payload(), success=False)
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "device_command_timeout") from exc
    except RuntimeError as exc:
        result = _device_error_payload(exc)
    await _finish_pin_attempt(session, attempt, pin, result, success=_device_result_success(result))
    return {"device_id": device_id, "payload": payload, "device_result": result}


@router.post("/imports")
async def create_pinterest_import(
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    try:
        payload = json.loads(manifest)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid manifest JSON: {exc.msg}") from exc

    try:
        parsed_manifest = parse_pinterest_manifest(payload)
    except ManifestValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    upload_map: dict[str, PinterestImportFile] = {}
    for upload in files:
        content = await upload.read()
        if upload.filename in upload_map:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Duplicate file upload: {upload.filename}")
        upload_map[upload.filename] = PinterestImportFile(
            filename=upload.filename,
            content=content,
            content_type=upload.content_type,
        )

    try:
        result = await import_manifest(session, config, parsed_manifest, upload_map)
    except PinterestImportError as exc:
        message = str(exc)
        status_code = status.HTTP_409_CONFLICT if "already exists" in message else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code, message) from exc

    return {
        "id": result.import_row.id,
        "import_id": result.import_row.import_id,
        "status": result.import_row.status,
        "created_accounts_count": result.created_accounts_count,
        "created_boards_count": result.created_boards_count,
        "created_assets_count": result.created_assets_count,
        "created_pins_count": result.created_pins_count,
    }


@router.get("/assets/{asset_id}/download/{filename}")
async def download_pinterest_asset(
    asset_id: int,
    filename: str,
    token: str,
    expires: int,
    session: AsyncSession = Depends(get_db_session),
    config: VPSConfig = Depends(get_config),
):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid filename")
    if not verify_pinterest_asset_token(
        asset_id=asset_id,
        filename=filename,
        expires=expires,
        token=token,
        config=config,
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired token")

    asset = await session.get(PinterestAsset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pinterest asset not found")
    try:
        file_path = validate_asset_file(asset, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asset file not found") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return FileResponse(str(file_path), media_type=asset_media_type(file_path), filename=file_path.name)


def _account_payload(
    account: PinterestAccount,
    settings: PinterestSchedulerSettings | None,
) -> dict[str, Any]:
    return {
        "id": account.id,
        "username": account.username,
        "display_name": account.display_name,
        "model": account.model,
        "device_id": account.device_id,
        "status": account.status,
        "app_installed": account.app_installed,
        "gallery_permission_granted": account.gallery_permission_granted,
        "observed_account_label": account.observed_account_label,
        "last_error_code": account.last_error_code,
        "last_error_message": account.last_error_message,
        "scheduler": _scheduler_payload(settings),
    }


def _scheduler_payload(settings: PinterestSchedulerSettings | None) -> dict[str, Any]:
    if settings is None:
        return {
            "enabled": False,
            "timezone": "America/New_York",
            "target_pins_per_day": 10,
            "min_pins_per_day": None,
            "max_pins_per_day": None,
            "posting_windows": [],
            "min_gap_minutes": 40,
            "jitter_minutes": 10,
            "max_retries": 3,
            "retry_delay_minutes": 30,
            "pause_after_failures": 3,
            "device_conflict_policy": "wait",
            "reelsomet_guard_minutes": 20,
            "safe_mode_enabled": False,
        }
    return {
        "id": settings.id,
        "account_id": settings.account_id,
        "enabled": settings.enabled,
        "timezone": settings.timezone,
        "target_pins_per_day": settings.target_pins_per_day,
        "min_pins_per_day": settings.min_pins_per_day,
        "max_pins_per_day": settings.max_pins_per_day,
        "posting_windows": json.loads(settings.posting_windows_json or "[]"),
        "min_gap_minutes": settings.min_gap_minutes,
        "jitter_minutes": settings.jitter_minutes,
        "max_retries": settings.max_retries,
        "retry_delay_minutes": settings.retry_delay_minutes,
        "pause_after_failures": settings.pause_after_failures,
        "device_conflict_policy": settings.device_conflict_policy,
        "reelsomet_guard_minutes": settings.reelsomet_guard_minutes,
        "safe_mode_enabled": settings.safe_mode_enabled,
    }


def _board_payload(board: PinterestBoard, account: PinterestAccount) -> dict[str, Any]:
    return {
        "id": board.id,
        "account_id": account.id,
        "account_username": account.username,
        "key": board.key,
        "name": board.name,
        "description": board.description,
        "visibility": board.visibility,
        "status": board.status,
        "pinterest_board_url": board.pinterest_board_url,
        "last_error_code": board.last_error_code,
        "last_error_message": board.last_error_message,
    }


def _pin_payload(
    pin: PinterestPin,
    account: PinterestAccount,
    board: PinterestBoard,
    asset: PinterestAsset,
) -> dict[str, Any]:
    return {
        "id": pin.id,
        "external_id": pin.external_id,
        "account_id": account.id,
        "account_username": account.username,
        "board_id": board.id,
        "board_name": board.name,
        "asset_id": asset.id,
        "asset_filename": asset.original_file,
        "phone_storage_path": asset.phone_storage_path,
        "phone_staged_at": asset.phone_staged_at.isoformat() if asset.phone_staged_at else None,
        "title": pin.title,
        "description": pin.description,
        "priority": pin.priority,
        "order_index": pin.order_index,
        "status": pin.status,
        "attempt_count": pin.attempt_count,
        "next_retry_at": pin.next_retry_at.isoformat() if pin.next_retry_at else None,
        "posted_at": pin.posted_at.isoformat() if pin.posted_at else None,
        "pinterest_pin_url": pin.pinterest_pin_url,
        "last_error_code": pin.last_error_code,
        "last_error_message": pin.last_error_message,
    }


async def _import_payload(session: AsyncSession, row: PinterestImport) -> dict[str, Any]:
    assets_count = await _count(session, PinterestAsset.id, PinterestAsset.import_id == row.id)
    boards_count = await _count(session, PinterestBoard.id, PinterestBoard.source_import_id == row.id)
    pins_count = await _count(session, PinterestPin.id, PinterestPin.source_import_id == row.id)
    return {
        "id": row.id,
        "import_id": row.import_id,
        "model": row.model,
        "platform": row.platform,
        "source_name": row.source_name,
        "status": row.status,
        "assets_count": assets_count,
        "boards_count": boards_count,
        "pins_count": pins_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _count(session: AsyncSession, column: Any, *where: Any) -> int:
    result = await session.execute(select(func.count(column)).where(*where))
    return int(result.scalar_one() or 0)


async def _select_assets_for_staging(
    session: AsyncSession,
    *,
    device_id: int,
    body: PinterestStageAssetsCommand,
) -> list[tuple[PinterestAsset, PinterestPin]]:
    stmt = (
        select(PinterestAsset, PinterestPin)
        .join(PinterestPin, PinterestPin.asset_id == PinterestAsset.id)
        .join(PinterestAccount, PinterestAccount.id == PinterestPin.account_id)
        .where(
            PinterestAccount.device_id == device_id,
            PinterestAsset.status == "ready",
            PinterestPin.status.in_(["ready", "retry_waiting", "failed", "needs_attention"]),
        )
        .order_by(PinterestPin.priority.desc(), PinterestPin.order_index.asc(), PinterestPin.created_at.asc())
        .limit(body.limit)
    )
    if body.account_id is not None:
        stmt = stmt.where(PinterestPin.account_id == body.account_id)
    if body.pin_ids:
        stmt = stmt.where(PinterestPin.id.in_(body.pin_ids))
    if not body.force:
        stmt = stmt.where(PinterestAsset.phone_staged_at.is_(None))
    return list((await session.execute(stmt)).all())


def _dedupe_assets(assets: list[PinterestAsset]) -> list[PinterestAsset]:
    seen: set[int] = set()
    result: list[PinterestAsset] = []
    for asset in assets:
        if asset.id in seen:
            continue
        seen.add(asset.id)
        result.append(asset)
    return result


def _event_payload(row: PinterestTaskEvent) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "event_id": row.event_id,
        "attempt_id": row.attempt_id,
        "task_id": row.task_id,
        "trace_id": row.trace_id,
        "device_id": row.device_id,
        "account_id": row.account_id,
        "board_id": row.board_id,
        "pin_id": row.pin_id,
        "ts_ms": row.ts_ms,
        "fsm": row.fsm_kind,
        "state": row.state,
        "state_entered_at_ms": row.state_entered_at_ms,
        "action": {
            "name": row.action_name,
            "target": row.action_target,
            "result": row.action_result,
        },
        "next_action": {
            "name": row.next_action_name,
            "target": row.next_action_target,
            "scheduled_at": row.next_action_at_ms,
        },
        "screen_activity": row.screen_activity,
        "screen_hash": row.screen_hash,
        "screenshot_id": row.screenshot_id,
        "message": row.message,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    try:
        payload["fields"] = json.loads(row.fields_json or "{}")
    except json.JSONDecodeError:
        payload["fields"] = {}
    return payload


async def _require_online_device(
    session: AsyncSession,
    ws: DeviceConnectionManager,
    device_id: int,
) -> None:
    device = await session.get(Device, device_id)
    if device is None or not device.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    if not ws.is_online(device_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Device is offline")


def _device_command_payload(body: PinterestDeviceCommand | None, prefix: str) -> dict[str, Any]:
    body = body or PinterestDeviceCommand()
    suffix = int(time.time())
    payload: dict[str, Any] = {
        "task_id": body.task_id or f"{prefix}-{suffix}",
        "trace_id": body.trace_id or f"{prefix}-{suffix}-trace",
    }
    if body.account_username:
        payload["account"] = {"username": body.account_username}
    return payload


def _task_ids(body: PinterestTaskCommand | None, prefix: str) -> tuple[str, str]:
    body = body or PinterestTaskCommand()
    suffix = uuid.uuid4().hex[:12]
    task_id = body.task_id or f"{prefix}-{suffix}"
    trace_id = body.trace_id or f"ptrace-{uuid.uuid4().hex}"
    return task_id, trace_id


def _board_command_payload(
    *,
    task_id: str,
    trace_id: str,
    account: PinterestAccount,
    board: PinterestBoard,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "trace_id": trace_id,
        "account": {
            "id": account.id,
            "username": account.username,
        },
        "board": {
            "id": board.id,
            "key": board.key,
            "name": board.name,
            "description": board.description,
            "visibility": board.visibility,
        },
    }


def _pin_command_payload(
    *,
    task_id: str,
    trace_id: str,
    account: PinterestAccount,
    board: PinterestBoard,
    asset: PinterestAsset,
    pin: PinterestPin,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "trace_id": trace_id,
        "account": {
            "id": account.id,
            "username": account.username,
        },
        "pin": {
            "id": pin.id,
            "external_id": pin.external_id,
            "title": pin.title,
            "description": pin.description,
        },
        "board": {
            "id": board.id,
            "key": board.key,
            "name": board.name,
            "description": board.description,
            "visibility": board.visibility,
        },
        "media": {
            "asset_id": asset.id,
            "filename": asset.original_file,
            "phone_storage_path": asset.phone_storage_path,
            "mime_type": asset.mime_type,
            "media_hash": asset.media_hash,
        },
        "policy": {
            "profile_only": True,
            "fill_destination": False,
        },
    }


def _device_error_payload(exc: RuntimeError) -> dict[str, Any]:
    message = str(exc) or "device_error"
    return {
        "status": "error",
        "success": False,
        "error": message,
        "message": message,
    }


def _device_timeout_payload() -> dict[str, Any]:
    return {
        "status": "error",
        "success": False,
        "error": "device_command_timeout",
        "message": "device_command_timeout",
    }


def _device_result_success(result: dict[str, Any]) -> bool:
    if result.get("success") is False:
        return False
    if result.get("success") is True:
        return True
    status_value = str(result.get("status") or result.get("result") or "").lower()
    return status_value in {"ok", "success", "posted", "active", "created"}


def _device_result_code(result: dict[str, Any]) -> str:
    return str(result.get("status") or result.get("result") or "success")


def _device_error_code(result: dict[str, Any]) -> str:
    return str(result.get("error_code") or result.get("code") or result.get("error") or "device_rejected")


def _device_error_message(result: dict[str, Any]) -> str:
    return str(result.get("error_message") or result.get("error") or result.get("message") or "Pinterest task failed")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _finish_board_attempt(
    session: AsyncSession,
    attempt: PinterestPostAttempt,
    board: PinterestBoard,
    result: dict[str, Any],
    *,
    success: bool,
) -> None:
    now = _utcnow()
    attempt.finished_at = now
    attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
    if success:
        attempt.status = "success"
        attempt.result_code = _device_result_code(result)
        board.status = "active"
        board.confirmed_at = now
        board.last_error_code = None
        board.last_error_message = None
    else:
        attempt.status = "failed"
        attempt.error_code = _device_error_code(result)
        attempt.error_message = _device_error_message(result)
        board.status = "failed"
        board.last_error_code = attempt.error_code
        board.last_error_message = attempt.error_message
    await session.commit()


async def _finish_pin_attempt(
    session: AsyncSession,
    attempt: PinterestPostAttempt,
    pin: PinterestPin,
    result: dict[str, Any],
    *,
    success: bool,
) -> None:
    now = _utcnow()
    attempt.finished_at = now
    attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
    if success:
        attempt.status = "success"
        attempt.result_code = _device_result_code(result)
        pin.status = "posted"
        pin.posted_at = now
        pin.last_error_code = None
        pin.last_error_message = None
        if result.get("pinterest_pin_url"):
            pin.pinterest_pin_url = str(result["pinterest_pin_url"])
    else:
        attempt.status = "failed"
        attempt.error_code = _device_error_code(result)
        attempt.error_message = _device_error_message(result)
        pin.status = "failed"
        pin.last_error_code = attempt.error_code
        pin.last_error_message = attempt.error_message
    await session.commit()
