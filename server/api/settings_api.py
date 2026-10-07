"""Settings API: read/update server configuration."""
from __future__ import annotations

import dataclasses
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from server.config import VPSConfig, save_config
from server.dependencies import get_config, require_auth

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Sections that are exposed via the settings API (no secrets).
# SECURITY: Never expose llm_api_key, telegram_bot_token, jwt_secret,
# admin_password_hash, or license_key through this endpoint.
_EXPOSED_SECTIONS: dict[str, list[str]] = {
    # The dashboard keeps this section as informational UI only. Unsupported
    # placeholder toggles were removed so the API no longer claims they are
    # persisted server settings.
    "general": [],
    "server": ["host", "port", "domain"],
    "database": ["database_path"],
    "telegram": ["telegram_admin_chat_ids"],
    "llm": [
        "llm_provider", "llm_base_url", "llm_model", "engagement_llm_timeout",
        # Frontend aliases (without llm_ prefix)
        "provider", "base_url", "api_key", "model", "timeout",
        "max_tokens", "temperature",
    ],
    "websocket": ["ws_ping_interval", "ws_ping_timeout"],
    "farm": [
        "farm_default_posting_times",
        "farm_default_story_posting_times",
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
        # Rate-limit knobs — _check_rate_limits enforces all three.
        "farm_min_inter_post_gap_seconds",
        "farm_max_posts_per_account_per_3h",
        "farm_posts_per_week_soft_cap",
        "farm_max_stories_per_account_per_day",
        # Asset reuse anti-collision (photos + story assets).
        "farm_min_photoset_reuse_gap_minutes",
        "farm_min_storyasset_reuse_gap_minutes",
        # Sibling-account staggering + schedule horizon.
        "farm_intra_slot_spread_seconds",
        "farm_schedule_horizon_days",
        "farm_device_silent_timeout_seconds",
        # Carousel dedup — global cooldown for photo sets without a
        # per-set max_uses_per_account cap. Day count; 0 disables.
        "carousel_set_cooldown_days",
        # Story auto-sticker configuration — consumed by
        # scheduler._maybe_populate_story_element. probability in
        # [0.0, 1.0], fallbacks are free-text strings.
        "story_element_probability",
        "story_poll_fallback",
        "story_question_fallback",
        # Frontend aliases (without farm_ prefix + telegram fields)
        "telegram_bot_token", "telegram_admin_chat_ids",
        "posting_interval_minutes",
        "max_auto_retries", "auto_retry_delay_minutes",
        "action_blocked_pause_hours",
        "default_posting_times", "default_story_posting_times",
        "max_posts_per_account_per_day",
        "max_posts_per_account_per_3h", "max_stories_per_account_per_day",
        "min_inter_post_gap_seconds", "posts_per_week_soft_cap",
        "min_photoset_reuse_gap_minutes", "min_storyasset_reuse_gap_minutes",
        "intra_slot_spread_seconds", "schedule_jitter_std_seconds",
        "schedule_horizon_days", "device_silent_timeout_seconds",
        "timezone",
    ],
}

# --------------------------------------------------------------------------
# Alias mapping: frontend short key -> real VPSConfig field name
# Used in both GET (to produce alias values) and PATCH (to resolve writes).
# --------------------------------------------------------------------------
_ALIAS_TO_CONFIG: dict[str, dict[str, str]] = {
    "llm": {
        "provider": "llm_provider",
        "base_url": "llm_base_url",
        "api_key": "llm_api_key",
        "model": "llm_model",
        "timeout": "engagement_llm_timeout",
        "max_tokens": "llm_max_tokens",
        "temperature": "llm_temperature",
    },
    "farm": {
        "timezone": "farm_timezone",
        "max_auto_retries": "farm_max_auto_retries",
        "auto_retry_delay_minutes": "farm_auto_retry_delay_minutes",
        "action_blocked_pause_hours": "farm_action_blocked_pause_hours",
        "posting_interval_minutes": "farm_upload_window_minutes",
        "default_posting_times": "farm_default_posting_times",
        "default_story_posting_times": "farm_default_story_posting_times",
        "max_posts_per_account_per_day": "farm_max_posts_per_account_per_day",
        "max_posts_per_account_per_3h": "farm_max_posts_per_account_per_3h",
        "max_stories_per_account_per_day": "farm_max_stories_per_account_per_day",
        "min_inter_post_gap_seconds": "farm_min_inter_post_gap_seconds",
        "posts_per_week_soft_cap": "farm_posts_per_week_soft_cap",
        "min_photoset_reuse_gap_minutes": "farm_min_photoset_reuse_gap_minutes",
        "min_storyasset_reuse_gap_minutes": "farm_min_storyasset_reuse_gap_minutes",
        "intra_slot_spread_seconds": "farm_intra_slot_spread_seconds",
        "schedule_jitter_std_seconds": "farm_schedule_jitter_std_seconds",
        "schedule_horizon_days": "farm_schedule_horizon_days",
        "device_silent_timeout_seconds": "farm_device_silent_timeout_seconds",
        # telegram fields are actual config field names, no mapping needed
    },
}

_SECRET_PLACEHOLDER = "__REDACTED__"
_SECRET_CONFIG_FIELDS = {
    "admin_password_hash",
    "api_key",
    "jwt_secret",
    "license_key",
    "llm_api_key",
    "setup_token",
    "telegram_bot_token",
}
_CSV_INT_CONFIG_FIELDS = {"telegram_admin_chat_ids"}


def _serialize_setting_value(config_key: str, value: Any) -> Any:
    """Prevent configured secrets from leaking through API responses."""
    if config_key in _SECRET_CONFIG_FIELDS:
        return _SECRET_PLACEHOLDER if value else ""
    if config_key in _CSV_INT_CONFIG_FIELDS:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
    return value


def _resolve_config_key(section: str, key: str) -> str:
    """Map a frontend alias key to the real VPSConfig attribute name."""
    return _ALIAS_TO_CONFIG.get(section, {}).get(key, key)


def _coerce_special_value(config_key: str, key: str, value: Any) -> Any:
    """Handle non-scalar config fields that need dashboard-friendly shapes."""
    if config_key not in _CSV_INT_CONFIG_FIELDS:
        return value

    if value is None:
        return []

    if isinstance(value, list):
        raw_parts = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        raw_parts = [part for part in re.split(r"[\s,]+", text) if part]
    else:
        raise HTTPException(400, f"'{key}' expects a comma-separated list of integers")

    parsed: list[int] = []
    for part in raw_parts:
        try:
            parsed.append(int(part))
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{key}' expects a comma-separated list of integers")
    return parsed


def _get_config_default(config: VPSConfig, config_key: str) -> Any:
    """Return the dataclass default for a config field."""
    for field_def in dataclasses.fields(config):
        if field_def.name != config_key:
            continue
        if field_def.default is not dataclasses.MISSING:
            return field_def.default
        if field_def.default_factory is not dataclasses.MISSING:
            return field_def.default_factory()
        break
    raise HTTPException(400, f"'{config_key}' cannot be empty")


def _serialize_section(config: VPSConfig, section: str) -> dict[str, Any]:
    """Return one exposed settings section with aliases/defaults applied."""
    if section not in _EXPOSED_SECTIONS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown section: {section}",
        )

    out: dict[str, Any] = {}
    for key in _EXPOSED_SECTIONS[section]:
        config_key = _resolve_config_key(section, key)
        out[key] = _serialize_setting_value(
            config_key,
            getattr(config, config_key, None),
        )
    return out


class SettingPatch(BaseModel):
    value: Any


@router.get("")
async def get_settings(
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Return the current server configuration grouped by section."""
    return {
        section: _serialize_section(config, section)
        for section in _EXPOSED_SECTIONS
    }


@router.get("/{section}")
async def get_settings_section(
    section: str,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Return one exposed settings section for the section-level client contract."""
    return _serialize_section(config, section)


class SectionPatch(BaseModel):
    """Body for PATCH /settings/{section}: key+value in body."""

    key: str
    value: Any = None


@router.patch("/{section}")
async def patch_setting_from_body(
    section: str,
    body: SectionPatch,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Update a setting with key specified in the request body."""
    return await patch_setting(section, body.key, SettingPatch(value=body.value), config=config)


@router.patch("/{section}/{key}")
async def patch_setting(
    section: str,
    key: str,
    body: SettingPatch,
    _user: dict = Depends(require_auth),
    config: VPSConfig = Depends(get_config),
) -> dict[str, Any]:
    """Update a single configuration value and persist to disk."""
    if section not in _EXPOSED_SECTIONS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown section: {section}",
        )
    if key not in _EXPOSED_SECTIONS[section]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown key '{key}' in section '{section}'",
        )

    # Resolve alias to real config attribute
    config_key = _resolve_config_key(section, key)

    if not hasattr(config, config_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config field '{config_key}' not found",
        )

    if config_key in _SECRET_CONFIG_FIELDS and body.value == _SECRET_PLACEHOLDER:
        return {
            "section": section,
            "key": key,
            "value": _serialize_setting_value(config_key, getattr(config, config_key, None)),
        }

    # Coerce value to the expected type of the config field
    value = body.value
    if config_key in _SECRET_CONFIG_FIELDS and value is None:
        value = ""
    value = _coerce_special_value(config_key, key, value)
    if value is None and config_key not in _SECRET_CONFIG_FIELDS:
        value = _get_config_default(config, config_key)
    for field_def in dataclasses.fields(config):
        if field_def.name == config_key:
            expected_type = field_def.type
            # Handle common type coercions
            if expected_type in ("int", int) and not isinstance(value, int):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    raise HTTPException(400, f"'{key}' expects an integer")
            elif expected_type in ("float", float) and not isinstance(value, (int, float)):
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise HTTPException(400, f"'{key}' expects a number")
            elif expected_type in ("bool", bool) and not isinstance(value, bool):
                if isinstance(value, str):
                    value = value.lower() in ("true", "1", "yes")
                else:
                    value = bool(value)
            break

    setattr(config, config_key, value)
    save_config(config)
    return {
        "section": section,
        "key": key,
        "value": _serialize_setting_value(config_key, value),
    }
