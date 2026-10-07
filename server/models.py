"""SQLAlchemy 2.0 declarative models for the Reelsomet VPS database.

Standalone models (no imports from pc.farm) so the VPS can run independently.
All models use async-compatible patterns with mapped_column.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all VPS models."""
    pass


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False, default="")
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=8080)
    device_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    android_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    app_version: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="offline")
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    # Relationships
    account_links: Mapped[list[AccountDevice]] = relationship(
        "AccountDevice", back_populates="device", cascade="all, delete-orphan",
    )
    videos: Mapped[list[Video]] = relationship(
        "Video", back_populates="device", foreign_keys="Video.device_id",
    )
    post_logs: Mapped[list[PostLog]] = relationship(
        "PostLog", back_populates="device", foreign_keys="PostLog.device_id",
    )

    def __repr__(self) -> str:
        return f"<Device id={self.id} device_id={self.device_id!r} status={self.status!r}>"


class DeviceFsmState(Base):
    """ORM wrapper for the existing device_fsm_state live-state table."""

    __tablename__ = "device_fsm_state"

    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True,
    )
    fsm_kind: Mapped[str] = mapped_column(String(128), primary_key=True)
    current_state: Mapped[str] = mapped_column(Text, nullable=False)
    state_entered_at: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    account: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_event_id: Mapped[str] = mapped_column(Text, nullable=False)
    last_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[int] = mapped_column(Integer, nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_posted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Instagram credentials (encrypted-at-rest via app-level encryption)
    ig_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ig_2fa_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Posting config
    posting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    posting_times: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    max_posts_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Recreator config
    recreator_model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    recreator_video_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    gen_style_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Insights config
    insights_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    insights_interval_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    insights_max_reels: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_insights_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Engagement config
    engagement_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_engagement_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    engagement_like_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engagement_comment_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engagement_reply_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engagement_share_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    engagement_daily_budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    engagement_sessions_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    engagement_max_reels: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    engagement_follow: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Profile stats (scraped from Instagram profile page)
    followers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    following: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Retry config (nullable per-account overrides)
    max_auto_retries: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    auto_retry_delay_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_blocked_pause_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Story cadence overrides (null = global defaults) — T4
    story_max_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    story_element_probability: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    story_element_weights: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Story wall-clock schedule, parallel to `posting_times`.
    # Null = fall back to `farm_default_story_posting_times` global.
    story_posting_times: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Tri-state override for the reel generation path — T8.
    # NULL = fall back to `farm_use_scenarios_default` in config;
    # True = run the scenario picker (auto_generate_videos);
    # False = skip generation entirely and post only pre-uploaded raw
    # videos from data_dir/videos/<account>/.
    use_scenarios: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Relationships
    device_links: Mapped[list[AccountDevice]] = relationship(
        "AccountDevice", back_populates="account", cascade="all, delete-orphan",
    )
    videos: Mapped[list[Video]] = relationship(
        "Video", back_populates="account", foreign_keys="Video.account_username",
    )
    engagement_targets: Mapped[list[EngagementTarget]] = relationship(
        "EngagementTarget", back_populates="account", cascade="all, delete-orphan",
    )
    monitor_targets: Mapped[list[MonitorTarget]] = relationship(
        "MonitorTarget", back_populates="account", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} username={self.username!r}>"


class AccountDevice(Base):
    __tablename__ = "account_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_username: Mapped[str] = mapped_column(
        String(128), ForeignKey("accounts.username", ondelete="CASCADE"), nullable=False, index=True,
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    account: Mapped[Account] = relationship("Account", back_populates="device_links")
    device: Mapped[Device] = relationship("Device", back_populates="account_links")

    def __repr__(self) -> str:
        return f"<AccountDevice account={self.account_username!r} device_id={self.device_id}>"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    original_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    account_username: Mapped[str] = mapped_column(
        String(128), ForeignKey("accounts.username", ondelete="CASCADE"), nullable=False, index=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    phone_video_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="reel", server_default="reel",
    )
    image_filenames: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    story_element: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON for sticker
    scheduled_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True,
    )
    uploaded_to_phone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    upload_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    post_result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    post_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    post_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, onupdate=func.now(),
    )
    generation_run_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("generation_runs.id", ondelete="SET NULL"), nullable=True,
    )
    transcript: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Stats (populated from insights)
    stats_plays: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_saves: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_reach: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_engaged: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_profile_visits: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_follows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stats_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Insights identity + lifecycle (2026-04-24 insights overhaul)
    #
    # - `insights_video_marker`: ZWSP-encoded Video.id injected into the
    #   caption at posting time, scraped back at insights collection.
    #   Primary identity signal — exact, immutable unless user edits the
    #   leading chars of the caption.
    # - `insights_caption_hash`: SHA-256 of normalized caption. Secondary
    #   identity signal for legacy posts (pre-watermark) and re-posts.
    # - `insights_published_label`: raw IG subtitle text ("February 4 ·
    #   Duration 0:36") — cached first time we see the reel; lets us
    #   disambiguate same-caption reposts by publish date.
    # - `insights_published_at`: parsed form of `insights_published_label`
    #   when we can (IG's format is locale-specific).
    # - `insights_retired`: flipped true when the reel has been missing
    #   from the feed or returning zero deltas for N consecutive pulls.
    # - `insights_last_position`: last grid position observed (scroll hint
    #   for the phone — not the identity key).
    # - `insights_collection_count`: lifetime audit counter.
    # - `insights_dead_streak`: consecutive "no Δ" pulls; threshold flips
    #   `insights_retired`.
    insights_video_marker: Mapped[Optional[str]] = mapped_column(String(48), nullable=True, index=True)
    insights_caption_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    insights_published_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    insights_published_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    insights_retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    insights_retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    insights_retired_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    insights_last_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    insights_last_collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    insights_collection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insights_dead_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Relationships
    account: Mapped[Account] = relationship("Account", back_populates="videos")
    device: Mapped[Optional[Device]] = relationship("Device", back_populates="videos")
    generation_run: Mapped[Optional[GenerationRun]] = relationship(
        "GenerationRun", back_populates="videos",
    )
    post_logs: Mapped[list[PostLog]] = relationship(
        "PostLog", back_populates="video", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Video id={self.id} filename={self.filename!r} status={self.status!r}>"


class GenerationRun(Base):
    __tablename__ = "generation_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    format: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    requested_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    videos: Mapped[list[Video]] = relationship("Video", back_populates="generation_run")

    def __repr__(self) -> str:
        return f"<GenerationRun id={self.id} status={self.status!r}>"


class InsightsSnapshot(Base):
    __tablename__ = "insights_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True,
    )
    account_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    phone_snapshot_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption_snippet: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    reel_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Metrics
    plays: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    saves: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reposts: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reach: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    engaged: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    profile_visits: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    follows: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    watch_time_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_watch_time_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    skip_rate_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    followers_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    non_followers_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Identity audit (2026-04-24). Populated by the new layered-matching
    # logic in scheduler._upsert_insights_snapshot so we can see per-
    # snapshot how confident we are in the `video_id` link.
    # match_strategy ∈ {"marker", "hash_label", "position", "metric_band",
    # "unmatched", "legacy_contains"}.
    match_strategy: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)
    match_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    retention_curve_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Hash of the relevant insights-screen region (to detect IG UI drift).
    insights_screen_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<InsightsSnapshot id={self.id} account={self.account_username!r}>"


# ── Insights collection plan + daily rollup (2026-04-24) ──────────
#
# `insights_collection_plan` is a hot-path lookup table: the scheduler
# reads "what to collect next" with a single indexed query
# (ORDER BY next_due_at, priority_score DESC). Populated when a Video
# transitions to status='posted' and updated after every InsightsSnapshot
# is persisted. One row per live video; dropped (CASCADE) when Video is
# deleted or `insights_retired=true`.
#
# `insights_snapshot_daily` is the downsampled archive: after 7 days of
# raw snapshots we keep one row per (video_id, date) with end-of-day
# counters and daily deltas. Raw rows older than 90 days are purged.
class InsightsCollectionPlan(Base):
    __tablename__ = "insights_collection_plan"

    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    account_username: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True,
    )
    next_due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_staleness_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Target interval (hours) picked from the decay ladder at last update.
    # Kept here so debug reports don't re-derive it from posted_at.
    target_interval_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, onupdate=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class InsightsSnapshotDaily(Base):
    __tablename__ = "insights_snapshot_daily"

    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"),
        primary_key=True,
    )
    date: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    plays_end_of_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    saves_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reach_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_watch_time_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    skip_rate_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)


class EngagementSession(Base):
    __tablename__ = "engagement_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_session_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    account_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    channels_visited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reels_watched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_likes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_comments: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_replies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_follows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_shares: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    actions: Mapped[list[EngagementAction]] = relationship(
        "EngagementAction", back_populates="session", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<EngagementSession id={self.id} account={self.account_username!r} status={self.status!r}>"


class EngagementAction(Base):
    __tablename__ = "engagement_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone_action_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("engagement_sessions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    account_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_username: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reel_caption: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    comment_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    performed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    session: Mapped[EngagementSession] = relationship("EngagementSession", back_populates="actions")

    def __repr__(self) -> str:
        return f"<EngagementAction id={self.id} type={self.action_type!r}>"


class EngagementTarget(Base):
    __tablename__ = "engagement_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_username: Mapped[str] = mapped_column(String(128), nullable=False)
    account_username: Mapped[str] = mapped_column(
        String(128), ForeignKey("accounts.username", ondelete="CASCADE"), nullable=False, index=True,
    )
    max_reels: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    should_follow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Relationships
    account: Mapped[Account] = relationship("Account", back_populates="engagement_targets")

    def __repr__(self) -> str:
        return f"<EngagementTarget id={self.id} target={self.target_username!r}>"


class MonitorTarget(Base):
    __tablename__ = "monitor_targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_username: Mapped[str] = mapped_column(String(128), nullable=False)
    account_username: Mapped[str] = mapped_column(
        String(128), ForeignKey("accounts.username", ondelete="CASCADE"), nullable=False, index=True,
    )
    max_reels: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_monitored_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    account: Mapped[Account] = relationship("Account", back_populates="monitor_targets")

    def __repr__(self) -> str:
        return f"<MonitorTarget id={self.id} target={self.target_username!r}>"


class MonitorSnapshot(Base):
    __tablename__ = "monitor_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    account_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reel_position: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    caption_snippet: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    plays: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    likes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comments: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<MonitorSnapshot id={self.id} target={self.target_username!r}>"


class PostLog(Base):
    __tablename__ = "post_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("videos.id", ondelete="SET NULL"), nullable=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    account_username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    phone_log_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    # Relationships
    video: Mapped[Optional[Video]] = relationship("Video", back_populates="post_logs")
    device: Mapped[Optional[Device]] = relationship("Device", back_populates="post_logs")

    def __repr__(self) -> str:
        return f"<PostLog id={self.id} result={self.result!r}>"


# ── Carousel Photo Sets ─────────────────────────────────────────


class PhotoSet(Base):
    __tablename__ = "photo_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Per-account post cap. NULL = fall back to global cooldown
    # (carousel_set_cooldown_days). A positive integer N = hard cap:
    # once any given account has posted this set N times (over all time),
    # the scheduler permanently excludes the set for that account.
    # Set to 1 for "one-shot" sets (post once per account, never reuse).
    max_uses_per_account: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )

    images: Mapped[list[PhotoSetImage]] = relationship(
        "PhotoSetImage", back_populates="photo_set", cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<PhotoSet id={self.id} name={self.name!r} model={self.model!r}>"


class PhotoSetImage(Base):
    __tablename__ = "photo_set_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photo_sets.id", ondelete="CASCADE"), nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    photo_set: Mapped[PhotoSet] = relationship("PhotoSet", back_populates="images")


class PhotoSetUsage(Base):
    __tablename__ = "photo_set_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    set_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photo_sets.id"), nullable=False,
    )
    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("videos.id"), nullable=False,
    )
    account_username: Mapped[str] = mapped_column(String(128), nullable=False)
    # Generation-time clock: stamped when _maybe_generate_carousel creates the
    # Video row. Kept for backward compat with existing UI that reads this.
    used_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    # Dispatch-time clock: set AFTER bridge.send_carousel_download acks.
    # Null until the phone actually receives the command, which is the real
    # "this photo set left the VPS for device N" event. The sibling-gap check
    # and the admin Usage History UI both read from this column.
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True,
    )
    # Post-result clock: set when the phone reports a successful post for the
    # owning Video row. Used by the UI to show the full lifecycle.
    posted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True,
    )


class DonorUsage(Base):
    """Per-device LRU of recently-used iPhone donor rows.

    Populated after a successful carousel dispatch so future sibling posts
    on the same device within a 72-hour window skip donors that already
    appeared on that cluster. Prevents observable cross-account metadata
    fingerprint collisions in the 149-row donor pool.

    (model_key, row_index) references positions inside
    ``ghostcli.profiles._donor_pool_cache``; stable across process
    restarts as long as ``donor_pool.json`` is not rewritten. Stale rows
    are dropped by ``FarmScheduler.prune_donor_usage`` every 24 hours.
    """
    __tablename__ = "donor_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False,
    )
    donor_model: Mapped[str] = mapped_column(String(64), nullable=False)
    donor_index: Mapped[int] = mapped_column(Integer, nullable=False)
    used_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


# ── Story Assets ────────────────────────────────────────────────


class StoryAsset(Base):
    """Single-file story asset (photo or video) usable as an IG story.

    Unlike PhotoSet (which is a multi-image carousel pack), each
    StoryAsset represents exactly one media file that gets posted as a
    story. The scheduler picks one eligible asset per account per
    cycle in ``_maybe_generate_story`` and creates a Video row of
    ``content_type='story'``.
    """

    __tablename__ = "story_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(16), nullable=False)  # "photo" | "video"
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    caption_fallback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Per-account hard cap. NULL = fall back to the global
    # carousel_set_cooldown_days cooldown (reused for stories).
    # 1 = one-shot (never reuse on the same account).
    max_uses_per_account: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


class StoryAssetUsage(Base):
    """Per-account usage row written when a StoryAsset is picked.

    Populated inside ``_maybe_generate_story`` at the same time as the
    Video row; ``dispatched_at`` and ``posted_at`` are filled later as
    the pipeline progresses.
    """

    __tablename__ = "story_asset_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("story_assets.id", ondelete="CASCADE"), nullable=False,
    )
    video_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("videos.id", ondelete="CASCADE"), nullable=False,
    )
    account_username: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    used_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class CaptionSeed(Base):
    __tablename__ = "caption_seeds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    seed_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(),
    )


# ── Pinterest Module ────────────────────────────────────────────


class PinterestAccount(Base):
    __tablename__ = "pinterest_accounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'login_required', 'needs_attention', 'blocked', 'disabled')",
            name="ck_pinterest_accounts_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    profile_link_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    profile_link_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_bootstrap_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    app_installed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    gallery_permission_granted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_permission_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    observed_account_label: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    observed_ui_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    needs_attention_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestSchedulerSettings(Base):
    __tablename__ = "pinterest_scheduler_settings"
    __table_args__ = (
        CheckConstraint("target_pins_per_day >= 0", name="ck_pinterest_scheduler_target_nonnegative"),
        CheckConstraint("min_gap_minutes >= 0", name="ck_pinterest_scheduler_gap_nonnegative"),
        CheckConstraint("jitter_minutes >= 0", name="ck_pinterest_scheduler_jitter_nonnegative"),
        CheckConstraint("reelsomet_guard_minutes >= 0", name="ck_pinterest_scheduler_guard_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pinterest_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    target_pins_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    min_pins_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_pins_per_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    posting_windows_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    min_gap_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=40)
    jitter_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_delay_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    pause_after_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    device_conflict_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="wait")
    reelsomet_guard_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    safe_mode_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestImport(Base):
    __tablename__ = "pinterest_imports"
    __table_args__ = (
        CheckConstraint("platform = 'pinterest'", name="ck_pinterest_imports_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="pinterest")
    source_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    source_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_pins_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_assets_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_boards_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestAsset(Base):
    __tablename__ = "pinterest_assets"
    __table_args__ = (
        Index("ix_pinterest_assets_media_hash", "media_hash"),
        Index("ix_pinterest_assets_import_id", "import_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_imports.id", ondelete="SET NULL"), nullable=True,
    )
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    original_file: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    phone_storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_staged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestBoard(Base):
    __tablename__ = "pinterest_boards"
    __table_args__ = (
        UniqueConstraint("account_id", "key", name="ux_pinterest_boards_account_key"),
        UniqueConstraint("account_id", "name", name="ux_pinterest_boards_account_name"),
        CheckConstraint("visibility IN ('public', 'secret')", name="ck_pinterest_boards_visibility"),
        CheckConstraint(
            "status IN ('imported', 'needs_create', 'creating', 'active', 'failed', 'needs_attention', 'archived')",
            name="ck_pinterest_boards_status",
        ),
        Index("ix_pinterest_boards_account_status", "account_id", "status"),
        Index("ix_pinterest_boards_source_import", "source_import_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pinterest_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    source_import_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_imports.id", ondelete="SET NULL"), nullable=True,
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="public")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="imported")
    pinterest_board_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pinterest_board_external_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_create_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestPin(Base):
    __tablename__ = "pinterest_pins"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="ux_pinterest_pins_account_external"),
        CheckConstraint(
            "status IN ('ready', 'posting', 'posted', 'failed', 'retry_waiting', 'needs_attention', 'cancelled')",
            name="ck_pinterest_pins_status",
        ),
        Index("ix_pinterest_pins_pick", "account_id", "status", "priority", "order_index", "created_at"),
        Index("ix_pinterest_pins_board_status", "board_id", "status"),
        Index("ix_pinterest_pins_asset", "asset_id"),
        Index("ix_pinterest_pins_source_import", "source_import_id"),
        Index("ix_pinterest_pins_next_retry", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pinterest_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    board_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pinterest_boards.id", ondelete="CASCADE"), nullable=False,
    )
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("pinterest_assets.id", ondelete="CASCADE"), nullable=False,
    )
    source_import_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_imports.id", ondelete="SET NULL"), nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posting_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    pinterest_pin_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_attempt_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestPostAttempt(Base):
    __tablename__ = "pinterest_post_attempts"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('pinterest.health_check', 'pinterest.bootstrap_permissions', "
            "'pinterest.ensure_board', 'pinterest.publish_pin', 'pinterest.verify_pin')",
            name="ck_pinterest_attempts_task_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'needs_attention', 'cancelled', 'timeout')",
            name="ck_pinterest_attempts_status",
        ),
        Index("ix_pinterest_attempts_account_started", "account_id", "started_at"),
        Index("ix_pinterest_attempts_pin_started", "pin_id", "started_at"),
        Index("ix_pinterest_attempts_board_started", "board_id", "started_at"),
        Index("ix_pinterest_attempts_device_started", "device_id", "started_at"),
        Index("ix_pinterest_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True,
    )
    board_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_boards.id", ondelete="SET NULL"), nullable=True,
    )
    pin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_pins.id", ondelete="SET NULL"), nullable=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latest_state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    latest_action: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    next_action: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    screenshot_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    raw_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class PinterestTaskEvent(Base):
    __tablename__ = "pinterest_task_events"
    __table_args__ = (
        Index("ix_pinterest_events_trace_ts", "trace_id", "ts_ms"),
        Index("ix_pinterest_events_task_ts", "task_id", "ts_ms"),
        Index("ix_pinterest_events_device_ts", "device_id", "ts_ms"),
        Index("ix_pinterest_events_pin_ts", "pin_id", "ts_ms"),
        Index("ix_pinterest_events_board_ts", "board_id", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    attempt_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_post_attempts.id", ondelete="CASCADE"), nullable=True,
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_accounts.id", ondelete="SET NULL"), nullable=True,
    )
    board_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_boards.id", ondelete="SET NULL"), nullable=True,
    )
    pin_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("pinterest_pins.id", ondelete="SET NULL"), nullable=True,
    )
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    fsm_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    state_entered_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action_target: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    action_started_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_finished_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    next_action_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    next_action_target: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    next_action_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screen_activity: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    screen_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    screenshot_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


# ── Reddit Module ───────────────────────────────────────────────


class RedditAccount(Base):
    __tablename__ = "reddit_accounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'needs_attention', 'login_required', 'rate_limited', 'disabled')",
            name="ck_reddit_accounts_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    posting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    commenting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    app_installed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    logged_in: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    observed_account_label: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    observed_ui_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_health_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_comment_scan_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    needs_attention_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditSchedulerSettings(Base):
    __tablename__ = "reddit_scheduler_settings"
    __table_args__ = (
        CheckConstraint("target_posts_per_day >= 0", name="ck_reddit_scheduler_target_nonnegative"),
        CheckConstraint("min_post_gap_minutes >= 0", name="ck_reddit_scheduler_gap_nonnegative"),
        CheckConstraint("comment_scan_interval_minutes >= 0", name="ck_reddit_scheduler_scan_nonnegative"),
        CheckConstraint("max_auto_replies_per_hour >= 0", name="ck_reddit_scheduler_reply_hour_nonnegative"),
        CheckConstraint("max_auto_replies_per_day >= 0", name="ck_reddit_scheduler_reply_day_nonnegative"),
        CheckConstraint("thread_reply_cooldown_minutes >= 0", name="ck_reddit_scheduler_thread_nonnegative"),
        CheckConstraint("device_guard_minutes >= 0", name="ck_reddit_scheduler_guard_nonnegative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True,
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    posting_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    target_posts_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    min_post_gap_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    posting_window_start: Mapped[str] = mapped_column(String(8), nullable=False, default="10:00")
    posting_window_end: Mapped[str] = mapped_column(String(8), nullable=False, default="23:30")
    scan_comments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    comment_scan_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    auto_reply_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_auto_replies_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    max_auto_replies_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    thread_reply_cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    device_guard_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    safe_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditSubreddit(Base):
    __tablename__ = "reddit_subreddits"
    __table_args__ = (
        UniqueConstraint("account_id", "name", name="ux_reddit_subreddits_account_name"),
        CheckConstraint("mode IN ('owned', 'approved', 'manual_review', 'disabled')", name="ck_reddit_subreddits_mode"),
        CheckConstraint(
            "status IN ('active', 'paused', 'needs_attention', 'posting_disabled', 'disabled')",
            name="ck_reddit_subreddits_status",
        ),
        Index("ix_reddit_subreddits_account_status", "account_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="owned")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    posting_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    commenting_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_flair: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nsfw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rule_profile_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditImport(Base):
    __tablename__ = "reddit_imports"
    __table_args__ = (
        CheckConstraint("platform = 'reddit'", name="ck_reddit_imports_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="reddit")
    source_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    source_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="imported")
    created_posts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_assets_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_rows_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    validation_errors_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditAsset(Base):
    __tablename__ = "reddit_assets"
    __table_args__ = (
        Index("ix_reddit_assets_media_hash", "media_hash"),
        Index("ix_reddit_assets_import_id", "import_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_imports.id", ondelete="SET NULL"), nullable=True,
    )
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    original_file: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    phone_storage_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_staged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    width: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ghosted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ghost_metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditPost(Base):
    __tablename__ = "reddit_posts"
    __table_args__ = (
        UniqueConstraint("account_id", "external_id", name="ux_reddit_posts_account_external"),
        CheckConstraint(
            "status IN ('ready', 'staged', 'posting', 'posted', 'retry_waiting', "
            "'failed', 'needs_attention', 'cancelled')",
            name="ck_reddit_posts_status",
        ),
        Index("ix_reddit_posts_pick", "account_id", "status", "priority", "order_index", "created_at"),
        Index("ix_reddit_posts_subreddit_status", "subreddit_id", "status"),
        Index("ix_reddit_posts_asset", "asset_id"),
        Index("ix_reddit_posts_source_import", "source_import_id"),
        Index("ix_reddit_posts_next_attempt", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    subreddit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_subreddits.id", ondelete="CASCADE"), nullable=False,
    )
    asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_assets.id", ondelete="CASCADE"), nullable=False,
    )
    source_import_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_imports.id", ondelete="SET NULL"), nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    flair: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    nsfw: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    scheduled_after: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posting_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reddit_post_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    permalink: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_attempt_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditComment(Base):
    __tablename__ = "reddit_comments"
    __table_args__ = (
        UniqueConstraint("post_id", "body_hash", "author", name="ux_reddit_comments_post_hash_author"),
        CheckConstraint(
            "status IN ('new', 'ignored', 'needs_reply', 'drafted', 'replying', "
            "'replied', 'failed', 'needs_attention')",
            name="ck_reddit_comments_status",
        ),
        Index("ix_reddit_comments_post_status", "post_id", "status"),
        Index("ix_reddit_comments_account_status", "account_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    subreddit_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_subreddits.id", ondelete="CASCADE"), nullable=False,
    )
    post_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_posts.id", ondelete="CASCADE"), nullable=False,
    )
    reddit_comment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    reddit_parent_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    permalink: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    commented_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    classification: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditGrokCall(Base):
    __tablename__ = "reddit_grok_calls"
    __table_args__ = (
        CheckConstraint("status IN ('success', 'failed', 'timeout')", name="ck_reddit_grok_calls_status"),
        Index("ix_reddit_grok_calls_comment", "comment_id"),
        Index("ix_reddit_grok_calls_account_created", "account_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    comment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_comments.id", ondelete="SET NULL"), nullable=True,
    )
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    output_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    provider_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class RedditReplyDraft(Base):
    __tablename__ = "reddit_reply_drafts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('drafted', 'needs_review', 'approved', 'auto_approved', "
            "'replying', 'posted', 'rejected', 'failed')",
            name="ck_reddit_reply_drafts_status",
        ),
        Index("ix_reddit_reply_drafts_comment_status", "comment_id", "status"),
        Index("ix_reddit_reply_drafts_account_status", "account_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    comment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_comments.id", ondelete="CASCADE"), nullable=False,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="CASCADE"), nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="drafted")
    reply_text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="grok")
    prompt_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    grok_call_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_grok_calls.id", ondelete="SET NULL"), nullable=True,
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reddit_reply_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    permalink: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditPostAttempt(Base):
    __tablename__ = "reddit_post_attempts"
    __table_args__ = (
        CheckConstraint(
            "task_type IN ('reddit.health_check', 'reddit.publish_post', "
            "'reddit.scan_comments', 'reddit.reply_comment')",
            name="ck_reddit_attempts_task_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'needs_attention', 'cancelled', 'timeout')",
            name="ck_reddit_attempts_status",
        ),
        Index("ix_reddit_attempts_account_started", "account_id", "started_at"),
        Index("ix_reddit_attempts_post_started", "post_id", "started_at"),
        Index("ix_reddit_attempts_comment_started", "comment_id", "started_at"),
        Index("ix_reddit_attempts_device_started", "device_id", "started_at"),
        Index("ix_reddit_attempts_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), nullable=True,
    )
    subreddit_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_subreddits.id", ondelete="SET NULL"), nullable=True,
    )
    post_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_posts.id", ondelete="SET NULL"), nullable=True,
    )
    comment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_comments.id", ondelete="SET NULL"), nullable=True,
    )
    reply_draft_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_reply_drafts.id", ondelete="SET NULL"), nullable=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latest_state: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    latest_action: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    next_action: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    screenshot_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    raw_result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now(),
    )


class RedditTaskEvent(Base):
    __tablename__ = "reddit_task_events"
    __table_args__ = (
        Index("ix_reddit_events_trace_ts", "trace_id", "ts_ms"),
        Index("ix_reddit_events_task_ts", "task_id", "ts_ms"),
        Index("ix_reddit_events_device_ts", "device_id", "ts_ms"),
        Index("ix_reddit_events_post_ts", "post_id", "ts_ms"),
        Index("ix_reddit_events_comment_ts", "comment_id", "ts_ms"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    attempt_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_post_attempts.id", ondelete="CASCADE"), nullable=True,
    )
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True,
    )
    account_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_accounts.id", ondelete="SET NULL"), nullable=True,
    )
    subreddit_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_subreddits.id", ondelete="SET NULL"), nullable=True,
    )
    post_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_posts.id", ondelete="SET NULL"), nullable=True,
    )
    comment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_comments.id", ondelete="SET NULL"), nullable=True,
    )
    reply_draft_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("reddit_reply_drafts.id", ondelete="SET NULL"), nullable=True,
    )
    ts_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    fsm_kind: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    state_entered_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action_target: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    action_started_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_finished_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    action_result: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    next_action_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    next_action_target: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    next_action_at_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    screen_activity: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    screen_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    screenshot_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
