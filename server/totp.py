"""TOTP (Time-Based One-Time Password) generator for Instagram 2FA.

Implements RFC 6238 without external dependencies (no pyotp needed).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def generate_totp(secret: str, digits: int = 6, period: int = 30) -> str:
    """Generate a TOTP code from a base32-encoded secret.

    Parameters
    ----------
    secret : str
        Base32-encoded secret (e.g., "H4HCSCNZDHASWWDEIABG3HJYYWCPF4EV").
    digits : int
        Number of digits (default 6).
    period : int
        Time step in seconds (default 30).

    Returns
    -------
    str
        Zero-padded TOTP code (e.g., "482931").
    """
    # Decode base32 secret (pad if needed)
    secret_clean = secret.upper().replace(" ", "").replace("-", "")
    padding = 8 - len(secret_clean) % 8
    if padding != 8:
        secret_clean += "=" * padding
    key = base64.b32decode(secret_clean)

    # Current time step
    counter = int(time.time()) // period
    counter_bytes = struct.pack(">Q", counter)

    # HMAC-SHA1
    mac = hmac.new(key, counter_bytes, hashlib.sha1).digest()

    # Dynamic truncation
    offset = mac[-1] & 0x0F
    code_int = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    code = code_int % (10 ** digits)

    return str(code).zfill(digits)
