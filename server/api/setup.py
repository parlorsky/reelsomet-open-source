"""Setup wizard API: first-run configuration (no auth required)."""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from server.auth import create_jwt_token, hash_password
from server.config import VPSConfig, save_config
from server.dependencies import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["setup"])


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------

class SetupStatusResponse(BaseModel):
    needs_setup: bool
    has_password: bool
    has_telegram: bool
    device_count: int


class SetupCompleteRequest(BaseModel):
    admin_password: str = Field(..., min_length=6)
    telegram_token: str | None = None
    telegram_chat_id: int | None = None


class SetupCompleteResponse(BaseModel):
    success: bool
    device_token: str
    access_token: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status", response_model=SetupStatusResponse)
async def setup_status(config: VPSConfig = Depends(get_config)) -> Any:
    """Check whether first-run setup is needed."""
    has_password = bool(config.admin_password_hash)
    has_telegram = bool(
        config.telegram_bot_token
        and config.telegram_bot_token not in ("", "placeholder")
    )
    device_count = len(config.device_tokens)

    return SetupStatusResponse(
        needs_setup=not has_password,
        has_password=has_password,
        has_telegram=has_telegram,
        device_count=device_count,
    )


@router.post("/complete", response_model=SetupCompleteResponse)
async def setup_complete(
    body: SetupCompleteRequest,
    config: VPSConfig = Depends(get_config),
    x_setup_token: str | None = Header(default=None),
) -> Any:
    """Complete first-run setup. Only works when no admin password is set."""
    if config.admin_password_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Setup already completed. Admin password is already configured.",
        )

    if not config.setup_token or not secrets.compare_digest(x_setup_token or "", config.setup_token):
        raise HTTPException(status_code=403, detail="A valid setup token is required")

    # --- Hash the admin password ---
    config.admin_password_hash = hash_password(body.admin_password)

    # --- Generate JWT secret if it looks like the default random one ---
    # (always regenerate on first setup to ensure it's persisted)
    config.jwt_secret = secrets.token_urlsafe(32)

    # --- Generate device token for device_id=1 ---
    device_token = secrets.token_urlsafe(32)
    config.device_tokens[1] = device_token

    # --- Optional Telegram config ---
    if body.telegram_token:
        config.telegram_bot_token = body.telegram_token
    if body.telegram_chat_id is not None:
        config.telegram_admin_chat_ids = [body.telegram_chat_id]

    # The bootstrap credential can be used only once.
    config.setup_token = ""

    # --- Persist to config.yaml ---
    save_config(config)
    logger.info("First-run setup completed successfully")

    # --- Create a JWT access token for the admin ---
    access_token = create_jwt_token(
        {"sub": "admin", "role": "admin"},
        config.jwt_secret,
        expires_hours=config.jwt_expire_hours,
    )

    return SetupCompleteResponse(
        success=True,
        device_token=device_token,
        access_token=access_token,
    )
