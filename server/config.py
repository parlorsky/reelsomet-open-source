"""VPS configuration system.

Loads from YAML config file with sensible defaults for all fields.
Config path resolution: CLI arg > REELSOMET_DATA_DIR env > /opt/reelsomet/config.yaml.
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from server.farm_time import DEFAULT_FARM_TIMEZONE

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path("var/config.yaml")


def _resolve_config_path() -> Path:
    env_dir = os.environ.get("REELSOMET_DATA_DIR")
    if env_dir:
        return Path(env_dir) / "config.yaml"
    return _DEFAULT_CONFIG_PATH


@dataclass
class VPSConfig:
    """All VPS server settings with sensible defaults."""

    # Server
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = ""

    # WebSocket
    ws_ping_interval: float = 30.0
    ws_ping_timeout: float = 10.0

    # Database
    database_path: str = "var/data/db/farm.db"

    # Data directories
    data_dir: str = "var/data"
    static_dir: str = "web/dist"

    # Domain (for TLS / reverse proxy)
    domain: str = ""

    # Admin auth
    admin_password_hash: str = ""
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    jwt_expire_hours: int = 24
    setup_token: str = ""

    # Device tokens: {device_id: bearer_token}
    device_tokens: dict[int, str] = field(default_factory=dict)

    # Telegram
    telegram_bot_token: str = ""
    telegram_admin_chat_ids: list[int] = field(default_factory=list)

    # LLM
    llm_provider: str = ""  # "openai", "grok", "openrouter", "anthropic"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.7
    engagement_llm_timeout: float = 30.0

    # License
    license_key: str = ""

    # Update (git-based self-update)
    git_remote_url: str = ""

    # Farm controller
    farm_default_posting_times: list[str] = field(
        default_factory=lambda: ["09:00", "12:00", "15:00", "18:00", "21:00"],
    )
    # Global default story wall-clock slots, used when an account has
    # no per-account `story_posting_times` override. Symmetric with
    # `farm_default_posting_times` above — two slots keep stories
    # spread across the day without overlapping the reel peaks.
    farm_default_story_posting_times: list[str] = field(
        default_factory=lambda: ["10:00", "19:00"],
    )
    farm_timezone: str = DEFAULT_FARM_TIMEZONE
    farm_max_posts_per_account_per_day: int = 5
    # Hardening step 6: behavioral rate limits. The single "posts per day" cap
    # wasn't granular enough — 5 posts packed into 30 minutes and 5 posts spread
    # across 18 hours look very different to IG. The account suspended on
    # 2026-04-12 posted 6 stories in 98 min, which tripped IG's burst heuristic.
    #
    # These three knobs enforce a minimum temporal spacing on top of the
    # daily cap, per-account (with per-account overrides possible later via
    # the accounts table).
    farm_max_posts_per_account_per_3h: int = 2     # hard story-burst cap
    farm_min_inter_post_gap_seconds: int = 1200    # 20 min minimum between posts
    farm_posts_per_week_soft_cap: int = 30         # soft weekly cap (warns; doesn't block)
    # Sibling-gap check (T1): no two carousel dispatches referencing the same
    # photo_set_id on the same physical device may be closer than N minutes
    # apart. Motivated by the 2026-04-14 Usage History screenshot where 5
    # siblings showed the same photo set within 2 seconds — the cross-account
    # fingerprint is already randomised per-dispatch (DonorUsage / Option E),
    # but the POSTING PATTERN of 5 near-identical carousels in 15 min is
    # itself a cluster signal. Dispatch-time only (not schedule-time); fails
    # the attempt silently and retries on the next 30s poll tick.
    farm_min_photoset_reuse_gap_minutes: int = 45
    # Device IDs that are EXEMPT from the three rate-limit checks above
    # (min gap, 3h burst, daily cap). Intended for E2E test rigs only — do
    # not put production devices here or a burst ban will follow. An exempt
    # device still logs [RATE_LIMIT] bypass lines on every push so the
    # audit trail survives even though the limits don't fire.
    farm_rate_limit_exempt_devices: list[int] = field(default_factory=list)
    farm_upload_window_minutes: int = 10
    farm_max_auto_retries: int = 3
    # Consecutive "A11y service not running" post failures on the same
    # device that trigger quarantine (status='a11y_failure' + Telegram
    # alert + dispatch pause). Set to 0 to disable the quarantine. The
    # heartbeat-ground-truth cross-check still happens regardless.
    farm_a11y_failure_threshold: int = 2
    farm_auto_retry_delay_minutes: int = 5
    # Hardening step 7: 48h was lenient — Codex recommended ≥72h quarantine so
    # the account has time to fully cool down and any challenge/checkpoint
    # flow clears server-side. Per-account override via Account.action_blocked_pause_hours
    # still takes precedence for accounts with tighter/looser policies.
    farm_action_blocked_pause_hours: float = 72.0
    farm_schedule_jitter_std_seconds: int = 180
    # How many days ahead auto_schedule_videos should look when
    # distributing pending videos across a per-account slot grid.
    # Default is 7: most accounts post a handful of times per day and
    # with stride-based uniform distribution we want to smear the
    # backlog over the whole week instead of packing it into "today".
    farm_schedule_horizon_days: int = 7
    # When multiple accounts share a device, stagger their `scheduled_time`
    # within the same posting slot by this many seconds per sibling. A value
    # of 0 disables spreading and everyone lands on the same wall-clock
    # minute (legacy behaviour). Non-zero also disables random jitter in the
    # slot computation so the ordering stays stable.
    farm_intra_slot_spread_seconds: int = 240
    farm_poll_interval_seconds: int = 30
    farm_health_check_interval_seconds: int = 60
    farm_result_poll_interval_seconds: int = 15
    farm_device_silent_timeout_seconds: int = 90

    # ─── Insights collection overhaul (2026-04-24) ───────────────────
    #
    # Per-video decay ladder — how often each reel's insights should
    # be refreshed as it ages. List of (max_age_hours, interval_hours)
    # pairs sorted ascending by max_age_hours; the FIRST bracket the
    # video fits into wins. Anything older than the last bracket
    # falls back to `insights_decay_fallback_hours`. Tunes the
    # staleness term of `priority_score = staleness × volatility ×
    # freshness` in the new auto_start_insights.
    # list of [max_age_hours, interval_hours] — stored as nested list
    # (not tuple) for YAML safe_dump/safe_load round-trip.
    insights_decay_brackets: list[list[float]] = field(
        default_factory=lambda: [
            [1.0, 0.75],     # <1h old → sample once at ~45 min
            [6.0, 2.0],      # 1–6h → every 2h
            [24.0, 6.0],     # 6–24h → every 6h
            [72.0, 12.0],    # 1–3d → every 12h
            [168.0, 24.0],   # 3–7d → daily
            [720.0, 72.0],   # 7–30d → every 3 days
            [2160.0, 168.0], # 30–90d → weekly
        ]
    )
    insights_decay_fallback_hours: float = 720.0  # >90d: monthly

    # Top-K reels to collect per session. The scheduler picks by
    # `priority_score` DESC from due plans; too-large K makes sessions
    # long and flaky, too-small bunches up the backlog.
    insights_reels_per_session: int = 8
    # Per-account floor between consecutive sessions (minutes). Avoids
    # thrash when many reels become due simultaneously.
    insights_min_session_gap_minutes: int = 45
    # Coalesce window (minutes) — when starting a session, pull in any
    # reel that will become due within this window so one profile
    # visit sweeps a cluster of near-due videos.
    insights_coalesce_window_minutes: int = 30
    # Grace period (minutes) — if any posting is due in the next N
    # minutes on this device, skip insights to avoid preempting.
    insights_posting_grace_minutes: int = 10
    # Freshness boost halflife (hours): `exp(-age_h / halflife)`.
    # Smaller → fresh videos dominate more aggressively.
    insights_freshness_halflife_hours: float = 24.0
    # Volatility weight: `priority *= (1 + w × volatility)` where
    # volatility = |Δplays_last| / max(plays_last, 1).
    insights_volatility_weight: float = 1.0
    # Dead-reel retirement: after N consecutive snapshots with zero
    # deltas (all metrics unchanged) on a reel older than 7 d, flip
    # `Video.insights_retired = True`.
    insights_dead_streak_threshold: int = 3
    # Consecutive "reel not found in grid" misses before retirement.
    insights_missing_from_feed_threshold: int = 2
    # Retention windows for the daily prune job.
    insights_retention_full_days: int = 90         # keep raw snapshots N days
    insights_retention_daily_days: int = 275       # keep daily rollups N more days, then purge
    # Max videos per account to track in insights_collection_plan.
    # Older beyond this → implicitly dead, plan entry dropped.
    insights_max_videos_per_account: int = 60
    # Global default for the tri-state ``Account.use_scenarios`` override.
    # True (default) preserves legacy behavior — auto_generate_videos runs
    # the scenario picker for every account. False switches the whole
    # farm to raw mode by default: accounts dispatch whatever is
    # pre-uploaded into ``data_dir/videos/<account>/`` and do NOT
    # generate new reels from scenarios. Per-account overrides win.
    # (T8: raw-video mode — Codex flag 4.)
    farm_use_scenarios_default: bool = False

    # Ghost pipeline (media fingerprint randomization)
    ghost_enabled: bool = False
    ghost_workers: int = 4  # Carousel photo parallelism (was 1; aligned with scheduler default)
    ghost_ssim_threshold: float = 0.90

    # Carousel photo sets
    carousel_set_cooldown_days: int = 14
    carousel_min_photos: int = 3
    carousel_max_photos: int = 5
    carousel_caption_fallback: str = "Pick your favorite 💋"

    # Story assets (single-file photo/video story posts). Separate
    # reuse-gap knob from carousels so operators can throttle story
    # reuse independently. 30 minutes is the default minimum gap
    # before the same StoryAsset can be reused on any given
    # account, measured against the last dispatched_at row in
    # story_asset_usage.
    farm_min_storyasset_reuse_gap_minutes: int = 30

    # Story interactive elements
    story_element_probability: float = 0.7
    story_poll_fallback: str = "Like this? | Yes | No"
    story_question_fallback: str = "Ask me anything 👀"
    # Global story cadence defaults (per-account nullable columns override)
    farm_max_stories_per_account_per_day: int = 3
    story_element_weights_default: str = '{"poll":0.5,"question":0.3,"text":0.2}'

    # LLM fallback policy
    # When False (default), call sites silently use their static fallbacks when
    # no LLM provider is configured or the provider fails. When True, each call
    # site still falls back but logs a WARNING — useful to detect drift in
    # staging environments where an LLM is expected.
    farm_llm_required: bool = False

    # Channel Bot paid-post caption fallback — returned by generate_paid_caption
    # when llm_provider is unset so the channel bot can still drop paid posts.
    channel_bot_paid_caption_fallback: str = "Tap to unlock 💋"

    # Internal: path the config was loaded from (not persisted)
    _config_path: str = field(default="", repr=False)


def _get_nested(data: dict[str, Any], dot_path: str) -> Any:
    """Retrieve a value from a nested dict using dot-notation (e.g. 'server.host')."""
    keys = dot_path.split(".")
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise KeyError(dot_path)
        current = current[key]
    return current


def _set_nested(data: dict[str, Any], dot_path: str, value: Any) -> None:
    """Set a value in a nested dict using dot-notation, creating intermediates as needed."""
    keys = dot_path.split(".")
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


# Mapping: VPSConfig field name -> YAML dot-notation path
_FIELD_TO_YAML: dict[str, str] = {
    "host": "server.host",
    "port": "server.port",
    "api_key": "server.api_key",
    "ws_ping_interval": "websocket.ping_interval",
    "ws_ping_timeout": "websocket.ping_timeout",
    "database_path": "database.path",
    "data_dir": "data_dir",
    "static_dir": "static_dir",
    "domain": "server.domain",
    "admin_password_hash": "auth.admin_password_hash",
    "jwt_secret": "auth.jwt_secret",
    "jwt_expire_hours": "auth.jwt_expire_hours",
    "setup_token": "auth.setup_token",
    "telegram_bot_token": "telegram.bot_token",
    "telegram_admin_chat_ids": "telegram.admin_chat_ids",
    "llm_provider": "llm.provider",
    "llm_base_url": "llm.base_url",
    "llm_api_key": "llm.api_key",
    "llm_model": "llm.model",
    "llm_max_tokens": "llm.max_tokens",
    "llm_temperature": "llm.temperature",
    "engagement_llm_timeout": "llm.engagement_timeout",
    "device_tokens": "auth.device_tokens",
    "farm_default_posting_times": "farm.default_posting_times",
    "farm_default_story_posting_times": "farm.default_story_posting_times",
    "farm_timezone": "farm.timezone",
    "farm_max_posts_per_account_per_day": "farm.max_posts_per_account_per_day",
    "farm_max_posts_per_account_per_3h": "farm.max_posts_per_account_per_3h",
    "farm_min_inter_post_gap_seconds": "farm.min_inter_post_gap_seconds",
    "farm_min_photoset_reuse_gap_minutes": "farm.min_photoset_reuse_gap_minutes",
    "farm_rate_limit_exempt_devices": "farm.rate_limit_exempt_devices",
    "farm_posts_per_week_soft_cap": "farm.posts_per_week_soft_cap",
    "farm_upload_window_minutes": "farm.upload_window_minutes",
    "farm_max_auto_retries": "farm.max_auto_retries",
    "farm_a11y_failure_threshold": "farm.a11y_failure_threshold",
    "farm_auto_retry_delay_minutes": "farm.auto_retry_delay_minutes",
    "farm_action_blocked_pause_hours": "farm.action_blocked_pause_hours",
    "farm_schedule_jitter_std_seconds": "farm.schedule_jitter_std_seconds",
    "farm_schedule_horizon_days": "farm.schedule_horizon_days",
    "farm_intra_slot_spread_seconds": "farm.intra_slot_spread_seconds",
    "farm_poll_interval_seconds": "farm.poll_interval_seconds",
    "farm_health_check_interval_seconds": "farm.health_check_interval_seconds",
    "farm_result_poll_interval_seconds": "farm.result_poll_interval_seconds",
    "farm_device_silent_timeout_seconds": "farm.device_silent_timeout_seconds",

    # Insights overhaul (2026-04-24)
    "insights_decay_brackets": "insights.decay_brackets",
    "insights_decay_fallback_hours": "insights.decay_fallback_hours",
    "insights_reels_per_session": "insights.reels_per_session",
    "insights_min_session_gap_minutes": "insights.min_session_gap_minutes",
    "insights_coalesce_window_minutes": "insights.coalesce_window_minutes",
    "insights_posting_grace_minutes": "insights.posting_grace_minutes",
    "insights_freshness_halflife_hours": "insights.freshness_halflife_hours",
    "insights_volatility_weight": "insights.volatility_weight",
    "insights_dead_streak_threshold": "insights.dead_streak_threshold",
    "insights_missing_from_feed_threshold": "insights.missing_from_feed_threshold",
    "insights_retention_full_days": "insights.retention_full_days",
    "insights_retention_daily_days": "insights.retention_daily_days",
    "insights_max_videos_per_account": "insights.max_videos_per_account",
    "farm_use_scenarios_default": "farm.use_scenarios_default",
    "license_key": "license.key",
    "git_remote_url": "update.git_remote_url",
    # Ghost
    "ghost_enabled": "ghost.enabled",
    "ghost_workers": "ghost.workers",
    "ghost_ssim_threshold": "ghost.ssim_threshold",
    # Carousel
    "carousel_set_cooldown_days": "carousel.set_cooldown_days",
    "carousel_min_photos": "carousel.min_photos",
    "carousel_max_photos": "carousel.max_photos",
    "carousel_caption_fallback": "carousel.caption_fallback",
    # Story assets
    "farm_min_storyasset_reuse_gap_minutes": "farm.min_storyasset_reuse_gap_minutes",
    # Story
    "story_element_probability": "story.element_probability",
    "story_poll_fallback": "story.poll_fallback",
    "story_question_fallback": "story.question_fallback",
    "farm_max_stories_per_account_per_day": "farm.max_stories_per_account_per_day",
    "story_element_weights_default": "story.element_weights_default",
    # LLM fallback policy (T6)
    "farm_llm_required": "farm.llm_required",
    "channel_bot_paid_caption_fallback": "channel_bot.paid_caption_fallback",
}


def load_config(path: Path | None = None) -> VPSConfig:
    """Load VPS config from a YAML file, falling back to defaults for missing keys."""
    config_path = path or _resolve_config_path()
    config = VPSConfig()
    config._config_path = str(config_path)

    if not config_path.exists():
        logger.warning("Config file not found at %s — using defaults", config_path)
        return config

    with open(config_path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    field_types = {f.name: f.type for f in fields(VPSConfig) if not f.name.startswith("_")}

    for field_name, yaml_path in _FIELD_TO_YAML.items():
        try:
            value = _get_nested(raw, yaml_path)
        except KeyError:
            continue
        expected_type = field_types.get(field_name, "str")
        if expected_type == "int" and isinstance(value, float) and value == int(value):
            value = int(value)
        # device_tokens: YAML loads keys as int or str, normalize to {int: str}
        if field_name == "device_tokens" and isinstance(value, dict):
            value = {int(k): str(v) for k, v in value.items()}
        setattr(config, field_name, value)

    return config


def save_config(config: VPSConfig, path: Path | None = None) -> None:
    """Persist the full config back to YAML."""
    if path is not None:
        config_path = path
    elif config._config_path:
        config_path = Path(config._config_path)
    else:
        config_path = _resolve_config_path()

    # Load existing YAML to preserve comments/extra keys
    existing: dict[str, Any] = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}

    for field_name, yaml_path in _FIELD_TO_YAML.items():
        value = getattr(config, field_name)
        _set_nested(existing, yaml_path, value)

    # Atomic write-to-tmp + os.replace so a crash mid-write does
    # NOT leave the live config.yaml truncated/empty (Codex iter 15
    # bug hunt 2026-04-14). A corrupted config makes the server
    # fail to start on the next boot — high-impact silent failure.
    import os as _os
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            try:
                _os.fsync(f.fileno())
            except OSError:
                pass
        _os.chmod(tmp_path, 0o600)
        _os.replace(tmp_path, config_path)
    except Exception:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("Config saved to %s", config_path)
