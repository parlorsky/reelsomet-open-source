"""Authentication: bcrypt password hashing, JWT tokens, FastAPI dependency."""
from __future__ import annotations

import hmac
import logging
import time
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, Request, status

from server.config import VPSConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

def create_jwt_token(data: dict[str, Any], secret: str, expires_hours: int = 24) -> str:
    """Create a signed JWT token with an expiration claim."""
    payload = data.copy()
    payload["exp"] = int(time.time()) + expires_hours * 3600
    payload["iat"] = int(time.time())
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_jwt_token(token: str, secret: str) -> dict[str, Any] | None:
    """Decode and validate a JWT token. Returns the payload dict or None on failure."""
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        logger.debug("JWT token expired")
        return None
    except jwt.InvalidTokenError as exc:
        logger.debug("Invalid JWT token: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Device token verification
# ---------------------------------------------------------------------------

def verify_device_token(token: str, config: VPSConfig) -> int | None:
    """Validate a device authentication token.

    Device tokens are simple bearer strings stored in config.device_tokens
    as {device_id: token_string}.  Returns the device_id if the token matches,
    else None.
    """
    for device_id, expected_token in config.device_tokens.items():
        if hmac.compare_digest(token, expected_token):
            return int(device_id)
    return None


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def get_current_user(request: Request) -> dict[str, Any]:
    """Extract and validate JWT from the Authorization header.

    Usage::

        @router.get("/protected")
        async def protected(user: dict = Depends(get_current_user)):
            ...

    Raises 401 if the token is missing or invalid.
    """
    auth_header: str | None = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:]  # strip "Bearer "
    config: VPSConfig = request.app.state.config
    payload = decode_jwt_token(token, config.jwt_secret)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload
