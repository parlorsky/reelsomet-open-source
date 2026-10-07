"""Auth API: login, token refresh, current user info."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.auth import create_jwt_token, get_current_user, verify_password
from server.config import VPSConfig
from server.dependencies import get_config

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/login")
async def login(body: LoginRequest, config: VPSConfig = Depends(get_config)) -> Any:
    """Authenticate with admin password and receive a JWT token.

    If no admin password is configured yet, returns a special
    ``{"error": "not_configured", "needs_setup": true}`` response so the
    frontend can redirect to the setup wizard.
    """
    if not config.admin_password_hash:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "not_configured", "needs_setup": True},
        )
    if not verify_password(body.password, config.admin_password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )
    token = create_jwt_token(
        {"sub": "admin", "role": "admin"},
        config.jwt_secret,
        expires_hours=config.jwt_expire_hours,
    )
    return TokenResponse(access_token=token)


@router.get("/me")
async def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """Return the current authenticated user info from the JWT payload."""
    return {"sub": user.get("sub"), "role": user.get("role")}


@router.get("/verify")
async def verify_token(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, bool]:
    """Verify the current JWT token is valid."""
    return {"valid": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    _user: dict[str, Any] = Depends(get_current_user),
    config: VPSConfig = Depends(get_config),
) -> dict[str, str]:
    """Change the admin password."""
    from server.auth import hash_password
    from server.config import save_config

    if not verify_password(body.current_password, config.admin_password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    config.admin_password_hash = hash_password(body.new_password)
    save_config(config)
    return {"message": "Password changed successfully"}
