"""Compatibility model for the former commercial edition.

MIT-licensed Reelsomet has no activation, fingerprint, signing secret,
expiration, or licensed device limit.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class LicenseInfo:
    hwid: str = ""
    expires: str = "lifetime"
    max_devices: int = 0  # Existing callers interpret zero as unlimited.
    tier: str = "open-source"
    is_expired: bool = False
    days_remaining: int | None = None

    @property
    def is_lifetime(self) -> bool:
        return self.expires == "lifetime"
