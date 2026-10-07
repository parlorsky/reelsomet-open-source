"""Instagram account login API: bulk import, credential storage, login trigger."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.dependencies import (
    get_bridge,
    get_config,
    get_db_session,
    get_ws_manager,
    require_auth,
)
from server.models import Account, AccountDevice, Device
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ig", tags=["instagram-login"])


async def _set_import_device_link(
    session: AsyncSession,
    *,
    account_username: str,
    device_id: int,
) -> None:
    """Normalize import-assigned device links down to one primary link."""
    existing_links = (await session.execute(
        select(AccountDevice)
        .where(AccountDevice.account_username == account_username)
        .order_by(AccountDevice.id),
    )).scalars().all()

    retained_link: AccountDevice | None = None
    for link in existing_links:
        if retained_link is None and link.device_id == device_id:
            retained_link = link
            retained_link.is_primary = True
            continue
        await session.delete(link)

    if retained_link is None:
        session.add(
            AccountDevice(
                account_username=account_username,
                device_id=device_id,
                is_primary=True,
            )
        )


class AccountImportItem(BaseModel):
    """Single account in login:password:2fa format."""
    username: str
    password: str
    totp_secret: str = ""
    device_id: int | None = None


class BulkImportRequest(BaseModel):
    """Bulk import: raw text lines in login:password:2fa format or list of items."""
    raw: str = ""
    accounts: list[AccountImportItem] = []
    device_id: int | None = None


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def bulk_import(
    body: BulkImportRequest,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Bulk import Instagram accounts from login:password:2fa_secret format.

    Accepts either ``raw`` text (one account per line, colon-separated)
    or a ``accounts`` list of structured items. Creates Account records
    and stores credentials.

    Example raw format::

        cacosta1771031406:eKz1U9ua1OPIXEX3K:H4HCSCNZDHASWWDEIABG3HJYYWCPF4EV
        eguzman1770966758:TVUsxQgiOmwqXYfpkPP:VOFQKGLELZJ546F6SYKUVPPSH7ZN5NOU
    """
    items: list[AccountImportItem] = list(body.accounts)

    # Parse raw text lines
    if body.raw:
        for line in body.raw.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) < 2:
                continue
            items.append(AccountImportItem(
                username=parts[0].strip(),
                password=parts[1].strip(),
                totp_secret=parts[2].strip() if len(parts) > 2 else "",
                device_id=body.device_id,
            ))

    created = 0
    updated = 0
    errors: list[str] = []

    for item in items:
        try:
            resolved_device_id = item.device_id or body.device_id
            if resolved_device_id:
                device = (await session.execute(
                    select(Device).where(
                        Device.id == resolved_device_id,
                        Device.is_active == True,  # noqa: E712
                    ),
                )).scalar_one_or_none()
                if device is None:
                    raise ValueError("Device not found")

            existing = (await session.execute(
                select(Account).where(Account.username == item.username),
            )).scalar_one_or_none()

            if existing:
                existing.ig_password = item.password
                existing.ig_2fa_secret = item.totp_secret or existing.ig_2fa_secret
                existing.is_active = True
                if resolved_device_id:
                    await _set_import_device_link(
                        session,
                        account_username=existing.username,
                        device_id=resolved_device_id,
                    )
                updated += 1
            else:
                acct = Account(
                    username=item.username,
                    ig_password=item.password,
                    ig_2fa_secret=item.totp_secret,
                    posting_enabled=True,
                    engagement_enabled=True,
                    insights_enabled=True,
                )
                session.add(acct)
                await session.flush()

                # Link to device if specified
                if resolved_device_id:
                    await _set_import_device_link(
                        session,
                        account_username=item.username,
                        device_id=resolved_device_id,
                    )
                created += 1
        except Exception as exc:
            # Roll back the current transaction so the next loop
            # iteration runs on a clean session. Without this, the
            # session is left in an aborted state and subsequent
            # flush() calls raise InvalidRequestError, corrupting
            # the whole batch (Codex iter 6 bug hunt 2026-04-14).
            try:
                await session.rollback()
            except Exception:
                logger.exception("Bulk import rollback failed for @%s", item.username)
            logger.exception("Bulk import failed for @%s", item.username)
            # Whitelist safe exception types whose messages are
            # operator-controlled (no user-supplied bound params).
            # SQLAlchemy IntegrityError / ProgrammingError and
            # friends format with bound parameter values in their
            # `__str__`, so those are scrubbed to just the class
            # name to avoid leaking `ig_password` through the
            # response body.
            _safe_exc_types = (ValueError, LookupError, KeyError, HTTPException)
            if isinstance(exc, _safe_exc_types):
                errors.append(f"{item.username}: {exc}")
            else:
                errors.append(f"{item.username}: {type(exc).__name__}")

    await session.commit()
    logger.info("Bulk import: %d created, %d updated, %d errors", created, updated, len(errors))
    return {
        "created": created,
        "updated": updated,
        "errors": errors,
        "total": len(items),
    }


@router.post("/login/{account_id}")
async def trigger_login(
    account_id: int,
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Trigger Instagram login on the phone for a specific account.

    Generates a fresh TOTP code and sends login credentials to the device.
    """
    account = (await session.execute(
        select(Account).where(Account.id == account_id),
    )).scalar_one_or_none()
    if account is None:
        raise HTTPException(404, "Account not found")

    if not account.ig_password:
        raise HTTPException(400, "No password stored for this account")

    # Find device
    link = (await session.execute(
        select(AccountDevice)
        .join(Device, Device.id == AccountDevice.device_id)
        .where(
            AccountDevice.account_username == account.username,
            Device.is_active == True,  # noqa: E712
        )
        .order_by(AccountDevice.is_primary.desc())
        .limit(1),
    )).scalar_one_or_none()
    if link is None:
        raise HTTPException(400, "Account not linked to any device")
    if not ws.is_online(link.device_id):
        raise HTTPException(400, f"Device {link.device_id} is offline")

    # Send raw 2FA secret to device — phone generates fresh TOTP on-demand
    totp_secret = account.ig_2fa_secret or ""

    try:
        result = await bridge.instagram_login(
            device_id=link.device_id,
            username=account.username,
            password=account.ig_password,
            totp_secret=totp_secret,
        )
    except Exception as exc:
        raise HTTPException(500, f"Login command failed: {exc}")

    return {
        "account_id": account_id,
        "username": account.username,
        "device_id": link.device_id,
        "has_2fa": bool(totp_secret),
        "result": result,
    }


@router.post("/login-all")
async def trigger_login_all(
    _user: dict = Depends(require_auth),
    session: AsyncSession = Depends(get_db_session),
    ws: DeviceConnectionManager = Depends(get_ws_manager),
    bridge: DeviceBridge = Depends(get_bridge),
) -> dict[str, Any]:
    """Trigger login for ALL accounts that have stored credentials."""
    accounts = (await session.execute(
        select(Account).where(
            Account.is_active == True,  # noqa: E712
            Account.ig_password.isnot(None),
            Account.ig_password != "",
        ),
    )).scalars().all()

    results: list[dict[str, Any]] = []
    for account in accounts:
        link = (await session.execute(
            select(AccountDevice)
            .join(Device, Device.id == AccountDevice.device_id)
            .where(
                AccountDevice.account_username == account.username,
                Device.is_active == True,  # noqa: E712
            )
            .order_by(AccountDevice.is_primary.desc())
            .limit(1),
        )).scalar_one_or_none()

        if link is None or not ws.is_online(link.device_id):
            results.append({"username": account.username, "status": "skipped", "reason": "device offline"})
            continue

        totp_secret = account.ig_2fa_secret or ""

        try:
            result = await bridge.instagram_login(
                device_id=link.device_id,
                username=account.username,
                password=account.ig_password,
                totp_secret=totp_secret,
            )
            results.append({"username": account.username, "status": "sent", "result": result})
        except Exception as exc:
            results.append({"username": account.username, "status": "failed", "error": str(exc)})

    return {"total": len(accounts), "results": results}
