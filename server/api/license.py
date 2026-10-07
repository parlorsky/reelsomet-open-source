"""Read-only edition information for compatibility with existing clients."""
from fastapi import APIRouter

router = APIRouter(prefix="/api/license", tags=["edition"])


@router.get("/status")
async def license_status() -> dict:
    return {
        "active": True, "needs_license": False, "hwid": "",
        "tier": "open-source", "license": "MIT", "expires": None,
        "expires_display": "No expiration", "max_devices": None,
        "days_remaining": None,
    }
