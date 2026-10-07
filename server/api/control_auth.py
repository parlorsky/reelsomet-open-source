"""Step-up auth endpoint for remote screen control."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server.auth import create_jwt_token, get_current_user, verify_password
from server.config import VPSConfig
from server.dependencies import get_config

router = APIRouter(prefix="/api/auth", tags=["auth"])


class ControlGrantRequest(BaseModel):
    password: str


class ControlGrantResponse(BaseModel):
    control_token: str


@router.post("/control-grant")
async def control_grant(
    body: ControlGrantRequest,
    _user: dict = Depends(get_current_user),
    config: VPSConfig = Depends(get_config),
) -> ControlGrantResponse:
    if not config.admin_password_hash or not verify_password(body.password, config.admin_password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )

    token = create_jwt_token(
        {"sub": "admin", "purpose": "screen_control"},
        config.jwt_secret,
        expires_hours=1,
    )
    return ControlGrantResponse(control_token=token)
