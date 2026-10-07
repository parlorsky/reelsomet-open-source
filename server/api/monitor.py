"""Monitor API: competitive monitoring targets & snapshots."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AliasChoices, BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.dependencies import get_bridge, get_db_session, get_ws_manager, require_auth
from server.models import Account, AccountDevice, Device, MonitorSnapshot, MonitorTarget
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])


class MonitorTargetCreate(BaseModel):
    target_username: str = Field(validation_alias=AliasChoices("target_username", "username"))
    account_username: str | None = None
    max_reels: int = Field(default=12, ge=1, le=100)
    enabled: bool = True

    @field_validator("target_username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        value = value.strip().lstrip("@").strip()
        if not value:
            raise ValueError("username must not be empty")
        return value


def _active_targets():
    return select(MonitorTarget).join(
        Account, Account.username == MonitorTarget.account_username,
    ).where(MonitorTarget.is_active.is_(True), Account.is_active.is_(True))


async def _create_target(session: AsyncSession, body: MonitorTargetCreate, username: str | None):
    if username:
        account = (await session.execute(select(Account).where(Account.username == username))).scalar_one_or_none()
        if account is None:
            raise HTTPException(404, "Account not found")
        if not account.is_active:
            raise HTTPException(409, "Account is inactive")
    else:
        account = (await session.execute(
            select(Account).join(AccountDevice, AccountDevice.account_username == Account.username)
            .join(Device, Device.id == AccountDevice.device_id)
            .where(Account.is_active.is_(True), Device.is_active.is_(True))
            .order_by(Account.username).limit(1)
        )).scalar_one_or_none()
        if account is None:
            raise HTTPException(409, "No active account linked to an active device")
    existing = (await session.execute(select(MonitorTarget.target_username).where(
        MonitorTarget.account_username == account.username, MonitorTarget.is_active.is_(True),
    ))).scalars().all()
    normalized = body.target_username.casefold()
    if any(raw.strip().lstrip("@").strip().casefold() == normalized for raw in existing):
        raise HTTPException(409, "Target already exists")
    target = MonitorTarget(account_username=account.username, target_username=body.target_username,
                           max_reels=body.max_reels, is_active=body.enabled)
    session.add(target)
    await session.flush()
    return {"id": target.id, "target_username": target.target_username,
            "username": target.target_username, "account_username": target.account_username,
            "max_reels": target.max_reels, "is_active": target.is_active, "enabled": target.is_active}


class MonitorBulkImport(BaseModel):
    usernames: list[str]
    account_username: str
    max_reels: int = 12


@router.get("/summary")
async def monitor_summary(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return summary statistics for the monitor page stat cards."""
    # Count active targets
    active_targets = (await session.execute(
        select(func.count()).select_from(_active_targets().subquery()),
    )).scalar_one()

    # Total snapshots
    total_snapshots = (await session.execute(
        select(func.count(MonitorSnapshot.id)),
    )).scalar_one()

    # Average plays across all snapshots (where plays is not null)
    avg_plays_raw = (await session.execute(
        select(func.avg(MonitorSnapshot.plays)).where(
            MonitorSnapshot.plays.isnot(None),
        ),
    )).scalar_one()
    avg_plays = round(avg_plays_raw) if avg_plays_raw is not None else 0

    # Average ER% = (likes + comments) / plays * 100 (only where plays > 0)
    er_result = (await session.execute(
        select(
            func.avg(
                (MonitorSnapshot.likes + MonitorSnapshot.comments) * 100.0 / MonitorSnapshot.plays
            ),
        ).where(
            MonitorSnapshot.plays.isnot(None),
            MonitorSnapshot.plays > 0,
            MonitorSnapshot.likes.isnot(None),
            MonitorSnapshot.comments.isnot(None),
        ),
    )).scalar_one()
    avg_er = round(er_result, 2) if er_result is not None else 0.0

    # Last monitored timestamp
    last_monitored = (await session.execute(
        select(func.max(MonitorTarget.last_monitored_at)),
    )).scalar_one()

    return {
        "active_targets": active_targets,
        "total_snapshots": total_snapshots,
        "avg_plays": avg_plays,
        "avg_er_pct": avg_er,
        "last_monitored_at": last_monitored.isoformat() if last_monitored else None,
    }


@router.post("/targets/bulk-import", status_code=status.HTTP_201_CREATED)
async def bulk_import_targets(
    body: MonitorBulkImport,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Bulk-add monitoring targets from a list of usernames."""
    account = (await session.execute(
        select(Account).where(Account.username == body.account_username),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(404, "Account not found")

    # Deduplicate and strip whitespace / @ prefix
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in body.usernames:
        u = raw.strip().lstrip("@").strip()
        if u and u.lower() not in seen:
            seen.add(u.lower())
            cleaned.append(u)

    # Check existing active targets to skip duplicates
    existing_result = await session.execute(
        select(MonitorTarget.target_username).where(
            MonitorTarget.account_username == body.account_username,
            MonitorTarget.is_active == True,  # noqa: E712
        ),
    )
    existing_usernames = {r.strip().lstrip("@").strip().lower() for r in existing_result.scalars().all()}

    created = 0
    skipped = 0
    for username in cleaned:
        if username.lower() in existing_usernames:
            skipped += 1
            continue
        target = MonitorTarget(
            account_username=body.account_username,
            target_username=username,
            max_reels=body.max_reels,
        )
        session.add(target)
        created += 1

    await session.flush()
    return {
        "created": created,
        "skipped": skipped,
        "total_submitted": len(cleaned),
    }


@router.get("/status")
async def monitor_status(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Return monitoring overview."""
    active_targets = (await session.execute(
        select(func.count()).select_from(_active_targets().subquery()),
    )).scalar_one()

    total_snapshots = (await session.execute(
        select(func.count(MonitorSnapshot.id)),
    )).scalar_one()

    return {
        "active_targets": active_targets,
        "total_snapshots": total_snapshots,
    }


class MonitorToggle(BaseModel):
    enabled: bool


@router.post("/toggle")
async def toggle_monitor(
    body: MonitorToggle,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Enable or disable monitoring globally (updates insights_enabled on all active accounts)."""
    result = await session.execute(
        select(Account).where(Account.is_active == True),  # noqa: E712
    )
    accounts = result.scalars().all()
    count = 0
    for acc in accounts:
        acc.insights_enabled = body.enabled
        count += 1
    await session.flush()
    return {"enabled": body.enabled, "accounts_updated": count}


@router.get("/targets")
async def list_all_targets(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return all active monitoring targets across all accounts."""
    result = await session.execute(
        _active_targets().order_by(MonitorTarget.id),  # noqa: E712
    )
    targets = result.scalars().all()
    return [
        {
            "id": t.id,
            "target_username": t.target_username,
            "username": t.target_username,
            "account_username": t.account_username,
            "max_reels": t.max_reels,
            "is_active": t.is_active,
            "enabled": t.is_active,
            "last_monitored_at": t.last_monitored_at.isoformat() if t.last_monitored_at else None,
            "last_checked_at": t.last_monitored_at.isoformat() if t.last_monitored_at else None,
            "check_interval_hours": 12,
            "status": "ok",
            "notes": "",
        }
        for t in targets
    ]


@router.post("/targets", status_code=status.HTTP_201_CREATED)
async def create_target_flat(
    body: MonitorTargetCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Create a target with either the API or dashboard field names."""
    return await _create_target(session, body, body.account_username)


@router.delete("/targets/{target_id}")
async def delete_target_flat(
    target_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Deactivate a monitoring target by ID."""
    target = (await session.execute(
        select(MonitorTarget).where(MonitorTarget.id == target_id),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Target not found")
    target.is_active = False
    await session.flush()
    return {"id": target.id, "is_active": False}


@router.patch("/targets/{target_id}")
async def update_target_flat(
    target_id: int,
    body: dict[str, Any],
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Update a monitoring target by ID."""
    target = (await session.execute(
        select(MonitorTarget).where(MonitorTarget.id == target_id),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Target not found")
    if "enabled" in body:
        body["is_active"] = body["enabled"]
    for key in ("target_username", "max_reels", "is_active"):
        if key in body:
            setattr(target, key, body[key])
    await session.flush()
    return {
        "id": target.id,
        "target_username": target.target_username,
        "account_username": target.account_username,
        "max_reels": target.max_reels,
        "is_active": target.is_active,
        "enabled": target.is_active,
    }


@router.get("/targets/{target_id}/snapshots")
async def target_snapshots(
    target_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return snapshots for a specific target by ID."""
    target = (await session.execute(
        select(MonitorTarget).where(MonitorTarget.id == target_id),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(404, "Target not found")
    result = await session.execute(
        select(MonitorSnapshot).where(
            MonitorSnapshot.target_username == target.target_username,
            MonitorSnapshot.account_username == target.account_username,
        ).order_by(MonitorSnapshot.id.desc()).limit(limit),
    )
    snaps = result.scalars().all()
    return [
        {
            "id": s.id,
            "target_username": s.target_username,
            "account_username": s.account_username,
            "plays": s.plays,
            "likes": s.likes,
            "comments": s.comments,
            "reel_position": s.reel_position,
            "caption_snippet": s.caption_snippet,
            "collected_at": s.collected_at.isoformat() if s.collected_at else None,
            "timestamp": s.collected_at.isoformat() if s.collected_at else None,
        }
        for s in snaps
    ]


@router.get("/{account_username}/targets")
async def list_targets(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict[str, Any]]:
    """Return monitoring targets for an account."""
    result = await session.execute(
        select(MonitorTarget).where(
            MonitorTarget.account_username == account_username,
            MonitorTarget.is_active == True,  # noqa: E712
        ),
    )
    targets = result.scalars().all()
    return [
        {
            "id": t.id,
            "target_username": t.target_username,
            "username": t.target_username,
            "account_username": t.account_username,
            "max_reels": t.max_reels,
            "is_active": t.is_active,
            "enabled": t.is_active,
            "last_monitored_at": t.last_monitored_at.isoformat() if t.last_monitored_at else None,
            "last_checked_at": t.last_monitored_at.isoformat() if t.last_monitored_at else None,
            "check_interval_hours": 12,
            "status": "ok",
            "notes": "",
        }
        for t in targets
    ]


@router.post("/{account_username}/targets", status_code=status.HTTP_201_CREATED)
async def add_target(
    account_username: str,
    body: MonitorTargetCreate,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Add a normalized, unique target for an active account."""
    return await _create_target(session, body, account_username)


@router.delete("/{account_username}/targets/{target_id}")
async def delete_target(
    account_username: str,
    target_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Deactivate a monitoring target."""
    target = (await session.execute(
        select(MonitorTarget).where(
            MonitorTarget.id == target_id,
            MonitorTarget.account_username == account_username,
        ),
    )).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    target.is_active = False
    await session.flush()
    return {"id": target.id, "is_active": False}


@router.get("/{account_username}/snapshots")
async def list_snapshots(
    account_username: str,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict[str, Any]]:
    """Return monitoring snapshots for an account."""
    query = (
        select(MonitorSnapshot)
        .where(MonitorSnapshot.account_username == account_username)
        .order_by(MonitorSnapshot.id.desc())
        .limit(limit)
    )
    result = await session.execute(query)
    snaps = result.scalars().all()
    return [
        {
            "id": s.id,
            "target_username": s.target_username,
            "account_username": s.account_username,
            "plays": s.plays,
            "likes": s.likes,
            "comments": s.comments,
            "reel_position": s.reel_position,
            "caption_snippet": s.caption_snippet,
            "collected_at": s.collected_at.isoformat() if s.collected_at else None,
            "timestamp": s.collected_at.isoformat() if s.collected_at else None,
        }
        for s in snaps
    ]


@router.post("/run-now")
async def run_monitoring_now(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Trigger an immediate monitoring run on all online devices.

    Iterates through active monitor targets, groups by device, and sends
    monitoring commands to each online device.
    """
    # Gather all active monitor targets with their account-device links
    targets_result = await session.execute(
        _active_targets(),  # noqa: E712
    )
    all_targets = targets_result.scalars().all()

    # Group targets by account, then resolve each account to its device
    account_targets: dict[str, list[MonitorTarget]] = {}
    for t in all_targets:
        account_targets.setdefault(t.account_username, []).append(t)

    dispatched: list[int] = []
    skipped: list[str] = []

    for username, targets in account_targets.items():
        link = (await session.execute(
            select(AccountDevice).join(Device, Device.id == AccountDevice.device_id).where(
                Device.is_active.is_(True),
                AccountDevice.account_username == username,
            ).order_by(AccountDevice.is_primary.desc()).limit(1),
        )).scalar_one_or_none()

        if link is None or not ws.is_online(link.device_id):
            skipped.append(username)
            continue

        # Build monitoring payload matching the Android MessageRouter contract
        payload = {
            "accountUsername": username,
            "targets": [
                {
                    "targetUsername": t.target_username,
                    "maxReels": t.max_reels,
                }
                for t in targets
            ],
        }
        try:
            await bridge.start_monitoring(link.device_id, payload)
            dispatched.append(link.device_id)
            logger.info("Dispatched monitoring to device %d for %s", link.device_id, username)
        except Exception as exc:
            logger.warning("Failed to dispatch monitoring to device %d: %s", link.device_id, exc)
            skipped.append(username)

    return {
        "message": "Monitoring run dispatched",
        "devices_dispatched": dispatched,
        "accounts_skipped": skipped,
    }
