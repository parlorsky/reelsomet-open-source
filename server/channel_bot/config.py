"""Channel Bot config adapter for VPS — reads from VPS config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ChannelConfig:
    """Per-channel settings."""
    channel_id: str = ""
    channel_description: str = ""
    legend: str = ""
    paid_media_dir: str = ""
    paid_probability: float = 0.3
    paid_star_count_min: int = 50
    paid_star_count_max: int = 100


@dataclass
class ChannelBotConfig:
    """Shared bot settings + list of channels."""
    bot_token: str = ""
    admin_chat_ids: list[int] = field(default_factory=list)
    schedule_hours: list[int] = field(default_factory=lambda: list(range(10, 24)))
    style_sample_count: int = 50
    context_window: int = 10
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_provider: str = ""
    db_path: str = ""
    timezone: str = "Europe/Moscow"
    channels: list[ChannelConfig] = field(default_factory=list)
    is_paused: bool = False
    _config_path: str = ""


def load_config(config_path: str | None = None) -> ChannelBotConfig:
    """Load channel bot config from VPS config.yaml."""
    import os
    if config_path is None:
        data_dir = os.environ.get("REELSOMET_DATA_DIR", "/opt/reelsomet")
        config_path = os.path.join(data_dir, "config.yaml")

    path = Path(config_path)
    if not path.exists():
        return ChannelBotConfig()

    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    cb = raw.get("channel_bot", {})
    llm = raw.get("llm", {})
    data_dir = raw.get("data_dir", "/opt/reelsomet/data")

    channels = [
        ChannelConfig(**{k: ch.get(k, getattr(ChannelConfig, k, "")) for k in ChannelConfig.__dataclass_fields__})
        for ch in (cb.get("channels") or [])
    ]

    return ChannelBotConfig(
        bot_token=cb.get("bot_token", ""),
        admin_chat_ids=cb.get("admin_chat_ids", []),
        schedule_hours=cb.get("schedule_hours", list(range(10, 24))),
        style_sample_count=cb.get("style_sample_count", 50),
        context_window=cb.get("context_window", 10),
        llm_provider=cb.get("llm_provider") or llm.get("provider", ""),
        llm_model=cb.get("llm_model") or llm.get("model", ""),
        llm_base_url=cb.get("llm_base_url") or llm.get("base_url", ""),
        llm_api_key=cb.get("llm_api_key") or llm.get("api_key", ""),
        db_path=cb.get("db_path") or str(Path(data_dir) / "db" / "channel_bot.db"),
        timezone=cb.get("timezone", "Europe/Moscow"),
        channels=channels,
        is_paused=cb.get("is_paused", False),
        _config_path=str(path),
    )


def save_channel_key(config: ChannelBotConfig, channel_idx: int, key: str, value: Any) -> None:
    """Persist a per-channel setting change to config.yaml."""
    path = Path(config._config_path)
    if not path.exists():
        return

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    cb = raw.setdefault("channel_bot", {})
    channels = cb.setdefault("channels", [])
    while len(channels) <= channel_idx:
        channels.append({})
    channels[channel_idx][key] = value

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    if channel_idx < len(config.channels) and hasattr(config.channels[channel_idx], key):
        setattr(config.channels[channel_idx], key, value)


def save_shared_key(config: ChannelBotConfig, key: str, value: Any) -> None:
    """Persist a shared setting change to config.yaml."""
    path = Path(config._config_path)
    if not path.exists():
        return

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw.setdefault("channel_bot", {})[key] = value
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    if hasattr(config, key):
        setattr(config, key, value)
