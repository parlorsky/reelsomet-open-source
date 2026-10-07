"""Tests for server.auth: password hashing, JWT tokens, device tokens."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest

from server.auth import (
    create_jwt_token,
    decode_jwt_token,
    hash_password,
    verify_device_token,
    verify_password,
)
from server.config import VPSConfig


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:

    def test_hash_and_verify_password(self) -> None:
        """hash -> verify round-trip succeeds."""
        pw = "super-secret-123!"
        hashed = hash_password(pw)
        assert isinstance(hashed, str)
        assert hashed != pw  # Not stored in plain text
        assert verify_password(pw, hashed) is True

    def test_verify_wrong_password(self) -> None:
        """Wrong password does not verify."""
        hashed = hash_password("correct-horse")
        assert verify_password("wrong-horse", hashed) is False

    def test_hash_produces_different_salts(self) -> None:
        """Two hashes of the same password differ (different salts)."""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # Different salts
        # But both still verify
        assert verify_password("same", h1) is True
        assert verify_password("same", h2) is True

    def test_hash_handles_unicode(self) -> None:
        """Unicode passwords hash and verify correctly."""
        pw = "\u041f\u0430\u0440\u043e\u043b\u044c123"  # Russian "Password123"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed) is True

    def test_hash_handles_empty_string(self) -> None:
        """Empty string is a valid password (for hashing purposes)."""
        hashed = hash_password("")
        assert verify_password("", hashed) is True
        assert verify_password("x", hashed) is False


# ---------------------------------------------------------------------------
# JWT tokens
# ---------------------------------------------------------------------------

class TestJWTTokens:

    SECRET = "test-secret-key-for-jwt"

    def test_create_and_decode_jwt(self) -> None:
        """Round-trip: create a token and decode it back."""
        data = {"user": "admin", "role": "owner"}
        token = create_jwt_token(data, self.SECRET, expires_hours=24)
        assert isinstance(token, str)

        payload = decode_jwt_token(token, self.SECRET)
        assert payload is not None
        assert payload["user"] == "admin"
        assert payload["role"] == "owner"
        assert "exp" in payload
        assert "iat" in payload

    def test_jwt_contains_expiry_and_iat(self) -> None:
        """Token payload includes exp and iat claims."""
        now = int(time.time())
        token = create_jwt_token({"x": 1}, self.SECRET, expires_hours=12)
        payload = decode_jwt_token(token, self.SECRET)
        assert payload is not None
        # exp should be ~12 hours from now
        assert payload["exp"] >= now + 11 * 3600
        assert payload["exp"] <= now + 13 * 3600
        # iat should be approximately now
        assert abs(payload["iat"] - now) < 5

    def test_decode_expired_jwt(self) -> None:
        """Expired token returns None."""
        data = {"user": "admin"}
        # Create a token that expired 1 hour ago
        payload = data.copy()
        payload["exp"] = int(time.time()) - 3600
        payload["iat"] = int(time.time()) - 7200
        token = pyjwt.encode(payload, self.SECRET, algorithm="HS256")

        result = decode_jwt_token(token, self.SECRET)
        assert result is None

    def test_decode_invalid_jwt(self) -> None:
        """Garbage token returns None."""
        result = decode_jwt_token("not.a.valid.token", self.SECRET)
        assert result is None

    def test_decode_empty_string(self) -> None:
        """Empty string returns None."""
        result = decode_jwt_token("", self.SECRET)
        assert result is None

    def test_decode_wrong_secret(self) -> None:
        """Token signed with different secret returns None."""
        token = create_jwt_token({"user": "admin"}, "secret-A")
        result = decode_jwt_token(token, "secret-B")
        assert result is None

    def test_jwt_preserves_nested_payload(self) -> None:
        """Complex payloads (nested dicts, lists) survive round-trip."""
        data = {"devices": [1, 2, 3], "meta": {"region": "eu"}}
        token = create_jwt_token(data, self.SECRET)
        payload = decode_jwt_token(token, self.SECRET)
        assert payload is not None
        assert payload["devices"] == [1, 2, 3]
        assert payload["meta"]["region"] == "eu"


# ---------------------------------------------------------------------------
# Device token verification
# ---------------------------------------------------------------------------

class TestDeviceToken:

    def _make_config(self) -> VPSConfig:
        return VPSConfig(device_tokens={42: "valid-token-abc", 7: "token-seven"})

    def test_verify_device_token_valid(self) -> None:
        """Valid device token returns the device_id."""
        config = self._make_config()
        result = verify_device_token("valid-token-abc", config)
        assert result == 42

    def test_verify_device_token_second_device(self) -> None:
        """Second device token also works."""
        config = self._make_config()
        result = verify_device_token("token-seven", config)
        assert result == 7

    def test_verify_device_token_invalid(self) -> None:
        """Garbage token returns None."""
        config = self._make_config()
        result = verify_device_token("garbage.token.here", config)
        assert result is None

    def test_verify_device_token_empty(self) -> None:
        """Empty token returns None."""
        config = self._make_config()
        result = verify_device_token("", config)
        assert result is None

    def test_verify_device_token_no_tokens_configured(self) -> None:
        """No tokens configured returns None."""
        config = VPSConfig(device_tokens={})
        result = verify_device_token("any-token", config)
        assert result is None

    def test_verify_device_token_partial_match(self) -> None:
        """Partial match of token string returns None."""
        config = self._make_config()
        result = verify_device_token("valid-token", config)
        assert result is None
