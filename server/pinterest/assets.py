"""Pinterest asset staging helpers."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path

from server.config import VPSConfig
from server.models import PinterestAsset

_TOKEN_TTL_SECONDS = 600
_ALLOWED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def build_public_base_url(config: VPSConfig) -> str:
    if config.domain:
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", config.domain):
            return f"http://{config.domain}:8443"
        return f"https://{config.domain}"
    return f"http://{config.host}:{config.port}"


def build_pinterest_asset_url(config: VPSConfig, asset: PinterestAsset) -> str:
    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    filename = Path(asset.storage_path).name
    token = compute_pinterest_asset_token(asset.id, filename, expires, config.jwt_secret)
    base = build_public_base_url(config)
    return f"{base}/api/pinterest/assets/{asset.id}/download/{filename}?token={token}&expires={expires}"


def compute_pinterest_asset_token(asset_id: int, filename: str, expires: int, secret: str) -> str:
    message = f"pinterest-asset:{asset_id}:{filename}:{expires}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_pinterest_asset_token(
    *,
    asset_id: int,
    filename: str,
    expires: int,
    token: str,
    config: VPSConfig,
) -> bool:
    if int(time.time()) > expires:
        return False
    expected = compute_pinterest_asset_token(asset_id, filename, expires, config.jwt_secret)
    return hmac.compare_digest(token, expected)


def validate_asset_file(asset: PinterestAsset, filename: str) -> Path:
    path = Path(asset.storage_path)
    if path.name != filename:
        raise ValueError("filename_mismatch")
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError("unsupported_asset_type")
    if not path.is_file():
        raise FileNotFoundError(str(path))
    return path


def asset_media_type(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def stage_payload(config: VPSConfig, asset: PinterestAsset, index: int) -> dict[str, object]:
    return {
        "url": build_pinterest_asset_url(config, asset),
        "filename": Path(asset.storage_path).name,
        "index": index,
        "assetId": asset.id,
    }
