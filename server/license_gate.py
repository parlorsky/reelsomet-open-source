"""Edition compatibility helpers; authentication is enforced separately.

Open-source installs do not require license keys. JWT and device-token
checks still apply to their respective API and WebSocket endpoints.
"""
from fastapi import Request
from server.config import VPSConfig
from server.licensing import LicenseInfo


def get_license_info(config: VPSConfig) -> LicenseInfo:
    return LicenseInfo()


def is_licensed(config: VPSConfig) -> bool:
    return True


async def require_license(request: Request) -> LicenseInfo:
    return LicenseInfo()
