"""Tests for server.api.settings_api: get/patch server configuration."""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from server.api.settings_api import _SECRET_PLACEHOLDER


class TestGetSettings:

    @pytest.mark.asyncio
    async def test_get_settings(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/settings returns config grouped by section."""
        resp = await client.get("/api/settings", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()

        # Check all exposed sections exist
        assert "general" in body
        assert "server" in body
        assert "database" in body
        assert "telegram" in body
        assert "llm" in body
        assert "websocket" in body
        assert body["general"] == {}

        # Check server section values from test_config
        assert body["server"]["host"] == "127.0.0.1"
        assert body["server"]["port"] == 9999
        assert body["server"]["domain"] == "test.example.com"

        # Check websocket section
        assert body["websocket"]["ws_ping_interval"] == 15.0
        assert body["websocket"]["ws_ping_timeout"] == 5.0

        # Comma-separated text field for dashboard input
        assert body["telegram"]["telegram_admin_chat_ids"] == "111, 222"

        # Check LLM section
        assert body["llm"]["llm_base_url"] == "http://localhost:11434"
        assert body["llm"]["llm_model"] == "test-model"

    @pytest.mark.asyncio
    async def test_get_settings_unauthorized(self, client: AsyncClient) -> None:
        """GET /api/settings without token returns 401."""
        resp = await client.get("/api/settings")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_get_settings_no_secrets(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Settings response does not expose password hash or JWT secret."""
        resp = await client.get("/api/settings", headers=auth_headers)
        body = resp.json()
        all_values_str = str(body)
        assert "jwt_secret" not in all_values_str
        assert "admin_password_hash" not in all_values_str
        assert "test-llm-key" not in all_values_str
        assert "123456:ABC-DEF" not in all_values_str
        assert body["llm"]["api_key"] == _SECRET_PLACEHOLDER
        assert body["farm"]["telegram_bot_token"] == _SECRET_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_get_settings_section(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """GET /api/settings/{section} returns the serialized section payload."""
        resp = await client.get("/api/settings/llm", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["llm_base_url"] == "http://localhost:11434"
        assert body["llm_model"] == "test-model"
        assert body["api_key"] == _SECRET_PLACEHOLDER

    @pytest.mark.asyncio
    async def test_get_settings_section_invalid_section(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Unknown section lookups return 404 instead of a generic failure."""
        resp = await client.get("/api/settings/not-a-section", headers=auth_headers)
        assert resp.status_code == 404
        assert "Unknown section" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_settings_includes_farm_section(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Farm settings are visible in the settings response."""
        resp = await client.get("/api/settings", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "farm" in body

        farm = body["farm"]
        # All 10 farm keys should be present
        expected_keys = [
            "farm_default_posting_times",
            "farm_timezone",
            "farm_max_posts_per_account_per_day",
            "farm_upload_window_minutes",
            "farm_max_auto_retries",
            "farm_auto_retry_delay_minutes",
            "farm_action_blocked_pause_hours",
            "farm_schedule_jitter_std_seconds",
            "farm_poll_interval_seconds",
            "farm_health_check_interval_seconds",
            "farm_result_poll_interval_seconds",
            # Feature A addition
            "carousel_set_cooldown_days",
            # Feature D additions
            "story_element_probability",
            "story_poll_fallback",
            "story_question_fallback",
        ]
        for key in expected_keys:
            assert key in farm, f"Missing farm key: {key}"

        # Verify types of a few known defaults
        assert isinstance(farm["farm_default_posting_times"], list)
        assert farm["farm_timezone"] == "Europe/Moscow"
        assert isinstance(farm["farm_max_auto_retries"], int)
        assert isinstance(farm["farm_action_blocked_pause_hours"], float)
        # Feature D — story config defaults
        assert isinstance(farm["story_element_probability"], float)
        assert 0.0 <= farm["story_element_probability"] <= 1.0
        assert isinstance(farm["story_poll_fallback"], str)
        assert isinstance(farm["story_question_fallback"], str)
        # Feature A — carousel cooldown default
        assert isinstance(farm["carousel_set_cooldown_days"], int)


class TestFeatureDStoryConfigPatch:
    """Feature D: PATCH round-trip for the three story config fields."""

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_story_element_probability(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = await client.patch(
            "/api/settings/farm/story_element_probability",
            json={"value": 0.35},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        # Verify round-trip via GET
        get_resp = await client.get("/api/settings/farm", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["story_element_probability"] == pytest.approx(0.35)

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_story_poll_fallback(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        new_val = "Best pic? | This one | That one"
        resp = await client.patch(
            "/api/settings/farm/story_poll_fallback",
            json={"value": new_val},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        get_resp = await client.get("/api/settings/farm", headers=auth_headers)
        assert get_resp.json()["story_poll_fallback"] == new_val

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_story_question_fallback(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        new_val = "Ask something bold 🔥"
        resp = await client.patch(
            "/api/settings/farm/story_question_fallback",
            json={"value": new_val},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        get_resp = await client.get("/api/settings/farm", headers=auth_headers)
        assert get_resp.json()["story_question_fallback"] == new_val

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_carousel_set_cooldown_days(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = await client.patch(
            "/api/settings/farm/carousel_set_cooldown_days",
            json={"value": 30},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        get_resp = await client.get("/api/settings/farm", headers=auth_headers)
        assert get_resp.json()["carousel_set_cooldown_days"] == 30


class TestPatchSetting:

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_setting(
        self, mock_save, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """PATCH /api/settings/{section}/{key} updates a config value."""
        resp = await client.patch(
            "/api/settings/server/domain",
            json={"value": "new.example.com"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["section"] == "server"
        assert body["key"] == "domain"
        assert body["value"] == "new.example.com"

        # Verify the change persists in the config
        get_resp = await client.get("/api/settings", headers=auth_headers)
        assert get_resp.json()["server"]["domain"] == "new.example.com"

        # save_config should have been called
        assert mock_save.called

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_setting_port(
        self, mock_save, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Updating a numeric setting works correctly."""
        resp = await client.patch(
            "/api/settings/server/port",
            json={"value": 7777},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == 7777

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_numeric_setting_null_resets_to_default(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Explicit null resets numeric config fields back to VPSConfig defaults."""
        resp = await client.patch(
            "/api/settings/farm",
            json={"key": "auto_retry_delay_minutes", "value": 17},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == 17
        assert test_config.farm_auto_retry_delay_minutes == 17

        reset_resp = await client.patch(
            "/api/settings/farm",
            json={"key": "auto_retry_delay_minutes", "value": None},
            headers=auth_headers,
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["value"] == 5
        assert test_config.farm_auto_retry_delay_minutes == 5
        assert mock_save.called

    @pytest.mark.asyncio
    async def test_patch_removed_placeholder_setting_returns_404(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Unsupported placeholder settings are no longer accepted as persisted config."""
        resp = await client.patch(
            "/api/settings/general",
            json={"key": "monitoring_interval_hours", "value": 6},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Unknown key" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_setting_llm(
        self, mock_save, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Updating an LLM setting works."""
        resp = await client.patch(
            "/api/settings/llm/llm_model",
            json={"value": "gpt-4o"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "gpt-4o"

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_setting_telegram_chat_ids_from_csv(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Comma-separated chat IDs are parsed into config list[int]."""
        resp = await client.patch(
            "/api/settings/farm",
            json={"key": "telegram_admin_chat_ids", "value": "333, 444,555"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "333, 444, 555"
        assert test_config.telegram_admin_chat_ids == [333, 444, 555]
        assert mock_save.called

        get_resp = await client.get("/api/settings", headers=auth_headers)
        assert get_resp.json()["farm"]["telegram_admin_chat_ids"] == "333, 444, 555"

    @pytest.mark.asyncio
    async def test_patch_setting_telegram_chat_ids_rejects_invalid_value(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """Non-numeric chat IDs are rejected instead of corrupting config."""
        resp = await client.patch(
            "/api/settings/farm",
            json={"key": "telegram_admin_chat_ids", "value": "111, not-a-number"},
            headers=auth_headers,
        )
        assert resp.status_code == 400
        assert "comma-separated list of integers" in resp.json()["detail"]

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_secret_setting_redacts_response(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Secret patches persist the new value without echoing it back."""
        resp = await client.patch(
            "/api/settings/llm",
            json={"key": "api_key", "value": "sk-live-new-secret"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == _SECRET_PLACEHOLDER
        assert test_config.llm_api_key == "sk-live-new-secret"
        assert mock_save.called

        get_resp = await client.get("/api/settings", headers=auth_headers)
        assert get_resp.json()["llm"]["api_key"] == _SECRET_PLACEHOLDER

    @pytest.mark.asyncio
    @patch("server.api.settings_api.save_config")
    async def test_patch_redacted_secret_is_noop(
        self,
        mock_save,
        client: AsyncClient,
        auth_headers: dict[str, str],
        test_config: Any,
    ) -> None:
        """Sending the placeholder back leaves the stored secret unchanged."""
        original = test_config.telegram_bot_token

        resp = await client.patch(
            "/api/settings/farm",
            json={"key": "telegram_bot_token", "value": _SECRET_PLACEHOLDER},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == _SECRET_PLACEHOLDER
        assert test_config.telegram_bot_token == original
        assert not mock_save.called

    @pytest.mark.asyncio
    async def test_patch_setting_invalid_section(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """PATCH with unknown section returns 404."""
        resp = await client.patch(
            "/api/settings/nonexistent/foo",
            json={"value": "bar"},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Unknown section" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_setting_invalid_key(
        self, client: AsyncClient, auth_headers: dict[str, str],
    ) -> None:
        """PATCH with unknown key in valid section returns 404."""
        resp = await client.patch(
            "/api/settings/server/nonexistent_key",
            json={"value": "bar"},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "Unknown key" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_setting_unauthorized(self, client: AsyncClient) -> None:
        """PATCH /api/settings without token returns 401."""
        resp = await client.patch(
            "/api/settings/server/domain",
            json={"value": "x"},
        )
        assert resp.status_code == 401
