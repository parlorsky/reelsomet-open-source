"""APScheduler-based farm scheduler: video dispatch, result collection, health checks.

Periodic jobs:
- process_pending_videos     — push due videos to phones via WS bridge
- collect_post_results       — poll devices for post outcomes, update DB
- health_check               — mark silent devices as offline
- auto_schedule_videos       — assign scheduled_time to unscheduled pending videos
- auto_start_engagement      — auto-start engagement sessions for enabled accounts
- collect_engagement_results — poll running engagement sessions and sync actions
- auto_start_insights        — auto-trigger insights collection for enabled accounts
- collect_insights_results   — poll devices running insights and sync snapshots
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import math
import random
import secrets
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from typing import TYPE_CHECKING

from server.api.video_download import generate_download_url, generate_carousel_asset_url
from server.config import VPSConfig
from server.device_actions import abort_engagement_on_device
from server import farm_time
from server.insights_watermark import (
    encode_watermark,
    extract_watermark,
    stamp_caption,
    strip_watermark,
)
from server.models import (
    Account,
    AccountDevice,
    CaptionSeed,
    Device,
    DonorUsage,
    EngagementAction,
    EngagementSession,
    EngagementTarget,
    InsightsCollectionPlan,
    GenerationRun,
    InsightsSnapshot,
    InsightsSnapshotDaily,
    PhotoSet,
    PhotoSetImage,
    PhotoSetUsage,
    PinterestAccount,
    PinterestAsset,
    PinterestBoard,
    PinterestPin,
    PinterestPostAttempt,
    PinterestSchedulerSettings,
    PostLog,
    RedditAccount,
    RedditAsset,
    RedditComment,
    RedditGrokCall,
    RedditPost,
    RedditPostAttempt,
    RedditReplyDraft,
    RedditSchedulerSettings,
    RedditSubreddit,
    StoryAsset,
    StoryAssetUsage,
    Video,
)
from server.pinterest.device_calendar import check_device_calendar
from server.pinterest.assets import stage_payload
from server.reddit.assets import build_reddit_asset_url
from server.reddit.device_guard import check_reddit_device_guard
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.bridge import DeviceBridge
from server.ws.manager import DeviceConnectionManager

if TYPE_CHECKING:
    from server.telegram import VPSTelegramBot

logger = logging.getLogger(__name__)


def _parse_posting_times(raw: str | None) -> list[str]:
    """Parse posting_times from DB: handles JSON array or comma-separated."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(t).strip() for t in parsed if str(t).strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [t.strip().strip('"') for t in raw.split(",") if t.strip()]


def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (SQLite-compatible).

    Uses the non-deprecated ``datetime.now(timezone.utc)`` API internally.
    """
    return farm_time.utcnow_naive()


def _parse_hhmm_minutes(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        hour_raw, minute_raw = value.strip().split(":", 1)
        hour = int(hour_raw)
        minute = int(minute_raw)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _pinterest_posting_window_block(
    *,
    posting_windows_json: str | None,
    timezone_name: str | None,
    now: datetime,
) -> str | None:
    raw = (posting_windows_json or "").strip()
    if not raw or raw == "[]":
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        logger.warning("[PINTEREST] Ignoring invalid posting_windows_json: %r", raw[:200])
        return None
    if not isinstance(parsed, list):
        return None

    windows: list[tuple[int, int]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        start = _parse_hhmm_minutes(item.get("start"))
        end = _parse_hhmm_minutes(item.get("end"))
        if start is None or end is None or start == end:
            continue
        windows.append((start, end))
    if not windows:
        return None

    local_now = farm_time.utc_naive_to_local(now, timezone_name)
    current_minute = (
        local_now.hour * 60
        + local_now.minute
        + local_now.second / 60
        + local_now.microsecond / 60_000_000
    )
    for start, end in windows:
        if start < end and start <= current_minute < end:
            return None
        if start > end and (current_minute >= start or current_minute < end):
            return None

    return f"outside posting window ({local_now:%H:%M} {timezone_name or ''})"


def _reddit_posting_window_block(
    *,
    posting_window_start: str | None,
    posting_window_end: str | None,
    timezone_name: str | None,
    now: datetime,
) -> str | None:
    start = _parse_hhmm_minutes(posting_window_start)
    end = _parse_hhmm_minutes(posting_window_end)
    if start is None or end is None or start == end:
        return None

    local_now = farm_time.utc_naive_to_local(now, timezone_name)
    current_minute = (
        local_now.hour * 60
        + local_now.minute
        + local_now.second / 60
        + local_now.microsecond / 60_000_000
    )
    if start < end and start <= current_minute < end:
        return None
    if start > end and (current_minute >= start or current_minute < end):
        return None
    return f"outside posting window ({local_now:%H:%M} {timezone_name or ''})"


# Case-insensitive substring match for the a11y-death signal. Android
# executors use slightly different phrasings (PostingExecutor,
# MessageRouter, VideoTransferServer) — "A11y service not running" vs
# "Accessibility service not running". We match both.
_A11Y_ERROR_NEEDLES = (
    "a11y service not running",
    "accessibility service not running",
)

_PROFILE_FALSE_FAILURE_NEEDLES = (
    "post not visible on profile",
)

_INSIGHTS_VIDEO_STAT_FIELDS: tuple[tuple[str, str], ...] = (
    ("plays", "stats_plays"),
    ("likes", "stats_likes"),
    ("comments", "stats_comments"),
    ("shares", "stats_shares"),
    ("saves", "stats_saves"),
    ("reach", "stats_reach"),
    ("engaged", "stats_engaged"),
    ("profile_visits", "stats_profile_visits"),
    ("follows", "stats_follows"),
)


# --------------------------------------------------------------------------
# Silent-account watchdog cause classification (Phase 1, 2026-04-24)
#
# When the hourly stall watchdog identifies an "active account with no post
# in the last 3 hours and pending backlog", we now classify *why* before
# alerting / autofixing. Most silent windows are working-as-designed (rate
# limit, daily cap, paused, blocked) and should be informational only.
# Real problems (offline phone, stale schedule with no dispatcher claim,
# completely unknown reason) get escalated.
# --------------------------------------------------------------------------

#: A pending video whose ``scheduled_time`` is older than this is treated
#: as "stale" — auto_schedule_videos didn't pick it up and process_pending
#: didn't dispatch it within the dispatch window. The hourly watchdog will
#: clear ``scheduled_time`` so the next 60s auto_schedule tick re-slots it.
SILENT_STALE_THRESHOLD = timedelta(minutes=30)


class SilentCause:
    """Reason an active account didn't post in the last 3 hours.

    Plain string constants (NOT an Enum) so they round-trip cleanly through
    JSON for the WS broadcast and Telegram payloads. Order in
    ``_classify_silent_cause`` is cheapest-and-most-specific first: account
    state beats device state beats rate limit beats queue.
    """

    DEVICE_OFFLINE = "device_offline"
    ACCOUNT_PAUSED = "account_paused"
    ACCOUNT_BLOCKED = "account_blocked"
    DAILY_CAP = "daily_cap"
    RATE_LIMIT_3H = "rate_limit_3h"
    MIN_INTER_POST_GAP = "min_inter_post_gap"
    STALE_SCHEDULE = "stale_schedule"
    EMPTY_QUEUE = "empty_queue"
    UNKNOWN = "unknown"


#: Causes that should escalate to Telegram and the admin WS as alerts. Other
#: causes are logged but not paged because they are working-as-designed.
ALERTABLE_CAUSES = frozenset({
    SilentCause.DEVICE_OFFLINE,
    SilentCause.STALE_SCHEDULE,
    SilentCause.UNKNOWN,
})


def _format_silent_classification(c: dict[str, Any]) -> str:
    """Render a single silent-account classification as a Telegram line.

    Format follows the spec: ``@account: cause (extra hint)``. The hint is
    cause-specific and chosen to make the alert actionable on a glance.
    """
    account = c.get("account", "?")
    cause = c.get("cause", SilentCause.UNKNOWN)
    payload = c.get("payload") or {}
    if cause == SilentCause.DEVICE_OFFLINE:
        device = payload.get("device_name") or payload.get("device_serial") or "device"
        last = payload.get("last_seen_at")
        last_short = last[11:16] if isinstance(last, str) and len(last) >= 16 else last
        return (
            f"{account}: device_offline ({device}"
            + (f" last seen {last_short}" if last_short else "")
            + " → restart phone)"
        )
    if cause == SilentCause.ACCOUNT_PAUSED:
        return f"{account}: account_paused"
    if cause == SilentCause.ACCOUNT_BLOCKED:
        until = payload.get("blocked_until")
        return f"{account}: account_blocked" + (f" (until {until[11:16]})" if isinstance(until, str) and len(until) >= 16 else "")
    if cause == SilentCause.DAILY_CAP:
        return f"{account}: daily_cap ({payload.get('posts_24h')}/{payload.get('limit')})"
    if cause == SilentCause.RATE_LIMIT_3H:
        return f"{account}: rate_limit_3h ({payload.get('posts_3h')}/{payload.get('limit')})"
    if cause == SilentCause.MIN_INTER_POST_GAP:
        wait = payload.get("wait_seconds", 0)
        return f"{account}: min_inter_post_gap (wait {wait}s)"
    if cause == SilentCause.STALE_SCHEDULE:
        applied = c.get("applied", False)
        suffix = " → re-slotted" if applied else " (skipped — race)"
        vid = payload.get("video_id")
        overdue = payload.get("overdue_minutes")
        return (
            f"{account}: stale_schedule{suffix}"
            f" (v#{vid}, {overdue}m overdue)"
        )
    if cause == SilentCause.EMPTY_QUEUE:
        return f"{account}: empty_queue (no pending videos)"
    return f"{account}: unknown"


def _sha256_text(value: str | None) -> str:
    clean = strip_watermark(value)
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()


def _insights_day_bucket(value: datetime) -> datetime:
    return datetime(value.year, value.month, value.day)


def _delta_or_zero(start: int | None, end: int | None) -> int:
    if start is None or end is None:
        return 0
    return max(0, end - start)


def _datetime_rank(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return (
        (value.toordinal() * 86400.0)
        + (value.hour * 3600.0)
        + (value.minute * 60.0)
        + value.second
        + (value.microsecond / 1_000_000.0)
    )


async def _llm_call_with_retry(llm_fn, *args, **kwargs):
    """One-shot retry on transient LLM errors (429/502/503/504/timeout/conn).

    Non-transient errors (auth/400/etc.) raise immediately so the caller can
    catch them and fall back. Transient HTTP status codes and network errors
    get a single 1-second backoff and are retried once. If the retry also
    fails the exception is re-raised unchanged for the caller's fallback.
    """
    import httpx
    try:
        return await llm_fn(*args, **kwargs)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (429, 502, 503, 504):
            raise
        await asyncio.sleep(1.0)
        return await llm_fn(*args, **kwargs)
    except (httpx.TimeoutException, ConnectionError):
        await asyncio.sleep(1.0)
        return await llm_fn(*args, **kwargs)


class FarmScheduler:
    """Drives the posting pipeline via periodic APScheduler jobs.

    Parameters
    ----------
    session_factory:
        Async SQLAlchemy session factory (``async_sessionmaker``).
    ws_manager:
        Device connection manager — used for online checks.
    bridge:
        High-level WS bridge to send commands to devices.
    config:
        VPS configuration with farm timing settings.
    broadcaster:
        Admin WebSocket broadcaster for pushing events to browsers.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: DeviceConnectionManager,
        bridge: DeviceBridge,
        config: VPSConfig,
        broadcaster: AdminBroadcaster,
        telegram_bot: VPSTelegramBot | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.ws_manager = ws_manager
        self.bridge = bridge
        self.config = config
        self.broadcaster = broadcaster
        self.telegram_bot = telegram_bot
        self.scheduler = AsyncIOScheduler()
        self._running = False
        self._reddit_dispatch_lock = asyncio.Lock()

        # Track last poll timestamp per device (epoch ms) for result collection
        self._last_poll_ms: dict[int, int] = {}

        # Track last poll timestamp per device for engagement/insights collection
        self._last_engagement_poll_ms: dict[int, int] = {}
        self._last_insights_poll_ms: dict[int, int] = {}

        # GC protection for background tasks (prevent premature collection)
        self._background_tasks: set[asyncio.Task[None]] = set()

        # Notification rate limiter: {account_username: last_notify_epoch}
        self._last_notify_ts: dict[str, float] = {}
        self._notify_cooldown = 300.0  # 5 min between notifications per account

        # Hourly stall watchdog state:
        # - _started_at: suppresses silent-device/account alerts for the
        #   first 30 min after boot (otherwise every restart produces
        #   spurious "no posts in last 3h" alerts).
        # - _watchdog_last_alert_at: per-stall-type last Telegram alert
        #   epoch; re-nag allowed only every `_watchdog_alert_cooldown_s`
        #   or when the victim fingerprint changes.
        # - _watchdog_last_fingerprint: stable hash of affected ids per
        #   stall type, so a persistent stall doesn't spam every hour but
        #   a newly-stuck video still fires an alert.
        self._started_at: datetime = _utcnow()
        self._watchdog_last_alert_at: dict[str, float] = {}
        self._watchdog_last_fingerprint: dict[str, str] = {}
        self._watchdog_alert_cooldown_s: float = 6 * 3600.0  # 6h re-nag

        # Per-device consecutive a11y-failure counter. Incremented on
        # every post that returns "A11y service not running" (Android
        # executor's explicit signal), reset on any good outcome or
        # non-a11y failure. `_a11y_alerted` dedupes the Telegram page —
        # one alert per quarantine event, cleared when the device
        # auto-recovers via heartbeat (see health_check).
        self._a11y_fail_counts: dict[int, int] = {}
        self._a11y_alerted: set[int] = set()

        # Auto-restart watchdog state (2026-05-05). When a device
        # produces back-to-back bare "Timeout" failures (the FSM never
        # got past WAITING_FOR_INSTAGRAM, Honor MagicOS holding IG in
        # background), the only reliable recovery is to kill the phone
        # process and let Android's sticky foreground services respawn
        # it from scratch. We send `cmd.app_force_restart` over WS,
        # then stamp the device epoch here for a 30-min cooldown so a
        # phone that crashes-and-restarts without recovering doesn't
        # get force-killed in a tight loop.
        self._last_force_restart_at: dict[int, float] = {}

        # Per-account action_blocked alert dedup (2026-05-05). Separate
        # from `_last_notify_ts` which paces post-failure alerts. When
        # the phone reconnects after a WS gap (e.g. VPS restart) it
        # re-pushes its full PostLog buffer in one batch, generating a
        # storm of identical "ACTION BLOCKED" Telegram alerts at
        # identical receive timestamps. 1h cooldown per account makes
        # the operator see the first one and not 27.
        self._last_action_block_alert_at: dict[str, float] = {}

        # Phase 4 (2026-04-24) — active healthcheck probe state.
        # `_rtt_history` keeps the last 5 RTT samples (or `None` for
        # timeouts) per device so 3 consecutive timeouts can force a
        # close. `_device_health` is the suspect/online/offline-forced
        # FSM. `_last_dispatch_ms` skips probes for devices that have
        # been actively talking to us recently (fresh dispatch implies
        # liveness). All three are kept in-memory; they are diagnostic
        # only and reset on restart, which is fine because the
        # watchdog grace window already absorbs the cold-start case.
        self._rtt_history: dict[int, deque[float | None]] = defaultdict(
            lambda: deque(maxlen=5),
        )
        self._device_health: dict[int, str] = {}
        self._last_dispatch_ms: dict[int, int] = {}
        self._suspect_alerted: set[int] = set()
        self._a11y_dead_alerted: set[int] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Register all periodic jobs and start the scheduler."""
        cfg = self.config

        self.scheduler.add_job(
            self.process_pending_videos,
            "interval",
            seconds=cfg.farm_poll_interval_seconds,
            id="process_pending_videos",
            name="Push due videos to phones",
        )
        self.scheduler.add_job(
            self.process_pinterest_pins,
            "interval",
            seconds=cfg.farm_poll_interval_seconds,
            id="process_pinterest_pins",
            name="Process due Pinterest pins",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.process_reddit_comment_scans,
            "interval",
            seconds=cfg.farm_poll_interval_seconds,
            id="process_reddit_comment_scans",
            name="Scan Reddit comments",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.process_reddit_replies,
            "interval",
            seconds=cfg.farm_poll_interval_seconds,
            id="process_reddit_replies",
            name="Process due Reddit replies",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.process_reddit_posts,
            "interval",
            seconds=cfg.farm_poll_interval_seconds,
            id="process_reddit_posts",
            name="Process due Reddit posts",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.generate_reddit_reply_drafts,
            "interval",
            seconds=60,
            id="generate_reddit_reply_drafts",
            name="Generate Reddit reply drafts via Grok",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.collect_post_results,
            "interval",
            seconds=cfg.farm_result_poll_interval_seconds,
            id="collect_post_results",
            name="Collect post results from devices",
        )
        self.scheduler.add_job(
            self.health_check,
            "interval",
            seconds=cfg.farm_health_check_interval_seconds,
            id="health_check",
            name="Device health check",
        )
        # Active healthcheck probe (Phase 4, 2026-04-24). Round-trips a
        # token to every silent online device every 60 s. Catches the
        # "OkHttp reader thread alive but app handler wedged" failure
        # mode that the passive heartbeat can't surface, because the
        # phone's auto-pong is enough to keep the heartbeat happy
        # without proving the app is responsive.
        self.scheduler.add_job(
            self.probe_silent_devices,
            "interval",
            seconds=60,
            id="probe_silent_devices",
            name="Active healthcheck probe for silent devices",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.auto_schedule_videos,
            "interval",
            seconds=cfg.farm_health_check_interval_seconds,
            id="auto_schedule_videos",
            name="Auto-schedule unscheduled videos",
        )

        self.scheduler.add_job(
            self.recover_stale_videos,
            "interval",
            seconds=300,
            id="recover_stale",
            name="Recover stale uploading/scheduled videos",
        )

        # Hourly stall watchdog — catches "something lagged at 15:00 and
        # nothing posted the rest of the day" pattern. Reports stuck
        # uploading/scheduled, clears past-due pending.scheduled_time so
        # auto_schedule re-slots, warns about silent accounts/devices.
        # max_instances=1 + coalesce=True prevents overlap; misfire_grace
        # 5 min so a slow tick doesn't silence the job permanently.
        self.scheduler.add_job(
            self.check_posting_stalls,
            "interval",
            hours=1,
            id="check_posting_stalls",
            name="Posting stall watchdog (hourly)",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        # Phone auto-restart watchdog DISABLED 2026-05-05.
        #
        # Empirical finding (Honor logcat 2026-05-05 12:42–14:24): each
        # ``cmd.app_force_restart`` triggers a 3-process death cascade
        # within 13 s — the kill, then Android's sticky-service restart
        # spawns a new process that dies again ~5 s later, then a third
        # process spawns and dies ~8 s after that. Honor MagicOS appears
        # to refuse to keep our app alive when it tries to come up "in
        # the background" with no visible UI right after a kill. After
        # each cascade ``AssistService.onServiceConnected`` fires 8x in
        # a row (a11y rebind storm) and each fire triggers
        # ``Recovery: scheduled next pending video`` — i.e. 8 duplicate
        # dispatch attempts queued. Those produce more bare-Timeout
        # PostLog rows, which trip the watchdog again on the next
        # cooldown expiry → a self-reinforcing 16–20 min restart loop.
        #
        # Disabling the registration is the minimum-blast-radius fix.
        # The watchdog method body (``auto_restart_stalled_devices``)
        # is intentionally left in place for future re-introduction
        # under a different recovery strategy (e.g. send an FSM-reset
        # command instead of a process kill, or only fire after manual
        # operator confirmation).
        # self.scheduler.add_job(
        #     self.auto_restart_stalled_devices,
        #     "interval",
        #     seconds=120,
        #     id="auto_restart_stalled_devices",
        #     name="Auto-restart phones stuck in WAITING_FOR_INSTAGRAM",
        #     max_instances=1,
        #     coalesce=True,
        #     misfire_grace_time=120,
        # )

        # Auto-generate videos for accounts that need more content.
        # max_instances=1 is the APScheduler default but we set it
        # explicitly to document the guarantee: two concurrent runs of
        # this job must never overlap because _maybe_generate_carousel
        # relies on session commit ordering to avoid double-picking the
        # same PhotoSet for two accounts within one generation cycle.
        self.scheduler.add_job(
            self.auto_generate_videos,
            "interval",
            seconds=300,
            id="auto_generate",
            name="Auto-generate vid_bait videos",
            max_instances=1,
            coalesce=True,
        )
        # Story posting is disabled until the Android client has a real
        # story-posting executor. The current phone app ignores contentType
        # and routes story PNG rows through the Reel posting state machine.

        # Engagement jobs
        self.scheduler.add_job(
            self.auto_start_engagement,
            "interval",
            seconds=300,
            id="auto_engagement",
            name="Auto-start engagement sessions",
        )
        self.scheduler.add_job(
            self.collect_engagement_results,
            "interval",
            seconds=30,
            id="collect_engagement",
            name="Collect engagement results from devices",
        )

        # Insights jobs
        self.scheduler.add_job(
            self.auto_start_insights,
            "interval",
            seconds=600,
            id="auto_insights",
            name="Auto-start insights collection",
        )
        self.scheduler.add_job(
            self.collect_insights_results,
            "interval",
            seconds=30,
            id="collect_insights",
            name="Collect insights results from devices",
        )
        self.scheduler.add_job(
            self.refresh_insights_plan_priorities,
            "interval",
            seconds=600,
            id="refresh_insights_plan_priorities",
            name="Refresh insights plan priorities",
        )

        # Advanced logging step 4 (Codex roadmap): nightly farm_logs retention prune.
        # DEBUG entries deleted after 7d, INFO after 30d, WARN/ERROR kept forever.
        self.scheduler.add_job(
            self.prune_farm_logs,
            "interval",
            hours=24,
            id="prune_farm_logs",
            name="Prune farm_logs (DEBUG 7d / INFO 30d retention)",
        )
        self.scheduler.add_job(
            self.prune_donor_usage,
            "interval",
            hours=24,
            id="prune_donor_usage",
            name="Prune donor_usage (72h retention)",
        )
        self.scheduler.add_job(
            self.retire_ghost_reels,
            "interval",
            hours=1,
            id="retire_ghost_reels",
            name="Retire ghost reels from insights plan",
        )
        self.scheduler.add_job(
            self.prune_insights,
            "interval",
            hours=24,
            id="prune_insights",
            name="Prune and downsample insights snapshots",
        )

        self.scheduler.start()
        self._running = True
        logger.info("FarmScheduler started (poll=%ds, results=%ds, health=%ds)",
                     cfg.farm_poll_interval_seconds,
                     cfg.farm_result_poll_interval_seconds,
                     cfg.farm_health_check_interval_seconds)

    @property
    def running(self) -> bool:
        """Whether the scheduler is currently running."""
        return self._running

    async def stop(self) -> None:
        """Shut down the scheduler gracefully."""
        if self._running:
            self.scheduler.shutdown(wait=False)
            self._running = False
        if self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
        logger.info("FarmScheduler stopped")

    def _pick_decay_bracket(self, age_hours: float) -> float:
        """Return the configured target interval for a reel age."""
        for max_age_hours, interval_hours in self.config.insights_decay_brackets:
            if age_hours <= float(max_age_hours):
                return float(interval_hours)
        return float(self.config.insights_decay_fallback_hours)

    @staticmethod
    def _video_age_hours(video: Video, now: datetime) -> float:
        anchor = video.posted_at or video.created_at or now
        return max(0.0, (now - anchor).total_seconds() / 3600.0)

    @staticmethod
    def _metric_band_score(
        video: Video, snapshot: InsightsSnapshot,
    ) -> tuple[int, float] | None:
        compared = 0
        within = 0
        total_error = 0.0
        for snap_attr, video_attr in _INSIGHTS_VIDEO_STAT_FIELDS:
            snap_value = getattr(snapshot, snap_attr)
            video_value = getattr(video, video_attr)
            if snap_value is None or video_value is None:
                continue
            compared += 1
            error = abs(float(snap_value) - float(video_value)) / max(float(video_value), 1.0)
            if error <= 0.10:
                within += 1
                total_error += error
        if compared == 0 or within == 0:
            return None
        if compared >= 2 and within != compared:
            return None
        if compared == 1 and snapshot.plays is None:
            return None
        return within, total_error

    @staticmethod
    def _snapshot_has_metric_delta(video: Video, snapshot: InsightsSnapshot) -> bool:
        for snap_attr, video_attr in _INSIGHTS_VIDEO_STAT_FIELDS:
            snap_value = getattr(snapshot, snap_attr)
            if snap_value is None:
                continue
            if getattr(video, video_attr) != snap_value:
                return True
        return False

    @staticmethod
    def _apply_snapshot_metrics(video: Video, snapshot: InsightsSnapshot) -> bool:
        changed = False
        for snap_attr, video_attr in _INSIGHTS_VIDEO_STAT_FIELDS:
            snap_value = getattr(snapshot, snap_attr)
            if snap_value is None:
                continue
            if getattr(video, video_attr) != snap_value:
                changed = True
            setattr(video, video_attr, snap_value)
        if changed:
            video.stats_updated_at = snapshot.collected_at or _utcnow()
        return changed

    def _stamp_video_caption(self, video: Video) -> str:
        stamped_caption = stamp_caption(video.caption, video.id)
        video.caption = stamped_caption
        video.insights_caption_hash = _sha256_text(stamped_caption)
        video.insights_video_marker = encode_watermark(video.id)
        return stamped_caption

    async def _upsert_insights_plan(
        self,
        session: AsyncSession,
        video: Video,
        *,
        anchor_at: datetime,
        next_due_at: datetime | None = None,
        priority_score: float | None = None,
        reset_failures: bool = False,
    ) -> InsightsCollectionPlan:
        plan = await session.get(InsightsCollectionPlan, video.id)
        if plan is None:
            plan = InsightsCollectionPlan(
                video_id=video.id,
                account_username=video.account_username,
                next_due_at=anchor_at,
            )
            session.add(plan)

        age_hours = self._video_age_hours(video, anchor_at)
        interval_hours = self._pick_decay_bracket(age_hours)
        plan.account_username = video.account_username
        plan.target_interval_hours = interval_hours
        plan.next_due_at = next_due_at or (anchor_at + timedelta(hours=interval_hours))
        if priority_score is not None:
            plan.priority_score = priority_score
        if reset_failures:
            plan.consecutive_failures = 0
        plan.last_staleness_ms = 0
        return plan

    async def _device_has_imminent_posting(
        self, session: AsyncSession, device_id: int, now: datetime,
    ) -> bool:
        active_post = (await session.execute(
            select(Video.id)
            .join(
                AccountDevice,
                AccountDevice.account_username == Video.account_username,
            )
            .where(
                AccountDevice.device_id == device_id,
                AccountDevice.is_primary.is_(True),
                Video.status.in_(["uploading", "scheduled"]),
            )
            .limit(1)
        )).scalar_one_or_none()
        if active_post is not None:
            return True

        grace_cutoff = now + timedelta(
            minutes=self.config.insights_posting_grace_minutes,
        )
        due_post = (await session.execute(
            select(Video.id)
            .join(
                AccountDevice,
                AccountDevice.account_username == Video.account_username,
            )
            .where(
                AccountDevice.device_id == device_id,
                AccountDevice.is_primary.is_(True),
                Video.status == "pending",
                Video.scheduled_time.is_not(None),
                Video.scheduled_time <= grace_cutoff,
            )
            .limit(1)
        )).scalar_one_or_none()
        return due_post is not None

    async def _match_video(
        self,
        session: AsyncSession,
        snapshot: InsightsSnapshot,
        *,
        published_label: str | None = None,
    ) -> tuple[Video | None, str, float]:
        marker_id = extract_watermark(snapshot.caption_snippet)
        marker = encode_watermark(marker_id) if marker_id is not None else None
        caption_hash = _sha256_text(snapshot.caption_snippet) if snapshot.caption_snippet is not None else None

        if marker is not None:
            marker_video = (await session.execute(
                select(Video)
                .where(
                    or_(
                        Video.insights_video_marker == marker,
                        Video.id == marker_id,
                    ),
                    Video.status == "posted",
                )
                .limit(1)
            )).scalar_one_or_none()
            if marker_video is not None:
                return marker_video, "marker", 1.0

        hash_candidates: list[Video] = []
        if caption_hash and snapshot.account_username:
            hash_candidates = list((await session.execute(
                select(Video)
                .where(
                    Video.account_username == snapshot.account_username,
                    Video.status == "posted",
                    Video.insights_caption_hash == caption_hash,
                )
                .order_by(Video.posted_at.desc(), Video.id.desc())
            )).scalars().all())

        if hash_candidates:
            if published_label:
                exact_label = [
                    candidate for candidate in hash_candidates
                    if candidate.insights_published_label == published_label
                ]
                if exact_label:
                    return exact_label[0], "hash_label", 0.85
                unset_label = [
                    candidate for candidate in hash_candidates
                    if not candidate.insights_published_label
                ]
                if len(unset_label) == 1:
                    return unset_label[0], "hash_label", 0.85
            elif len(hash_candidates) == 1:
                return hash_candidates[0], "hash_label", 0.85

        # Caption-prefix strategy (added after 2026-04-25 e2e test):
        # IG truncates the caption on the reels-player insights bottom-
        # sheet with a trailing "…", chopping off the ZWSP marker at
        # the end. The scraped caption_snippet therefore looks like
        # `"Got my lips done. Not for coffee. Not for …"` and neither
        # marker nor full-caption hash match. But the VISIBLE prefix
        # is unique enough per-account that we can pick the right
        # Video by a prefix match on stripped(Video.caption).
        #
        # We take the first 24 visible chars of the scraped snippet
        # (strip trailing `…` / "..." noise) and compare with
        # stripped(Video.caption). Requires ≥12 chars of signal to
        # avoid false positives on emoji-only or "Watch all" captions.
        if snapshot.account_username and snapshot.caption_snippet:
            visible_scraped = strip_watermark(snapshot.caption_snippet)
            visible_scraped = visible_scraped.rstrip(
                "…. \t\n "  # ellipsis, dots, whitespace, nbsp
            ).strip()
            if len(visible_scraped) >= 12:
                prefix = visible_scraped[:24]
                prefix_candidates = list((await session.execute(
                    select(Video)
                    .where(
                        Video.account_username == snapshot.account_username,
                        Video.status == "posted",
                        Video.caption.is_not(None),
                    )
                    .order_by(Video.posted_at.desc(), Video.id.desc())
                )).scalars().all())
                prefix_hits = [
                    v for v in prefix_candidates
                    if strip_watermark(v.caption or "").startswith(prefix)
                ]
                if len(prefix_hits) == 1:
                    return prefix_hits[0], "caption_prefix", 0.75
                if len(prefix_hits) > 1:
                    # Caption-reuse is common in our farm (captions come
                    # from a seed pool), so the same "Yoga taught me…"
                    # prefix can match 4-9 different Videos posted weeks
                    # apart. Tie-breaker: pick the MOST RECENT by
                    # posted_at. The phone scrapes reels in profile-
                    # grid order which IG sorts newest-first, so the
                    # top hit by posted_at DESC is the reel currently
                    # on screen. Confidence drops because this is a
                    # heuristic, not a unique match.
                    prefix_candidates_sorted = sorted(
                        prefix_hits,
                        key=lambda v: (
                            -_datetime_rank(v.posted_at),
                            -v.id,
                        ),
                    )
                    logger.info(
                        "[INSIGHTS] caption_prefix @%s prefix=%r -> %d "
                        "candidates, picking newest id=%d posted_at=%s",
                        snapshot.account_username, prefix,
                        len(prefix_hits), prefix_candidates_sorted[0].id,
                        prefix_candidates_sorted[0].posted_at,
                    )
                    return prefix_candidates_sorted[0], "caption_prefix", 0.6

        # Position strategy: demoted below caption_prefix (2026-04-25)
        # because `insights_last_position` gets stamped on every
        # successful match, and two consecutive snapshots taken one
        # swipe apart (position delta = 1) end up in the ±2 window
        # of each other's last known position — so every subsequent
        # snapshot gets aliased to the first one's Video. Caption
        # prefix is a much stronger signal; only fall back to
        # position when the caption itself didn't match anything.
        if snapshot.account_username and snapshot.reel_position is not None:
            position_candidates = list((await session.execute(
                select(Video)
                .where(
                    Video.account_username == snapshot.account_username,
                    Video.status == "posted",
                    Video.insights_last_position.is_not(None),
                    func.abs(Video.insights_last_position - snapshot.reel_position) <= 2,
                )
            )).scalars().all())
            if position_candidates:
                position_candidates.sort(
                    key=lambda candidate: (
                        abs((candidate.insights_last_position or 0) - snapshot.reel_position),
                        -_datetime_rank(candidate.insights_last_collected_at),
                        -_datetime_rank(candidate.posted_at),
                    ),
                )
                return position_candidates[0], "position", 0.6

        if snapshot.account_username:
            metric_candidates = list((await session.execute(
                select(Video)
                .where(
                    Video.account_username == snapshot.account_username,
                    Video.status == "posted",
                )
                .order_by(Video.posted_at.desc(), Video.id.desc())
            )).scalars().all())
            ranked_metric_candidates: list[tuple[int, float, Video]] = []
            for candidate in metric_candidates:
                score = self._metric_band_score(candidate, snapshot)
                if score is None:
                    continue
                matched_fields, total_error = score
                ranked_metric_candidates.append((matched_fields, total_error, candidate))
            if ranked_metric_candidates:
                ranked_metric_candidates.sort(
                    key=lambda item: (
                        -item[0],
                        item[1],
                        -_datetime_rank(item[2].posted_at),
                    ),
                )
                return ranked_metric_candidates[0][2], "metric_band", 0.4

        return None, "unmatched", 0.0

    # ------------------------------------------------------------------
    # Job: recover_stale_videos (every 5 min)
    # ------------------------------------------------------------------

    async def recover_stale_videos(self) -> None:
        """Reset videos stuck in 'uploading' or 'scheduled' status for too long.

        Double-post guard (added 2026-04-24 after watchdog review): only
        reset a stale row if there is NO evidence the post already
        completed on the phone. Specifically we require BOTH:

        1. ``Video.posted_at IS NULL`` — phone's post_result would have
           stamped this on success (scheduler.py: _handle_post_result).
        2. No ``PostLog`` row exists for this video with
           ``result='success'`` — covers the case where ``posted_at``
           was cleared by a prior reset but the phone batch-delivered a
           success log afterwards.

        Without this guard, a phone that posted successfully but lost
        WS connectivity for > ``farm_upload_window_minutes * 3`` would
        have its video reset to ``pending`` and then re-dispatched,
        resulting in a duplicate post on the same account.
        """
        cutoff = _utcnow() - timedelta(
            minutes=self.config.farm_upload_window_minutes * 3,
        )
        # Subquery: video_ids that have a successful PostLog — these
        # already posted and MUST NOT be reset.
        #
        # Guard against the SQLite rowid reuse bug observed 2026-04-26 on
        # video.id=546: an old success log whose recycled `video_id` now
        # points at a newer Video row must only count if it matches the
        # same account and happened after that Video was created.
        success_log_subq = (
            select(PostLog.video_id)
            .join(Video, Video.id == PostLog.video_id)
            .where(
                PostLog.video_id.is_not(None),
                PostLog.result == "success",
                PostLog.account_username == Video.account_username,
                PostLog.timestamp >= Video.created_at,
            )
            .distinct()
            .subquery()
        )
        async with self.session_factory() as session:
            # Reset retryable videos back to pending (with no-double-post guard)
            stale = (await session.execute(
                select(Video).where(
                    Video.status.in_(["uploading", "scheduled"]),
                    Video.updated_at < cutoff,
                    Video.retry_count < self.config.farm_max_auto_retries,
                    Video.posted_at.is_(None),
                    Video.id.not_in(select(success_log_subq.c.video_id)),
                )
            )).scalars().all()
            for video in stale:
                video.status = "pending"
                video.uploaded_to_phone = False
                video.retry_count += 1
                logger.warning(
                    "[POSTING] Recovered stale video %d (%s) — reset to pending (retry %d)",
                    video.id, video.filename, video.retry_count,
                )

            # Mark videos that exceeded max retries as failed (same guard)
            exhausted = (await session.execute(
                select(Video).where(
                    Video.status.in_(["uploading", "scheduled"]),
                    Video.updated_at < cutoff,
                    Video.retry_count >= self.config.farm_max_auto_retries,
                    Video.posted_at.is_(None),
                    Video.id.not_in(select(success_log_subq.c.video_id)),
                )
            )).scalars().all()
            for video in exhausted:
                video.status = "failed"
                video.post_error = "Max retries exceeded (stale recovery)"
                logger.warning(
                    "[POSTING] Stale video %d (%s) exhausted retries — marked failed",
                    video.id, video.filename,
                )

            # Reconcile the escape hatch: if a video is still flagged
            # "uploading"/"scheduled" but posted_at/success-log says it
            # already posted, just mark it posted so UI + stats agree.
            # Avoids the row sitting in a limbo state forever.
            limbo = (await session.execute(
                select(Video).where(
                    Video.status.in_(["uploading", "scheduled"]),
                    or_(
                        Video.posted_at.is_not(None),
                        Video.id.in_(select(success_log_subq.c.video_id)),
                    ),
                )
            )).scalars().all()
            for video in limbo:
                video.status = "posted"
                if video.posted_at is None:
                    video.posted_at = _utcnow()
                if not video.post_result:
                    video.post_result = "success"
                logger.info(
                    "[POSTING] Reconciled limbo video %d (%s) to posted "
                    "(had success log but stale status)",
                    video.id, video.filename,
                )

            await session.commit()

    # ------------------------------------------------------------------
    # Job: check_posting_stalls (every 1 hour) — watchdog
    # ------------------------------------------------------------------

    async def check_posting_stalls(self) -> dict[str, Any]:
        """Hourly stall detector.

        Detects four categories of posting-pipeline stalls:

        1. **Stuck uploading/scheduled** — ``Video.status IN
           ('uploading','scheduled')`` with ``updated_at < now-1h``.
           Purely observational: the existing ``recover_stale_videos``
           job (5-min cadence) does the actual reset with a no-double-
           post guard. We just count and alert.
        2. **Past-due pending** — ``status='pending'`` and
           ``scheduled_time < now-2h``. Side effect: we clear
           ``scheduled_time`` so ``auto_schedule_videos`` re-assigns a
           fresh slot. Safe because pending+past-due means the dispatch
           window was missed entirely; re-schedule is the right move.
        3. **Silent active account** — account is in an active state
           (``is_active AND NOT is_paused AND NOT is_blocked`` and
           either ``blocked_until IS NULL`` or in the past) AND has at
           least one due pending video AND zero successful posts in the
           last 3 hours. Alert-only, no DB writes.
        4. **Silent active device** — device is ``is_active=True`` and
           ``status != 'offline'`` AND has due backlog AND zero
           successful posts in the last 3 hours.

        Grace period: the first 30 minutes after scheduler start,
        silent-account/device detections are suppressed to avoid
        spurious alerts after a restart.

        Returns the detection dict for testability. Side effects:
        broadcasts ``watchdog:stall`` on the admin WS, sends a
        dedup'd Telegram alert with 6h re-nag cooldown.
        """
        now = _utcnow()
        cutoff_1h = now - timedelta(hours=1)
        cutoff_2h = now - timedelta(hours=2)
        cutoff_3h = now - timedelta(hours=3)
        # Grace window: don't fire silent-account/device alerts for the
        # first 30 minutes after boot. A restart naturally creates a
        # "no posts in last 3h" state that clears by itself as soon as
        # scheduling resumes.
        grace_active = (now - self._started_at) < timedelta(minutes=30)

        # Active-account predicate (no `accounts.status` column — composed
        # from the four Boolean flags + blocked_until clock).
        account_active = and_(
            Account.is_active.is_(True),
            Account.is_paused.is_(False),
            or_(
                Account.is_blocked.is_(False),
                and_(
                    Account.blocked_until.is_not(None),
                    Account.blocked_until <= now,
                ),
            ),
        )

        # Subquery: account_usernames with a posted_at in the last 3h.
        # This is the "recent successful post" signal — canonical source
        # is Video.posted_at (stamped in _handle_post_result on success).
        recent_success_accounts_subq = (
            select(Video.account_username)
            .where(Video.posted_at.is_not(None), Video.posted_at >= cutoff_3h)
            .distinct()
            .subquery()
        )
        backlog_accounts_subq = (
            select(Video.account_username)
            .where(
                Video.status == "pending",
                Video.scheduled_time.is_not(None),
                Video.scheduled_time <= now,
            )
            .distinct()
            .subquery()
        )
        recent_success_devices_subq = (
            select(Video.device_id)
            .where(
                Video.device_id.is_not(None),
                Video.posted_at.is_not(None),
                Video.posted_at >= cutoff_3h,
            )
            .distinct()
            .subquery()
        )
        backlog_devices_subq = (
            select(Video.device_id)
            .where(
                Video.device_id.is_not(None),
                Video.status == "pending",
                Video.scheduled_time.is_not(None),
                Video.scheduled_time <= now,
            )
            .distinct()
            .subquery()
        )

        async with self.session_factory() as session:
            # 1) Stuck uploading/scheduled — observation only.
            stuck_rows = (await session.execute(
                select(
                    Video.id, Video.account_username, Video.device_id,
                    Video.status, Video.filename,
                )
                .where(
                    Video.status.in_(["uploading", "scheduled"]),
                    func.coalesce(Video.updated_at, Video.created_at) < cutoff_1h,
                    Video.posted_at.is_(None),
                )
            )).all()
            stuck_processing = [
                {
                    "video_id": r[0],
                    "account": r[1],
                    "device_id": r[2],
                    "status": r[3],
                    "filename": r[4],
                }
                for r in stuck_rows
            ]

            # 2) Past-due pending — clear scheduled_time so auto_schedule
            # re-assigns on the next 60s tick.
            past_due_rows = (await session.execute(
                select(Video.id, Video.account_username, Video.device_id)
                .where(
                    Video.status == "pending",
                    Video.scheduled_time.is_not(None),
                    Video.scheduled_time < cutoff_2h,
                )
            )).all()
            past_due_pending = [
                {"video_id": r[0], "account": r[1], "device_id": r[2]}
                for r in past_due_rows
            ]
            cleared_count = 0
            if past_due_pending:
                cleared_ids = [row["video_id"] for row in past_due_pending]
                from sqlalchemy import update as _sql_update
                res = await session.execute(
                    _sql_update(Video)
                    .where(Video.id.in_(cleared_ids))
                    .values(scheduled_time=None)
                    .execution_options(synchronize_session=False)
                )
                cleared_count = res.rowcount or 0

            # 3) Silent active accounts (suppressed during grace window).
            #
            # We additionally classify each silent account by *cause*
            # (rate-limit / paused / device offline / stale schedule /
            # unknown) so auto-fixable cases (stale_schedule) are
            # repaired in-place, working-as-designed cases are demoted
            # from alert to log, and only real problems escalate to
            # Telegram.
            silent_accounts: list[str] = []
            silent_classifications: list[dict[str, Any]] = []
            autofix_video_ids: list[int] = []
            if not grace_active:
                rows = (await session.execute(
                    select(Account.username)
                    .where(
                        account_active,
                        Account.username.in_(
                            select(backlog_accounts_subq.c.account_username)
                        ),
                        Account.username.not_in(
                            select(recent_success_accounts_subq.c.account_username)
                        ),
                    )
                )).all()
                silent_accounts = [r[0] for r in rows]

            # 4) Silent active devices (suppressed during grace window).
            #
            # Resolved BEFORE the classifier autofix so the
            # classifier's potential UPDATE on Video.scheduled_time
            # doesn't invalidate the backlog signal that drives this
            # query — otherwise a single auto-recoverable account
            # would silently mask a wider device-side outage.
            silent_devices: list[dict[str, Any]] = []
            if not grace_active:
                rows = (await session.execute(
                    select(Device.id, Device.device_id, Device.name)
                    .where(
                        Device.is_active.is_(True),
                        Device.status != "offline",
                        Device.id.in_(select(backlog_devices_subq.c.device_id)),
                        Device.id.not_in(
                            select(recent_success_devices_subq.c.device_id)
                        ),
                    )
                )).all()
                silent_devices = [
                    {"id": r[0], "device_id": r[1], "name": r[2]}
                    for r in rows
                ]

            # 3b) Classify silent accounts and apply auto-fixes. Done
            # AFTER silent_devices so the device-level signal isn't
            # eaten by an account-level autofix.
            if not grace_active and silent_accounts:
                from sqlalchemy import update as _sql_update_2
                for username in silent_accounts:
                    cause, fix_action, payload = await self._classify_silent_cause(
                        session, username, now,
                    )
                    classification = {
                        "account": username,
                        "cause": cause,
                        "fix_action": fix_action,
                        "payload": payload,
                    }
                    if fix_action == "clear_scheduled_time":
                        # The classifier already verified scheduled_time
                        # is more than SILENT_STALE_THRESHOLD in the past.
                        # Clearing it lets auto_schedule_videos re-slot
                        # on the next 60s tick.
                        vid = payload.get("video_id")
                        if vid is not None:
                            res = await session.execute(
                                _sql_update_2(Video)
                                .where(Video.id == vid)
                                .values(scheduled_time=None)
                                .execution_options(synchronize_session=False)
                            )
                            if (res.rowcount or 0) > 0:
                                autofix_video_ids.append(int(vid))
                                classification["applied"] = True
                            else:
                                # Race: video changed status between
                                # classify and update. Don't claim a fix.
                                classification["applied"] = False
                    silent_classifications.append(classification)

            await session.commit()

        result = {
            "checked_at": now.isoformat(),
            "grace_active": grace_active,
            "stuck_processing": stuck_processing,
            "past_due_pending": past_due_pending,
            "past_due_cleared": cleared_count,
            "silent_accounts": silent_accounts,
            "silent_classifications": silent_classifications,
            "autofix_video_ids": autofix_video_ids,
            "silent_devices": silent_devices,
        }

        # Per-cause alertable subset. Used for both the Telegram message
        # and the fingerprint so working-as-designed silences (paused,
        # daily cap) don't trip the dedup or page the operator.
        alertable_classifications = [
            c for c in silent_classifications
            if c["cause"] in ALERTABLE_CAUSES
        ]

        # Logging — one line per non-empty category.
        if stuck_processing:
            logger.warning(
                "[POSTING] Watchdog: %d stuck uploading/scheduled videos "
                "(age > 1h): ids=%s",
                len(stuck_processing),
                [v["video_id"] for v in stuck_processing],
            )
        if past_due_pending:
            logger.warning(
                "[POSTING] Watchdog: %d pending videos past due > 2h "
                "(scheduled_time cleared, will re-slot): ids=%s",
                len(past_due_pending),
                [v["video_id"] for v in past_due_pending],
            )
        if silent_accounts:
            logger.warning(
                "[POSTING] Watchdog: %d active account(s) with due "
                "backlog but no successful post in 3h: %s",
                len(silent_accounts), silent_accounts,
            )
            for classification in silent_classifications:
                cause = classification["cause"]
                username = classification["account"]
                payload = classification.get("payload", {})
                if classification.get("applied"):
                    logger.warning(
                        "[POSTING] Watchdog autofix: @%s cause=%s "
                        "action=clear_scheduled_time video=%s overdue=%sm",
                        username, cause,
                        payload.get("video_id"),
                        payload.get("overdue_minutes"),
                    )
                elif cause in ALERTABLE_CAUSES:
                    logger.warning(
                        "[POSTING] Watchdog: @%s silent cause=%s payload=%s",
                        username, cause, payload,
                    )
                else:
                    logger.info(
                        "[POSTING] Watchdog: @%s silent (working as "
                        "designed) cause=%s payload=%s",
                        username, cause, payload,
                    )
        if silent_devices:
            logger.warning(
                "[POSTING] Watchdog: %d active device(s) with due "
                "backlog but no successful post in 3h: %s",
                len(silent_devices),
                [d["name"] or d["device_id"] for d in silent_devices],
            )
        any_stall = bool(
            stuck_processing or past_due_pending
            or alertable_classifications or silent_devices
        )
        if not any_stall and not grace_active:
            logger.info("[POSTING] Watchdog: farm healthy, no stalls detected")

        # Broadcast autofix events (one per fixed video) so the admin UI
        # can show a "watchdog repaired schedule" toast.
        for classification in silent_classifications:
            if not classification.get("applied"):
                continue
            try:
                await self.broadcaster.broadcast("watchdog:autofix", {
                    "account": classification["account"],
                    "cause": classification["cause"],
                    "action": classification["fix_action"],
                    "video_id": classification["payload"].get("video_id"),
                    "ts": int(time.time() * 1000),
                })
            except Exception:
                logger.exception(
                    "[POSTING] Watchdog autofix broadcast failed for @%s",
                    classification["account"],
                )

        # Admin WS broadcast — fires every tick; frontend can dedup.
        try:
            await self.broadcaster.broadcast("watchdog:stall", {
                "checked_at": result["checked_at"],
                "grace_active": grace_active,
                "healthy": not any_stall,
                "stalls": [
                    {
                        "type": "stuck_processing",
                        "count": len(stuck_processing),
                        "video_ids": [v["video_id"] for v in stuck_processing],
                    },
                    {
                        "type": "past_due_pending",
                        "count": len(past_due_pending),
                        "cleared": cleared_count,
                        "video_ids": [v["video_id"] for v in past_due_pending],
                    },
                    {
                        "type": "silent_account",
                        "count": len(silent_accounts),
                        "alertable_count": len(alertable_classifications),
                        "usernames": silent_accounts,
                        "classifications": silent_classifications,
                        "autofix_video_ids": autofix_video_ids,
                    },
                    {
                        "type": "silent_device",
                        "count": len(silent_devices),
                        "devices": silent_devices,
                    },
                ],
                "ts": int(time.time() * 1000),
            })
        except Exception:
            logger.exception("[POSTING] Watchdog WS broadcast failed")

        # Telegram alert with fingerprint-based dedup + 6h re-nag cap.
        # One consolidated message per tick, never one-per-category.
        # Cause classifications change the fingerprint so a paused
        # account flipping to device_offline re-fires the alert.
        if self.telegram_bot is not None and any_stall:
            fingerprint_parts = []
            for key, items in (
                ("stuck", sorted(str(v["video_id"]) for v in stuck_processing)),
                ("past_due", sorted(str(v["video_id"]) for v in past_due_pending)),
                # Only alertable classifications enter the fingerprint;
                # otherwise a paused-account-with-backlog would re-fire
                # alerts forever.
                ("silent_acc", sorted(
                    f"{c['account']}:{c['cause']}"
                    for c in alertable_classifications
                )),
                ("silent_dev", sorted(str(d["id"]) for d in silent_devices)),
            ):
                if items:
                    fingerprint_parts.append(f"{key}:{','.join(items)}")
            fingerprint = "|".join(fingerprint_parts)
            now_ts = time.time()
            last_fp = self._watchdog_last_fingerprint.get("all", "")
            last_ts = self._watchdog_last_alert_at.get("all", 0.0)
            # Fire if fingerprint changed (new victims) OR 6h since last alert.
            should_alert = (
                fingerprint != last_fp
                or (now_ts - last_ts) >= self._watchdog_alert_cooldown_s
            )
            if should_alert:
                lines = ["🚨 <b>Farm watchdog — stalls detected</b>"]
                lines.append(f"Checked: {now.strftime('%Y-%m-%d %H:%M UTC')}")
                lines.append("")
                if stuck_processing:
                    ids_preview = ", ".join(
                        f"v#{v['video_id']} @{v['account']}"
                        for v in stuck_processing[:5]
                    )
                    more = "" if len(stuck_processing) <= 5 else f" (+{len(stuck_processing)-5} more)"
                    lines.append(f"📦 Stuck uploading/scheduled: {len(stuck_processing)}")
                    lines.append(f"   {ids_preview}{more}")
                if past_due_pending:
                    ids_preview = ", ".join(
                        f"v#{v['video_id']} @{v['account']}"
                        for v in past_due_pending[:5]
                    )
                    more = "" if len(past_due_pending) <= 5 else f" (+{len(past_due_pending)-5} more)"
                    lines.append(f"⏰ Past-due pending: {len(past_due_pending)} (re-slotted)")
                    lines.append(f"   {ids_preview}{more}")
                if silent_classifications:
                    total_silent = len(silent_classifications)
                    alertable_count = len(alertable_classifications)
                    lines.append(
                        f"🚨 Silent accounts: {total_silent} "
                        f"(alertable: {alertable_count})"
                    )
                    for c in silent_classifications[:10]:
                        lines.append(f"   {_format_silent_classification(c)}")
                    if total_silent > 10:
                        lines.append(f"   (+{total_silent - 10} more)")
                if silent_devices:
                    dev_names = [d["name"] or d["device_id"] for d in silent_devices]
                    lines.append(f"📱 Silent devices (no post in 3h): {len(silent_devices)}")
                    lines.append(f"   {', '.join(dev_names)}")
                lines.append("")
                lines.append("<i>Next re-alert in 6h unless victims change.</i>")
                try:
                    await self.telegram_bot.send_notification("\n".join(lines))
                    self._watchdog_last_alert_at["all"] = now_ts
                    self._watchdog_last_fingerprint["all"] = fingerprint
                except Exception:
                    logger.exception("[POSTING] Watchdog Telegram alert failed")
        elif self.telegram_bot is not None and not any_stall and not grace_active:
            # Optional "all clear" — fires once when stalls drain to empty.
            prev_fp = self._watchdog_last_fingerprint.get("all", "")
            if prev_fp:
                try:
                    await self.telegram_bot.send_notification(
                        "✅ <b>Farm watchdog — all clear</b>\n"
                        "All prior stalls resolved."
                    )
                except Exception:
                    logger.exception("[POSTING] Watchdog all-clear send failed")
                self._watchdog_last_fingerprint["all"] = ""
                self._watchdog_last_alert_at["all"] = 0.0

        return result

    # ------------------------------------------------------------------
    # Job: auto_restart_stalled_devices (every 2 min) — phone respawn
    # ------------------------------------------------------------------

    async def auto_restart_stalled_devices(self) -> dict[str, Any]:
        """Kill the phone process when it gets stuck in WAITING_FOR_INSTAGRAM.

        Failure mode this addresses (Honor FNE-NX9 / MagicOS, observed
        2026-05-05): the AccessibilityService is alive, the WS is healthy,
        but Honor's aggressive background-app suppression refuses to bring
        Instagram to the foreground after we tap the launcher icon. The
        FSM polls in WAITING_FOR_INSTAGRAM until the 10-minute total
        cap fires and reports a bare ``error_message='Timeout'`` — the
        diagnostic exactly because no state-specific timeout fired
        (the per-state TIMEOUT_MS is much shorter).

        Detection: for each active online device, look at the last
        PostLog row in the past 20 min where ``result IN
        ('timeout','failed')``. If it has ``error_message`` exactly
        equal to ``'Timeout'`` (no "in state" suffix), the phone is
        stuck and only a clean process kill will recover it.

        Tuning history:
        - 2026-05-05 v1: required 2 bare-Timeouts in a row, 30 min
          cooldown. Combined gave ~35 min between effective restarts,
          which was longer than Honor's re-wedge interval (observed
          ~13 min post-restart). Posts piled up in the gap.
        - 2026-05-05 v2 (now): 1 bare-Timeout fires; 15 min cooldown.
          Trades one extra restart per hour for ~3x faster recovery
          when Honor MagicOS keeps suppressing IG foreground.

        Side effect: send ``cmd.app_force_restart`` over WS. The phone
        kills its own process; Android's sticky foreground services
        (WebSocketClientService + AssistService + VideoTransferService)
        respawn it within seconds. We stamp ``_last_force_restart_at``
        with the cooldown so a phone that crashes-and-restarts without
        recovering doesn't get force-killed every 2 minutes.

        Returns the action dict for testability. Also broadcasts
        ``device:auto_restart`` on the admin WS and pages Telegram.
        """
        now = _utcnow()
        now_epoch = time.time()
        cooldown_s = 15 * 60.0
        lookback = now - timedelta(minutes=20)
        bare_timeout = "Timeout"
        actions: list[dict[str, Any]] = []

        async with self.session_factory() as session:
            devices = (await session.execute(
                select(Device.id, Device.device_id, Device.name)
                .where(
                    Device.is_active.is_(True),
                    Device.status != "offline",
                )
            )).all()

            for row in devices:
                device_pk = int(row[0])
                last_kicked = self._last_force_restart_at.get(device_pk, 0.0)
                if (now_epoch - last_kicked) < cooldown_s:
                    continue

                last_one = (await session.execute(
                    select(PostLog.result, PostLog.error_message, PostLog.timestamp)
                    .where(
                        PostLog.device_id == device_pk,
                        PostLog.timestamp >= lookback,
                        PostLog.result.in_(["timeout", "failed"]),
                    )
                    .order_by(PostLog.timestamp.desc())
                    .limit(1)
                )).all()
                if len(last_one) < 1:
                    continue
                last = last_one[0]
                if (last.error_message or "").strip() != bare_timeout:
                    continue
                # Don't re-fire on a stale Timeout that we already
                # reacted to: require the bare-Timeout to be newer
                # than our last force-restart for this device.
                last_kick_dt = datetime.fromtimestamp(last_kicked, tz=timezone.utc) \
                    .replace(tzinfo=None)
                if last.timestamp <= last_kick_dt:
                    continue

                self._last_force_restart_at[device_pk] = now_epoch
                reason = (
                    f"Bare-Timeout post failure at "
                    f"{last.timestamp.isoformat()}"
                )
                actions.append({
                    "device_pk": device_pk,
                    "device_id": row[1],
                    "name": row[2],
                    "reason": reason,
                })

        if not actions:
            return {"checked_at": now.isoformat(), "restarts": []}

        for action in actions:
            try:
                await self.bridge.send_app_force_restart(
                    action["device_pk"], action["reason"],
                )
                logger.warning(
                    "[POSTING] Auto-restart sent to device %s (%s): %s",
                    action["name"] or action["device_id"],
                    action["device_pk"], action["reason"],
                )
            except Exception:
                logger.exception(
                    "[POSTING] Auto-restart send failed for device %s",
                    action["device_pk"],
                )
                continue

            try:
                await self.broadcaster.broadcast("device:auto_restart", {
                    "device_id": action["device_pk"],
                    "name": action["name"] or action["device_id"],
                    "reason": action["reason"],
                    "ts": int(now_epoch * 1000),
                })
            except Exception:
                logger.exception(
                    "[POSTING] Auto-restart broadcast failed for device %s",
                    action["device_pk"],
                )

            if self.telegram_bot is not None:
                try:
                    await self.telegram_bot.send_notification(
                        f"🔄 Auto-restart sent to "
                        f"{action['name'] or action['device_id']} — "
                        f"{action['reason']}. Phone process will respawn."
                    )
                except Exception:
                    logger.exception(
                        "[POSTING] Auto-restart Telegram alert failed"
                    )

        return {"checked_at": now.isoformat(), "restarts": actions}

    # ------------------------------------------------------------------
    # Job: process_pinterest_pins
    # ------------------------------------------------------------------

    async def process_pinterest_pins(self) -> None:
        """Dispatch one eligible Pinterest board/pin task to a shared phone.

        Pinterest is a separate domain, but the physical phone is shared with
        Instagram. The DB calendar check prevents collisions with scheduled
        Reelsomet work; the WebSocket online check protects the live transport.
        """
        now = _utcnow()
        async with self.session_factory() as session:
            row = (await session.execute(
                select(
                    PinterestPin,
                    PinterestAccount,
                    PinterestSchedulerSettings,
                    PinterestBoard,
                    PinterestAsset,
                )
                .join(PinterestAccount, PinterestAccount.id == PinterestPin.account_id)
                .join(PinterestSchedulerSettings, PinterestSchedulerSettings.account_id == PinterestAccount.id)
                .join(PinterestBoard, PinterestBoard.id == PinterestPin.board_id)
                .join(PinterestAsset, PinterestAsset.id == PinterestPin.asset_id)
                .where(
                    PinterestAccount.status == "active",
                    PinterestSchedulerSettings.enabled.is_(True),
                    PinterestPin.status.in_(["ready", "retry_waiting"]),
                    or_(
                        PinterestPin.next_retry_at.is_(None),
                        PinterestPin.next_retry_at <= now,
                    ),
                    PinterestBoard.status.in_(["active", "needs_create", "failed"]),
                    PinterestAsset.status == "ready",
                )
                .order_by(
                    PinterestPin.priority.desc(),
                    PinterestPin.order_index.asc(),
                    PinterestPin.created_at.asc(),
                )
                .limit(1)
            )).first()
            if row is None:
                return

            pin, account, settings, board, asset = row
            if account.device_id is None:
                logger.info(
                    "[PINTEREST] Skip pin %s: account %s has no assigned device",
                    pin.external_id,
                    account.username,
                )
                return
            device_id = int(account.device_id)
            if not self.ws_manager.is_online(device_id):
                logger.info(
                    "[PINTEREST] Skip pin %s: device %s is not connected",
                    pin.external_id,
                    device_id,
                )
                return

            cadence_block = await self._pinterest_cadence_block(
                session=session,
                account=account,
                settings=settings,
                now=now,
            )
            if cadence_block is not None:
                logger.info(
                    "[PINTEREST] Skip pin %s for account %s: %s",
                    pin.external_id,
                    account.username,
                    cadence_block,
                )
                return

            decision = await check_device_calendar(
                session,
                device_id=device_id,
                now=now,
                guard_minutes=settings.reelsomet_guard_minutes,
            )
            if not decision.allowed:
                logger.info(
                    "[PINTEREST] Skip pin %s on device %s: %s",
                    pin.external_id,
                    device_id,
                    decision.reason,
                )
                return

            if board.status != "active":
                await self._dispatch_pinterest_board(
                    session=session,
                    account=account,
                    board=board,
                    device_id=device_id,
                    now=now,
                )
                return

            staged = await self._stage_pinterest_asset(
                session=session,
                asset=asset,
                device_id=device_id,
                now=now,
            )
            if not staged:
                return

            await self._dispatch_pinterest_pin(
                session=session,
                account=account,
                settings=settings,
                board=board,
                asset=asset,
                pin=pin,
                device_id=device_id,
                now=now,
            )

    async def _pinterest_cadence_block(
        self,
        *,
        session: AsyncSession,
        account: PinterestAccount,
        settings: PinterestSchedulerSettings,
        now: datetime,
    ) -> str | None:
        target = int(settings.target_pins_per_day or 0)
        if target <= 0:
            return "target_pins_per_day=0"

        day_start, day_end = farm_time.local_day_bounds_utc(now, settings.timezone)
        posted_today = int(
            await session.scalar(
                select(func.count(PinterestPin.id)).where(
                    PinterestPin.account_id == account.id,
                    PinterestPin.status == "posted",
                    PinterestPin.posted_at.is_not(None),
                    PinterestPin.posted_at >= day_start,
                    PinterestPin.posted_at < day_end,
                )
            )
            or 0
        )
        if posted_today >= target:
            return f"daily target reached ({posted_today}/{target})"

        window_block = _pinterest_posting_window_block(
            posting_windows_json=settings.posting_windows_json,
            timezone_name=settings.timezone,
            now=now,
        )
        if window_block is not None:
            return window_block

        min_gap_minutes = int(settings.min_gap_minutes or 0)
        if min_gap_minutes <= 0:
            return None

        latest_posted_at = await session.scalar(
            select(func.max(PinterestPin.posted_at)).where(
                PinterestPin.account_id == account.id,
                PinterestPin.status == "posted",
                PinterestPin.posted_at.is_not(None),
            )
        )
        latest_attempt_at = await session.scalar(
            select(func.max(func.coalesce(PinterestPostAttempt.finished_at, PinterestPostAttempt.started_at))).where(
                PinterestPostAttempt.account_id == account.id,
                PinterestPostAttempt.task_type == "pinterest.publish_pin",
                PinterestPostAttempt.started_at.is_not(None),
            )
        )
        latest_activity_at = max(
            (value for value in (latest_posted_at, latest_attempt_at) if value is not None),
            default=None,
        )
        if latest_activity_at is None:
            return None

        next_allowed_at = latest_activity_at + timedelta(minutes=min_gap_minutes)
        if next_allowed_at <= now:
            return None
        wait_seconds = max(0, int((next_allowed_at - now).total_seconds()))
        wait_minutes = math.ceil(wait_seconds / 60)
        return f"min gap {min_gap_minutes}m; wait {wait_minutes}m"

    async def _dispatch_pinterest_board(
        self,
        *,
        session: AsyncSession,
        account: PinterestAccount,
        board: PinterestBoard,
        device_id: int,
        now: datetime,
    ) -> None:
        task_id = f"pinterest-board-{board.id}-{uuid.uuid4().hex[:12]}"
        trace_id = f"ptrace-{uuid.uuid4().hex}"
        attempt = PinterestPostAttempt(
            task_id=task_id,
            trace_id=trace_id,
            task_type="pinterest.ensure_board",
            status="running",
            account_id=account.id,
            board_id=board.id,
            device_id=device_id,
            started_at=now,
        )
        board.status = "creating"
        board.last_create_attempt_at = now
        session.add(attempt)
        await session.flush()
        await session.commit()

        payload = self._pinterest_board_payload(
            task_id=task_id,
            trace_id=trace_id,
            account=account,
            board=board,
        )
        try:
            result = await self.bridge.pinterest_ensure_board(device_id, payload)
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
            if self._pinterest_result_success(result):
                attempt.status = "success"
                attempt.result_code = self._pinterest_result_code(result)
                board.status = "active"
                board.confirmed_at = finished_at
                board.last_error_code = None
                board.last_error_message = None
            else:
                attempt.status = "failed"
                attempt.error_code = self._pinterest_error_code(result)
                attempt.error_message = self._pinterest_error_message(result)
                board.status = "failed"
                board.last_error_code = attempt.error_code
                board.last_error_message = attempt.error_message
        except Exception as exc:
            attempt.finished_at = _utcnow()
            attempt.status = "failed"
            attempt.error_code = "bridge_error"
            attempt.error_message = str(exc)
            board.status = "failed"
            board.last_error_code = attempt.error_code
            board.last_error_message = attempt.error_message
            logger.exception("[PINTEREST] Board provisioning failed for board_id=%s", board.id)
        await session.commit()

    async def _stage_pinterest_asset(
        self,
        *,
        session: AsyncSession,
        asset: PinterestAsset,
        device_id: int,
        now: datetime,
    ) -> bool:
        if not asset.phone_storage_path:
            asset.phone_storage_path = f"/storage/emulated/0/Pictures/Reelsomet/{Path(asset.storage_path).name}"

        batch_id = f"pinterest_assets_{int(time.time())}"
        payload_assets = [stage_payload(self.config, asset, 0)]
        try:
            result = await self.bridge.push_image_assets(device_id, batch_id=batch_id, assets=payload_assets)
        except Exception as exc:
            asset.last_error = str(exc)
            await session.commit()
            logger.exception("[PINTEREST] Asset staging failed for asset_id=%s", asset.id)
            return False

        if self._pinterest_result_success(result):
            asset.phone_staged_at = now
            asset.last_error = None
            await session.commit()
            return True

        asset.last_error = self._pinterest_error_message(result)
        await session.commit()
        logger.info("[PINTEREST] Asset staging rejected for asset_id=%s: %s", asset.id, asset.last_error)
        return False

    async def _dispatch_pinterest_pin(
        self,
        *,
        session: AsyncSession,
        account: PinterestAccount,
        settings: PinterestSchedulerSettings,
        board: PinterestBoard,
        asset: PinterestAsset,
        pin: PinterestPin,
        device_id: int,
        now: datetime,
    ) -> None:
        task_id = f"pinterest-pin-{pin.id}-{uuid.uuid4().hex[:12]}"
        trace_id = f"ptrace-{uuid.uuid4().hex}"
        attempt = PinterestPostAttempt(
            task_id=task_id,
            trace_id=trace_id,
            task_type="pinterest.publish_pin",
            status="running",
            account_id=account.id,
            board_id=board.id,
            pin_id=pin.id,
            device_id=device_id,
            started_at=now,
        )
        pin.status = "posting"
        pin.posting_started_at = now
        pin.attempt_count = int(pin.attempt_count or 0) + 1
        session.add(attempt)
        await session.flush()
        pin.last_attempt_id = attempt.id
        await session.commit()

        payload = self._pinterest_pin_payload(
            task_id=task_id,
            trace_id=trace_id,
            account=account,
            board=board,
            asset=asset,
            pin=pin,
        )
        notification_status: str | None = None
        notification_error: str | None = None
        notification_account = account.username
        notification_board = board.name
        notification_pin_title = pin.title
        notification_pin_external_id = pin.external_id
        try:
            result = await self.bridge.pinterest_publish_pin(device_id, payload)
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
            if self._pinterest_result_success(result):
                attempt.status = "success"
                attempt.result_code = self._pinterest_result_code(result)
                pin.status = "posted"
                pin.posted_at = finished_at
                pin.last_error_code = None
                pin.last_error_message = None
                if result.get("pinterest_pin_url"):
                    pin.pinterest_pin_url = str(result["pinterest_pin_url"])
                notification_status = "success"
            else:
                self._mark_pinterest_pin_failed(
                    pin=pin,
                    attempt=attempt,
                    settings=settings,
                    now=finished_at,
                    error_code=self._pinterest_error_code(result),
                    error_message=self._pinterest_error_message(result),
                )
                if pin.status == "failed":
                    notification_status = "failed"
                    notification_error = attempt.error_message
        except Exception as exc:
            self._mark_pinterest_pin_failed(
                pin=pin,
                attempt=attempt,
                settings=settings,
                now=_utcnow(),
                error_code="bridge_error",
                error_message=str(exc),
            )
            if pin.status == "failed":
                notification_status = "failed"
                notification_error = attempt.error_message
            logger.exception("[PINTEREST] Pin publish failed for pin_id=%s", pin.id)
        await session.commit()
        if notification_status is not None:
            await self._notify_pinterest_pin_result(
                account_username=notification_account,
                board_name=notification_board,
                pin_title=notification_pin_title,
                pin_external_id=notification_pin_external_id,
                status=notification_status,
                error=notification_error,
            )

    def _mark_pinterest_pin_failed(
        self,
        *,
        pin: PinterestPin,
        attempt: PinterestPostAttempt,
        settings: PinterestSchedulerSettings,
        now: datetime,
        error_code: str,
        error_message: str,
    ) -> None:
        attempt.finished_at = now
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_message = error_message
        pin.last_error_code = error_code
        pin.last_error_message = error_message
        if int(pin.attempt_count or 0) < int(settings.max_retries or 0):
            pin.status = "retry_waiting"
            pin.next_retry_at = now + timedelta(minutes=int(settings.retry_delay_minutes or 30))
        else:
            pin.status = "failed"

    async def _notify_pinterest_pin_result(
        self,
        *,
        account_username: str,
        board_name: str,
        pin_title: str,
        pin_external_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if self.telegram_bot is None:
            return

        is_success = status == "success"
        title = "Pinterest pin posted" if is_success else "Pinterest pin failed"
        icon = "✅" if is_success else "❌"
        lines = [
            f"{icon} <b>{title}</b>",
            f"Account: <code>{html.escape(account_username)}</code>",
            f"Board: <code>{html.escape(board_name)}</code>",
            (
                f"Pin: <code>{html.escape(pin_title)}</code> "
                f"(<code>{html.escape(pin_external_id)}</code>)"
            ),
            f"Status: <code>{html.escape(status)}</code>",
        ]
        if error:
            lines.append(f"Error: {html.escape(error)}")

        try:
            await self.telegram_bot.send_notification("\n".join(lines))
        except Exception as exc:
            logger.warning("Pinterest Telegram notification failed: %s", exc)

    async def process_reddit_posts(
        self,
        *,
        force_post_id: int | None = None,
        bypass_cadence: bool = False,
    ) -> None:
        """Dispatch one ready Reddit post, guarded by shared-device calendar."""
        now = _utcnow()
        async with self.session_factory() as session:
            conditions = [
                RedditPost.status.in_(["ready", "retry_waiting"]),
                or_(RedditPost.scheduled_after.is_(None), RedditPost.scheduled_after <= now),
                or_(RedditPost.next_attempt_at.is_(None), RedditPost.next_attempt_at <= now),
                RedditAccount.status == "active",
                RedditAccount.posting_enabled.is_(True),
                RedditSchedulerSettings.posting_enabled.is_(True),
                RedditSubreddit.status == "active",
                RedditSubreddit.posting_allowed.is_(True),
                RedditAsset.status == "ready",
            ]
            if force_post_id is not None:
                conditions.append(RedditPost.id == force_post_id)

            row = (
                await session.execute(
                    select(
                        RedditPost,
                        RedditAccount,
                        RedditSubreddit,
                        RedditAsset,
                        RedditSchedulerSettings,
                    )
                    .select_from(RedditPost)
                    .join(RedditAccount, RedditAccount.id == RedditPost.account_id)
                    .join(RedditSubreddit, RedditSubreddit.id == RedditPost.subreddit_id)
                    .join(RedditAsset, RedditAsset.id == RedditPost.asset_id)
                    .join(RedditSchedulerSettings, RedditSchedulerSettings.account_id == RedditAccount.id)
                    .where(*conditions)
                    .order_by(
                        RedditPost.priority.asc(),
                        RedditPost.order_index.asc(),
                        RedditPost.created_at.asc(),
                        RedditPost.id.asc(),
                    )
                    .limit(1)
                )
            ).first()
            if row is None:
                return

            post, account, subreddit, asset, settings = row
            if not bypass_cadence:
                cadence_block = await self._reddit_cadence_block(
                    session=session,
                    account=account,
                    settings=settings,
                    now=now,
                )
                if cadence_block is not None:
                    logger.info("[REDDIT] Skip post %s: %s", post.id, cadence_block)
                    return

            if account.device_id is None:
                logger.info("[REDDIT] Skip post %s: account has no assigned device", post.id)
                return

            device_id = int(account.device_id)
            if not self.ws_manager.is_online(device_id):
                logger.info("[REDDIT] Skip post %s: device %s is not connected", post.id, device_id)
                return

            decision = await check_reddit_device_guard(
                session,
                account_id=account.id,
                now=now,
                action="post",
            )
            if not decision.allowed:
                logger.info(
                    "[REDDIT] Skip post %s on device %s: %s",
                    post.id,
                    device_id,
                    decision.reason,
                )
                return

            async with self._reddit_dispatch_lock:
                await self._dispatch_reddit_post(
                    session=session,
                    account=account,
                    subreddit=subreddit,
                    post=post,
                    asset=asset,
                    device_id=device_id,
                    now=now,
                )

    async def _reddit_cadence_block(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        settings: RedditSchedulerSettings,
        now: datetime,
    ) -> str | None:
        target = int(settings.target_posts_per_day or 0)
        if target <= 0:
            return "target_posts_per_day=0"

        day_start, day_end = farm_time.local_day_bounds_utc(now, settings.timezone)
        posted_today = int(
            await session.scalar(
                select(func.count(RedditPost.id)).where(
                    RedditPost.account_id == account.id,
                    RedditPost.status == "posted",
                    RedditPost.posted_at.is_not(None),
                    RedditPost.posted_at >= day_start,
                    RedditPost.posted_at < day_end,
                )
            )
            or 0
        )
        if posted_today >= target:
            return f"daily target reached ({posted_today}/{target})"

        window_block = _reddit_posting_window_block(
            posting_window_start=settings.posting_window_start,
            posting_window_end=settings.posting_window_end,
            timezone_name=settings.timezone,
            now=now,
        )
        if window_block is not None:
            return window_block

        min_gap_minutes = int(settings.min_post_gap_minutes or 0)
        if min_gap_minutes <= 0:
            return None

        latest_posted_at = await session.scalar(
            select(func.max(RedditPost.posted_at)).where(
                RedditPost.account_id == account.id,
                RedditPost.status == "posted",
                RedditPost.posted_at.is_not(None),
            )
        )
        latest_attempt_at = await session.scalar(
            select(func.max(func.coalesce(RedditPostAttempt.finished_at, RedditPostAttempt.started_at))).where(
                RedditPostAttempt.account_id == account.id,
                RedditPostAttempt.task_type == "reddit.publish_post",
                RedditPostAttempt.started_at.is_not(None),
            )
        )
        latest_activity_at = max(
            (value for value in (latest_posted_at, latest_attempt_at) if value is not None),
            default=None,
        )
        if latest_activity_at is None:
            return None

        next_allowed_at = latest_activity_at + timedelta(minutes=min_gap_minutes)
        if next_allowed_at <= now:
            return None
        wait_seconds = max(0, int((next_allowed_at - now).total_seconds()))
        wait_minutes = math.ceil(wait_seconds / 60)
        return f"min gap {min_gap_minutes}m; wait {wait_minutes}m"

    async def _dispatch_reddit_post(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        asset: RedditAsset,
        device_id: int,
        now: datetime,
    ) -> None:
        task_id = f"reddit-post-{post.id}-{uuid.uuid4().hex[:12]}"
        trace_id = f"rtrace-{uuid.uuid4().hex}"
        attempt = RedditPostAttempt(
            task_id=task_id,
            trace_id=trace_id,
            task_type="reddit.publish_post",
            status="running",
            account_id=account.id,
            subreddit_id=subreddit.id,
            post_id=post.id,
            device_id=device_id,
            started_at=now,
        )
        post.status = "posting"
        post.posting_started_at = now
        post.attempt_count = int(post.attempt_count or 0) + 1
        session.add(attempt)
        await session.flush()
        post.last_attempt_id = attempt.id
        await session.commit()

        payload = self._reddit_post_payload(
            task_id=task_id,
            trace_id=trace_id,
            account=account,
            subreddit=subreddit,
            post=post,
            asset=asset,
        )
        try:
            result = await self.bridge.reddit_publish_post(device_id, payload)
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
            if self._reddit_result_success(result):
                result_post_id = (
                    str(result.get("redditPostId") or result.get("reddit_post_id") or result.get("postId") or "")
                    or None
                )
                result_permalink = (
                    str(result.get("permalink") or result.get("postPermalink") or "")
                    or None
                )
                verified_in_app = bool(result.get("verifiedInApp"))
                media_verified = bool(result.get("mediaVerified"))
                submitted = bool(result.get("submitted") or result.get("submittedToReddit"))
                public_post, public_lookup_error = await self._lookup_reddit_public_post_with_error(
                    subreddit=subreddit,
                    account=account,
                    title=post.title,
                )
                if public_post is None:
                    if (verified_in_app or submitted) and media_verified and public_lookup_error is not None:
                        logger.warning(
                            "[REDDIT] Public post lookup unavailable for r/%s after phone submit confirmation: %s",
                            subreddit.name,
                            public_lookup_error,
                        )
                    else:
                        self._mark_reddit_post_failed(
                            attempt=attempt,
                            post=post,
                            asset=asset,
                            error_code="reddit_public_post_not_found",
                            error_message="Device reported posted, but subreddit /new did not show the post",
                        )
                elif public_post.get("is_self") is True:
                    self._mark_reddit_post_failed(
                        attempt=attempt,
                        post=post,
                        asset=asset,
                        error_code="reddit_public_post_not_image",
                        error_message="Reddit published a text post instead of an image post",
                    )
                    post.reddit_post_id = str(public_post.get("name") or public_post.get("id") or "") or None
                    permalink = str(public_post.get("permalink") or "")
                    post.permalink = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink
                else:
                    result_post_id = str(public_post.get("name") or public_post.get("id") or "") or None
                    permalink = str(public_post.get("permalink") or "")
                    result_permalink = (
                        f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink or None
                    )

                if attempt.status != "failed":
                    attempt.status = "success"
                    attempt.result_code = self._reddit_result_code(result)
                    post.status = "posted"
                    post.posted_at = finished_at
                    post.reddit_post_id = result_post_id
                    post.permalink = result_permalink or post.permalink
                    post.last_error_code = None
                    post.last_error_message = None
                phone_storage_path = str(result.get("phoneStoragePath") or "").strip()
                if phone_storage_path:
                    asset.phone_storage_path = phone_storage_path
                    asset.phone_staged_at = finished_at
                    asset.last_error = None
            else:
                error_code = self._reddit_error_code(result)
                error_message = self._reddit_error_message(result)
                self._mark_reddit_post_failed(
                    attempt=attempt,
                    post=post,
                    asset=asset,
                    error_code=error_code,
                    error_message=error_message,
                )
                if self._reddit_subreddit_posting_blocked(error_code, error_message):
                    await self._disable_reddit_subreddit_posting(
                        session=session,
                        subreddit=subreddit,
                        current_post=post,
                        reason=error_message,
                    )
        except Exception as exc:
            error_message = str(exc)
            if self._reddit_subreddit_posting_blocked(error_message, error_message):
                self._mark_reddit_post_failed(
                    attempt=attempt,
                    post=post,
                    asset=asset,
                    error_code="subreddit_posting_not_allowed",
                    error_message=error_message,
                )
                await self._disable_reddit_subreddit_posting(
                    session=session,
                    subreddit=subreddit,
                    current_post=post,
                    reason=error_message,
                )
                logger.warning(
                    "[REDDIT] Disabled posting to r/%s after app-level posting block",
                    subreddit.name,
                )
                await session.commit()
                return
            if self._reddit_external_visibility_blocked(subreddit, error_message):
                clean_message = (
                    "Submitted Reddit post was not visible in subreddit /new after publish; "
                    "external community likely filtered it or requires manual approval."
                )
                self._mark_reddit_post_failed(
                    attempt=attempt,
                    post=post,
                    asset=asset,
                    error_code="subreddit_post_not_visible",
                    error_message=clean_message,
                )
                await self._disable_reddit_subreddit_posting(
                    session=session,
                    subreddit=subreddit,
                    current_post=post,
                    reason=clean_message,
                    error_code="subreddit_post_not_visible",
                )
                logger.warning(
                    "[REDDIT] Disabled posting to r/%s after submitted post was not visible",
                    subreddit.name,
                )
                await session.commit()
                return
            if not await self._recover_reddit_post_from_public_lookup(
                attempt=attempt,
                post=post,
                asset=asset,
                account=account,
                subreddit=subreddit,
                error_message=error_message,
            ):
                self._mark_reddit_post_failed(
                    attempt=attempt,
                    post=post,
                    asset=asset,
                    error_code="bridge_error",
                    error_message=error_message,
                )
                logger.exception("[REDDIT] Post dispatch failed for post_id=%s", post.id)
        await session.commit()

    def _reddit_post_payload(
        self,
        *,
        task_id: str,
        trace_id: str,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        asset: RedditAsset,
    ) -> dict[str, Any]:
        filename = Path(asset.storage_path).name
        return {
            "task_id": task_id,
            "trace_id": trace_id,
            "account": {
                "id": account.id,
                "username": account.username,
            },
            "subreddit": {
                "id": subreddit.id,
                "name": subreddit.name,
                "displayName": subreddit.display_name or f"r/{subreddit.name}",
            },
            "post": {
                "id": post.id,
                "externalId": post.external_id,
                "title": post.title,
                "body": post.body or "",
                "flair": post.flair,
                "nsfw": bool(post.nsfw),
            },
            "media": {
                "assetId": asset.id,
                "filename": filename,
                "url": build_reddit_asset_url(self.config, asset),
                "mimeType": asset.mime_type,
                "mediaHash": asset.media_hash,
                "phoneStoragePath": asset.phone_storage_path,
            },
            "policy": {
                "requiresGuard": True,
                "noInstagramInterference": True,
            },
        }

    @staticmethod
    def _mark_reddit_post_failed(
        *,
        attempt: RedditPostAttempt,
        post: RedditPost,
        asset: RedditAsset,
        error_code: str,
        error_message: str,
    ) -> None:
        attempt.finished_at = _utcnow()
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_message = error_message
        post.status = "failed"
        post.last_error_code = error_code
        post.last_error_message = error_message
        asset.last_error = error_message

    async def _disable_reddit_subreddit_posting(
        self,
        *,
        session: AsyncSession,
        subreddit: RedditSubreddit,
        current_post: RedditPost,
        reason: str,
        error_code: str = "subreddit_posting_not_allowed",
    ) -> None:
        clean_reason = reason or "Reddit app says posting is not allowed in this community"
        subreddit.posting_allowed = False
        subreddit.status = "needs_attention"
        subreddit.last_error = clean_reason
        subreddit.last_checked_at = _utcnow()
        current_post.status = "cancelled"
        current_post.last_error_code = error_code
        current_post.last_error_message = clean_reason

        queued_posts = (
            await session.execute(
                select(RedditPost).where(
                    RedditPost.subreddit_id == subreddit.id,
                    RedditPost.id != current_post.id,
                    RedditPost.status.in_(["ready", "staged", "retry_waiting", "failed"]),
                )
            )
        ).scalars().all()
        for queued_post in queued_posts:
            queued_post.status = "cancelled"
            queued_post.last_error_code = error_code
            queued_post.last_error_message = clean_reason

    async def _recover_reddit_post_from_public_lookup(
        self,
        *,
        attempt: RedditPostAttempt,
        post: RedditPost,
        asset: RedditAsset,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        error_message: str,
    ) -> bool:
        public_post = await self._lookup_reddit_public_post(
            subreddit=subreddit,
            account=account,
            title=post.title,
        )
        if public_post is None or public_post.get("is_self") is True:
            return False

        finished_at = _utcnow()
        reddit_post_id = str(public_post.get("name") or public_post.get("id") or "") or None
        permalink = str(public_post.get("permalink") or "")
        if permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"

        attempt.finished_at = finished_at
        attempt.status = "success"
        attempt.result_code = "public_verify_recovered"
        attempt.error_code = None
        attempt.error_message = None
        attempt.raw_result_json = json.dumps(
            {
                "success": True,
                "source": "reddit_public_recovery",
                "bridgeError": error_message,
                "redditPostId": reddit_post_id,
                "permalink": permalink or None,
            },
            ensure_ascii=False,
        )
        post.status = "posted"
        post.posted_at = finished_at
        post.reddit_post_id = reddit_post_id
        post.permalink = permalink or post.permalink
        post.last_error_code = None
        post.last_error_message = None
        asset.last_error = None
        logger.info("[REDDIT] Recovered post_id=%s via public subreddit listing", post.id)
        return True

    async def process_reddit_comment_scans(self) -> None:
        """Scan one posted Reddit thread for new comments via the assigned phone."""
        now = _utcnow()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(RedditPost, RedditAccount, RedditSubreddit, RedditSchedulerSettings)
                    .select_from(RedditPost)
                    .join(RedditAccount, RedditAccount.id == RedditPost.account_id)
                    .join(RedditSubreddit, RedditSubreddit.id == RedditPost.subreddit_id)
                    .join(RedditSchedulerSettings, RedditSchedulerSettings.account_id == RedditAccount.id)
                    .where(
                        RedditPost.status == "posted",
                        RedditPost.posted_at.is_not(None),
                        RedditAccount.status == "active",
                        RedditAccount.commenting_enabled.is_(True),
                        RedditSchedulerSettings.scan_comments_enabled.is_(True),
                        RedditSubreddit.status == "active",
                        RedditSubreddit.commenting_allowed.is_(True),
                    )
                    .order_by(RedditPost.posted_at.desc(), RedditPost.id.desc())
                    .limit(10)
                )
            ).all()
            if not rows:
                return

            for post, account, subreddit, settings in rows:
                interval_minutes = int(settings.comment_scan_interval_minutes or 0)
                if interval_minutes > 0:
                    latest_scan_at = await session.scalar(
                        select(func.max(func.coalesce(RedditPostAttempt.finished_at, RedditPostAttempt.started_at))).where(
                            RedditPostAttempt.post_id == post.id,
                            RedditPostAttempt.task_type == "reddit.scan_comments",
                            RedditPostAttempt.started_at.is_not(None),
                        )
                    )
                    if latest_scan_at is not None and latest_scan_at + timedelta(minutes=interval_minutes) > now:
                        continue

                device_id = int(account.device_id) if account.device_id is not None else None

                async with self._reddit_dispatch_lock:
                    await self._dispatch_reddit_comment_scan(
                        session=session,
                        account=account,
                        subreddit=subreddit,
                        post=post,
                        device_id=device_id,
                        now=now,
                    )
                return

    async def _dispatch_reddit_comment_scan(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        device_id: int | None,
        now: datetime,
    ) -> None:
        task_id = f"reddit-scan-{post.id}-{uuid.uuid4().hex[:12]}"
        trace_id = f"rtrace-{uuid.uuid4().hex}"
        attempt = RedditPostAttempt(
            task_id=task_id,
            trace_id=trace_id,
            task_type="reddit.scan_comments",
            status="running",
            account_id=account.id,
            subreddit_id=subreddit.id,
            post_id=post.id,
            device_id=device_id,
            started_at=now,
        )
        session.add(attempt)
        await session.flush()
        await session.commit()

        payload = self._reddit_comment_scan_payload(
            task_id=task_id,
            trace_id=trace_id,
            account=account,
            subreddit=subreddit,
            post=post,
        )
        finished_at = _utcnow()
        public_scan = await self._lookup_reddit_public_comments(
            subreddit=subreddit,
            account=account,
            post=post,
        )
        if public_scan is not None:
            created, seen = await self._upsert_reddit_scanned_comments(
                session=session,
                account=account,
                subreddit=subreddit,
                post=post,
                comments=public_scan.get("comments"),
                now=finished_at,
            )
            if public_scan.get("redditPostId"):
                post.reddit_post_id = str(public_scan["redditPostId"])
            if public_scan.get("permalink"):
                post.permalink = str(public_scan["permalink"])
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(
                {"success": True, "publicScan": public_scan},
                ensure_ascii=False,
            )
            attempt.status = "success"
            attempt.result_code = f"comments_seen={seen};comments_created={created};source=server_public_json"
            await session.commit()
            return

        if device_id is None:
            attempt.finished_at = _utcnow()
            attempt.status = "cancelled"
            attempt.result_code = "phone_fallback_skipped"
            attempt.error_code = "no_device_assigned"
            attempt.error_message = "Public Reddit comment scan failed and account has no assigned device"
            await session.commit()
            return

        if not self.ws_manager.is_online(device_id):
            attempt.finished_at = _utcnow()
            attempt.status = "cancelled"
            attempt.result_code = "phone_fallback_skipped"
            attempt.error_code = "device_offline"
            attempt.error_message = f"Public Reddit comment scan failed and device {device_id} is offline"
            await session.commit()
            return

        decision = await check_reddit_device_guard(
            session,
            account_id=account.id,
            now=_utcnow(),
            action="comment_scan",
        )
        if not decision.allowed:
            attempt.finished_at = _utcnow()
            attempt.status = "cancelled"
            attempt.result_code = "phone_fallback_skipped"
            attempt.error_code = decision.reason
            attempt.error_message = f"Phone Reddit comment scan fallback skipped: {decision.reason}"
            logger.info(
                "[REDDIT] Skip phone comment scan fallback for post %s on device %s: %s",
                post.id,
                device_id,
                decision.reason,
            )
            await session.commit()
            return

        try:
            result = await self.bridge.reddit_scan_comments(device_id, payload)
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
            if self._reddit_result_success(result):
                if result.get("redditPostId"):
                    post.reddit_post_id = str(result["redditPostId"])
                if result.get("permalink"):
                    post.permalink = str(result["permalink"])
                created, seen = await self._upsert_reddit_scanned_comments(
                    session=session,
                    account=account,
                    subreddit=subreddit,
                    post=post,
                    comments=result.get("comments") if isinstance(result, dict) else None,
                    now=finished_at,
                )
                if seen == 0 and isinstance(result, dict):
                    public_created, public_seen = await self._apply_reddit_public_comment_scan(
                        session=session,
                        account=account,
                        subreddit=subreddit,
                        post=post,
                        attempt=attempt,
                        result=result,
                        now=finished_at,
                    )
                    created += public_created
                    seen += public_seen
                attempt.status = "success"
                attempt.result_code = f"comments_seen={seen};comments_created={created}"
            else:
                public_created, public_seen = await self._apply_reddit_public_comment_scan(
                    session=session,
                    account=account,
                    subreddit=subreddit,
                    post=post,
                    attempt=attempt,
                    result=result if isinstance(result, dict) else {},
                    now=finished_at,
                )
                if public_seen > 0:
                    attempt.status = "success"
                    attempt.result_code = f"comments_seen={public_seen};comments_created={public_created}"
                    attempt.error_code = None
                    attempt.error_message = None
                else:
                    attempt.status = "failed"
                    attempt.error_code = self._reddit_error_code(result)
                    attempt.error_message = self._reddit_error_message(result)
        except Exception as exc:
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            public_created, public_seen = await self._apply_reddit_public_comment_scan(
                session=session,
                account=account,
                subreddit=subreddit,
                post=post,
                attempt=attempt,
                result={"phoneError": str(exc)},
                now=finished_at,
            )
            if public_seen > 0:
                attempt.status = "success"
                attempt.result_code = f"comments_seen={public_seen};comments_created={public_created}"
                attempt.error_code = None
                attempt.error_message = None
            else:
                attempt.status = "failed"
                attempt.error_code = "bridge_error"
                attempt.error_message = str(exc)
                logger.exception("[REDDIT] Comment scan failed for post_id=%s", post.id)
        await session.commit()

    @staticmethod
    def _reddit_comment_scan_payload(
        *,
        task_id: str,
        trace_id: str,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "trace_id": trace_id,
            "account": {
                "id": account.id,
                "username": account.username,
            },
            "subreddit": {
                "id": subreddit.id,
                "name": subreddit.name,
                "displayName": subreddit.display_name or f"r/{subreddit.name}",
            },
            "post": {
                "id": post.id,
                "externalId": post.external_id,
                "redditPostId": post.reddit_post_id,
                "permalink": post.permalink,
                "title": post.title,
            },
            "policy": {
                "requiresGuard": True,
                "noInstagramInterference": True,
                "publicJsonOnly": True,
            },
        }

    async def _upsert_reddit_scanned_comments(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        comments: Any,
        now: datetime,
    ) -> tuple[int, int]:
        if not isinstance(comments, list):
            return 0, 0

        created = 0
        seen = 0
        own_user = (account.username or "").strip().lower()
        for item in comments:
            if not isinstance(item, dict):
                continue
            body = self._normalize_reddit_comment_body(item.get("body") or item.get("text") or "")
            if not body:
                continue
            author = self._normalize_reddit_comment_author(item.get("author"))
            if author == "unknown":
                continue
            if author.lower() == own_user:
                continue
            body_hash = hashlib.sha256(body.lower().encode("utf-8")).hexdigest()
            seen += 1
            existing = (
                await session.execute(
                    select(RedditComment).where(
                        RedditComment.post_id == post.id,
                        RedditComment.body_hash == body_hash,
                        RedditComment.author == author,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing.last_seen_at = now
                existing.last_error = None
                continue

            comment = RedditComment(
                account_id=account.id,
                subreddit_id=subreddit.id,
                post_id=post.id,
                reddit_comment_id=str(item.get("redditCommentId") or item.get("id") or "") or None,
                reddit_parent_id=str(item.get("redditParentId") or item.get("parentId") or "") or None,
                author=author,
                body=body,
                body_hash=body_hash,
                permalink=str(item.get("permalink") or "") or None,
                commented_at=now,
                status="needs_reply",
                classification="scanned_visible_comment",
                last_seen_at=now,
            )
            session.add(comment)
            created += 1
        return created, seen

    @staticmethod
    def _normalize_reddit_comment_body(value: Any) -> str:
        body = " ".join(str(value or "").strip().split())
        if len(body) > 2000:
            body = body[:2000].rstrip()
        return body

    @staticmethod
    def _normalize_reddit_comment_author(value: Any) -> str:
        author = str(value or "").strip()
        if author.startswith("u/"):
            author = author[2:]
        if author.startswith("@"):
            author = author[1:]
        return author[:128] if author else "unknown"

    async def _lookup_reddit_public_comments(
        self,
        *,
        subreddit: RedditSubreddit,
        account: RedditAccount,
        post: RedditPost,
    ) -> dict[str, Any] | None:
        permalink = post.permalink
        reddit_post_id = post.reddit_post_id
        if not permalink:
            public_post = await self._lookup_reddit_public_post(
                subreddit=subreddit,
                account=account,
                title=post.title,
            )
            if public_post is None:
                return None
            reddit_post_id = str(public_post.get("name") or public_post.get("id") or "") or reddit_post_id
            permalink = self._absolute_reddit_permalink(str(public_post.get("permalink") or ""))

        if not permalink:
            return None

        try:
            listing = await asyncio.to_thread(self._fetch_reddit_comments_listing, permalink)
        except Exception as exc:
            logger.warning("[REDDIT] Public comment lookup failed for post_id=%s: %s", post.id, exc)
            return None

        return {
            "source": "reddit_public_json",
            "redditPostId": reddit_post_id,
            "permalink": permalink,
            "comments": self._parse_reddit_public_comments(listing),
        }

    async def _apply_reddit_public_comment_scan(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        attempt: RedditPostAttempt,
        result: dict[str, Any],
        now: datetime,
    ) -> tuple[int, int]:
        public_scan = await self._lookup_reddit_public_comments(
            subreddit=subreddit,
            account=account,
            post=post,
        )
        if public_scan is None:
            return 0, 0

        public_created, public_seen = await self._upsert_reddit_scanned_comments(
            session=session,
            account=account,
            subreddit=subreddit,
            post=post,
            comments=public_scan.get("comments"),
            now=now,
        )
        if public_scan.get("redditPostId"):
            post.reddit_post_id = str(public_scan["redditPostId"])
        if public_scan.get("permalink"):
            post.permalink = str(public_scan["permalink"])
        result["publicScan"] = public_scan
        attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
        return public_created, public_seen

    async def generate_reddit_reply_drafts(self) -> None:
        """Generate one Grok-backed Reddit reply draft without touching Android."""
        provider = self.config.llm_provider or "grok"
        api_key = self.config.llm_api_key or ""
        model = self.config.llm_model or "grok-3-mini"
        if not api_key:
            logger.debug("[REDDIT] Skip Grok reply generation: llm.api_key is empty")
            return

        now = _utcnow()
        async with self.session_factory() as session:
            existing_draft = (
                select(RedditReplyDraft.id)
                .where(
                    RedditReplyDraft.comment_id == RedditComment.id,
                    RedditReplyDraft.status.in_(
                        ["drafted", "needs_review", "approved", "auto_approved", "replying", "posted"]
                    ),
                )
                .exists()
            )
            row = (
                await session.execute(
                    select(RedditComment, RedditPost, RedditAccount, RedditSubreddit, RedditSchedulerSettings)
                    .select_from(RedditComment)
                    .join(RedditPost, RedditPost.id == RedditComment.post_id)
                    .join(RedditAccount, RedditAccount.id == RedditComment.account_id)
                    .join(RedditSubreddit, RedditSubreddit.id == RedditComment.subreddit_id)
                    .join(RedditSchedulerSettings, RedditSchedulerSettings.account_id == RedditAccount.id)
                    .where(
                        RedditComment.status == "needs_reply",
                        ~existing_draft,
                        RedditAccount.status == "active",
                        RedditAccount.commenting_enabled.is_(True),
                        RedditAccount.auto_reply_enabled.is_(True),
                        RedditSchedulerSettings.scan_comments_enabled.is_(True),
                        RedditSchedulerSettings.auto_reply_enabled.is_(True),
                        RedditSubreddit.status == "active",
                        RedditSubreddit.commenting_allowed.is_(True),
                    )
                    .order_by(RedditComment.created_at.asc(), RedditComment.id.asc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return

            comment, post, account, subreddit, settings = row
            system_prompt, user_prompt = self._reddit_reply_prompt(
                account=account,
                subreddit=subreddit,
                post=post,
                comment=comment,
            )
            prompt_payload = {
                "system": system_prompt,
                "user": user_prompt,
                "provider": provider,
                "model": model,
            }
            prompt_json = json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True)
            prompt_hash = hashlib.sha256(prompt_json.encode("utf-8")).hexdigest()

            started = time.monotonic()
            grok = RedditGrokCall(
                account_id=account.id,
                comment_id=comment.id,
                model=model,
                prompt_version="reddit_reply_v1",
                prompt_hash=prompt_hash,
                request_json=prompt_json,
                status="success",
            )
            session.add(grok)
            await session.flush()

            try:
                reply_text = await self._call_reddit_reply_llm(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    timeout=float(self.config.engagement_llm_timeout or 30.0),
                )
                reply_text = self._normalize_reddit_reply(reply_text)
                grok.output_text = reply_text
                grok.response_json = json.dumps({"text": reply_text}, ensure_ascii=False)
                grok.latency_ms = int((time.monotonic() - started) * 1000)

                draft = RedditReplyDraft(
                    comment_id=comment.id,
                    account_id=account.id,
                    status="auto_approved" if settings.auto_reply_enabled else "drafted",
                    reply_text=reply_text,
                    source="grok",
                    prompt_version="reddit_reply_v1",
                    grok_call_id=grok.id,
                    approved_by="auto" if settings.auto_reply_enabled else None,
                    approved_at=now if settings.auto_reply_enabled else None,
                )
                comment.status = "drafted"
                comment.last_error = None
                session.add(draft)
            except Exception as exc:
                grok.status = "failed"
                grok.provider_error = str(exc)
                grok.latency_ms = int((time.monotonic() - started) * 1000)
                comment.last_error = str(exc)
                logger.warning("[REDDIT] Grok reply generation failed for comment_id=%s: %s", comment.id, exc)

            await session.commit()

    async def _call_reddit_reply_llm(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        timeout: float,
    ) -> str:
        from server.llm_client import PROVIDER_DEFAULTS, _call_anthropic, _call_openai_compat

        base_url = self.config.llm_base_url or PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")
        if provider == "anthropic":
            return await _call_anthropic(base_url, api_key, model, system_prompt, user_prompt, timeout)
        return await _call_openai_compat(base_url, api_key, model, system_prompt, user_prompt, timeout)

    @staticmethod
    def _reddit_reply_prompt(
        *,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        comment: RedditComment,
    ) -> tuple[str, str]:
        system_prompt = (
            "You write short Reddit replies for a creator account. "
            "Sound natural to US men age 25-50: confident, playful, direct, not corporate. "
            "Return one reply only. No links, no hashtags, no markdown, no emojis unless the user used one. "
            "Keep it under 140 characters and do not mention that you are an AI."
        )
        user_prompt = (
            f"Account: u/{account.username}\n"
            f"Subreddit: r/{subreddit.name}\n"
            f"Post title: {post.title}\n"
            f"Comment author: {comment.author or 'unknown'}\n"
            f"Comment: {comment.body}\n\n"
            "Write the reply:"
        )
        return system_prompt, user_prompt

    @staticmethod
    def _normalize_reddit_reply(value: str) -> str:
        reply = " ".join(str(value or "").strip().split())
        if len(reply) > 280:
            reply = reply[:277].rstrip() + "..."
        if not reply:
            raise ValueError("empty Reddit reply")
        return reply

    async def process_reddit_replies(self) -> None:
        """Dispatch one approved Reddit reply, guarded by shared-device calendar."""
        now = _utcnow()
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(
                        RedditReplyDraft,
                        RedditComment,
                        RedditPost,
                        RedditAccount,
                        RedditSubreddit,
                    )
                    .select_from(RedditReplyDraft)
                    .join(RedditComment, RedditComment.id == RedditReplyDraft.comment_id)
                    .join(RedditPost, RedditPost.id == RedditComment.post_id)
                    .join(RedditAccount, RedditAccount.id == RedditReplyDraft.account_id)
                    .join(RedditSubreddit, RedditSubreddit.id == RedditComment.subreddit_id)
                    .join(RedditSchedulerSettings, RedditSchedulerSettings.account_id == RedditAccount.id)
                    .where(
                        RedditReplyDraft.status.in_(["approved", "auto_approved"]),
                        RedditComment.status.in_(["drafted", "needs_reply"]),
                        RedditAccount.status == "active",
                        RedditAccount.commenting_enabled.is_(True),
                        RedditAccount.auto_reply_enabled.is_(True),
                        RedditSchedulerSettings.scan_comments_enabled.is_(True),
                        RedditSchedulerSettings.auto_reply_enabled.is_(True),
                        RedditSubreddit.status == "active",
                        RedditSubreddit.commenting_allowed.is_(True),
                    )
                    .order_by(RedditReplyDraft.created_at.asc(), RedditReplyDraft.id.asc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return

            draft, comment, post, account, subreddit = row
            if account.device_id is None:
                logger.info("[REDDIT] Skip reply draft %s: account has no assigned device", draft.id)
                return

            device_id = int(account.device_id)
            if not self.ws_manager.is_online(device_id):
                logger.info("[REDDIT] Skip reply draft %s: device %s is not connected", draft.id, device_id)
                return

            decision = await check_reddit_device_guard(
                session,
                account_id=account.id,
                now=now,
                action="reply",
            )
            if not decision.allowed:
                logger.info(
                    "[REDDIT] Skip reply draft %s on device %s: %s",
                    draft.id,
                    device_id,
                    decision.reason,
                )
                return

            async with self._reddit_dispatch_lock:
                await self._dispatch_reddit_reply(
                    session=session,
                    account=account,
                    subreddit=subreddit,
                    post=post,
                    comment=comment,
                    draft=draft,
                    device_id=device_id,
                    now=now,
                )

    async def _dispatch_reddit_reply(
        self,
        *,
        session: AsyncSession,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        comment: RedditComment,
        draft: RedditReplyDraft,
        device_id: int,
        now: datetime,
    ) -> None:
        task_id = f"reddit-reply-{draft.id}-{uuid.uuid4().hex[:12]}"
        trace_id = f"rtrace-{uuid.uuid4().hex}"
        attempt = RedditPostAttempt(
            task_id=task_id,
            trace_id=trace_id,
            task_type="reddit.reply_comment",
            status="running",
            account_id=account.id,
            subreddit_id=subreddit.id,
            post_id=post.id,
            comment_id=comment.id,
            reply_draft_id=draft.id,
            device_id=device_id,
            started_at=now,
        )
        draft.status = "replying"
        comment.status = "replying"
        session.add(attempt)
        await session.flush()
        await session.commit()

        payload = self._reddit_reply_payload(
            task_id=task_id,
            trace_id=trace_id,
            account=account,
            subreddit=subreddit,
            post=post,
            comment=comment,
            draft=draft,
        )
        try:
            result = await self.bridge.reddit_reply_comment(device_id, payload)
            finished_at = _utcnow()
            attempt.finished_at = finished_at
            attempt.raw_result_json = json.dumps(result, ensure_ascii=False)
            if self._reddit_result_success(result):
                attempt.status = "success"
                attempt.result_code = self._reddit_result_code(result)
                draft.status = "posted"
                draft.posted_at = finished_at
                draft.reddit_reply_id = (
                    str(result.get("replyId") or result.get("reddit_reply_id") or "")
                    or None
                )
                draft.permalink = (
                    str(result.get("permalink") or result.get("replyPermalink") or "")
                    or draft.permalink
                )
                draft.last_error = None
                comment.status = "replied"
                comment.last_error = None
            else:
                error_code = self._reddit_error_code(result)
                error_message = self._reddit_error_message(result)
                if self._reddit_reply_error_is_retryable(error_code, error_message):
                    self._mark_reddit_reply_retryable(
                        attempt=attempt,
                        draft=draft,
                        comment=comment,
                        error_code=error_code,
                        error_message=error_message,
                    )
                else:
                    self._mark_reddit_reply_failed(
                        attempt=attempt,
                        draft=draft,
                        comment=comment,
                        error_code=error_code,
                        error_message=error_message,
                    )
        except Exception as exc:
            error_message = str(exc)
            if self._reddit_reply_error_is_retryable("bridge_error", error_message):
                self._mark_reddit_reply_retryable(
                    attempt=attempt,
                    draft=draft,
                    comment=comment,
                    error_code="bridge_error",
                    error_message=error_message,
                )
            else:
                self._mark_reddit_reply_failed(
                    attempt=attempt,
                    draft=draft,
                    comment=comment,
                    error_code="bridge_error",
                    error_message=error_message,
                )
            logger.exception("[REDDIT] Reply dispatch failed for draft_id=%s", draft.id)
        await session.commit()

    @staticmethod
    def _reddit_reply_payload(
        *,
        task_id: str,
        trace_id: str,
        account: RedditAccount,
        subreddit: RedditSubreddit,
        post: RedditPost,
        comment: RedditComment,
        draft: RedditReplyDraft,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "trace_id": trace_id,
            "account": {
                "id": account.id,
                "username": account.username,
            },
            "subreddit": {
                "id": subreddit.id,
                "name": subreddit.name,
                "displayName": subreddit.display_name or f"r/{subreddit.name}",
            },
            "post": {
                "id": post.id,
                "externalId": post.external_id,
                "redditPostId": post.reddit_post_id,
                "permalink": post.permalink,
                "title": post.title,
            },
            "comment": {
                "id": comment.id,
                "redditCommentId": comment.reddit_comment_id,
                "author": comment.author,
                "body": comment.body,
                "permalink": comment.permalink,
            },
            "reply": {
                "id": draft.id,
                "text": draft.reply_text,
                "source": draft.source,
                "promptVersion": draft.prompt_version,
            },
            "policy": {
                "requiresGuard": True,
                "noInstagramInterference": True,
            },
        }

    @staticmethod
    def _mark_reddit_reply_failed(
        *,
        attempt: RedditPostAttempt,
        draft: RedditReplyDraft,
        comment: RedditComment,
        error_code: str,
        error_message: str,
    ) -> None:
        attempt.finished_at = _utcnow()
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_message = error_message
        draft.status = "failed"
        draft.last_error = error_message
        comment.status = "failed"
        comment.last_error = error_message

    @staticmethod
    def _mark_reddit_reply_retryable(
        *,
        attempt: RedditPostAttempt,
        draft: RedditReplyDraft,
        comment: RedditComment,
        error_code: str,
        error_message: str,
    ) -> None:
        attempt.finished_at = _utcnow()
        attempt.status = "failed"
        attempt.error_code = error_code
        attempt.error_message = error_message
        draft.status = "auto_approved"
        draft.last_error = error_message
        comment.status = "drafted"
        comment.last_error = error_message

    @staticmethod
    def _reddit_reply_error_is_retryable(error_code: str, error_message: str) -> bool:
        message = f"{error_code} {error_message}".lower()
        return (
            "automation_busy" in message
            or "device" in message and "disconnected" in message
            or "timeout" in message
        )

    @staticmethod
    def _reddit_result_success(result: dict[str, Any]) -> bool:
        if result.get("success") is False:
            return False
        if result.get("success") is True:
            return True
        status_value = str(result.get("status") or result.get("result") or "").lower()
        return status_value in {"ok", "success", "posted", "replied"}

    @staticmethod
    def _reddit_result_code(result: dict[str, Any]) -> str:
        return str(result.get("status") or result.get("result") or "success")

    @staticmethod
    def _reddit_error_code(result: dict[str, Any]) -> str:
        return str(result.get("error_code") or result.get("code") or "device_rejected")

    @staticmethod
    def _reddit_error_message(result: dict[str, Any]) -> str:
        return str(result.get("error_message") or result.get("error") or result.get("message") or "Reddit task failed")

    @staticmethod
    def _reddit_subreddit_posting_blocked(error_code: str, error_message: str) -> bool:
        text = f"{error_code} {error_message}".lower()
        return (
            "subreddit_posting_not_allowed" in text
            or "not allowed to post" in text
            or "can't post in this community" in text
            or "cannot post in this community" in text
            or "only approved users can post" in text
            or "posting is restricted" in text
        )

    @staticmethod
    def _reddit_external_visibility_blocked(subreddit: RedditSubreddit, error_message: str) -> bool:
        if subreddit.mode == "owned":
            return False
        return "post_verify_failed" in str(error_message or "").lower()

    async def _lookup_reddit_public_post(
        self,
        *,
        subreddit: RedditSubreddit,
        account: RedditAccount,
        title: str,
    ) -> dict[str, Any] | None:
        public_post, _ = await self._lookup_reddit_public_post_with_error(
            subreddit=subreddit,
            account=account,
            title=title,
        )
        return public_post

    async def _lookup_reddit_public_post_with_error(
        self,
        *,
        subreddit: RedditSubreddit,
        account: RedditAccount,
        title: str,
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                listing = await asyncio.to_thread(self._fetch_reddit_new_listing, subreddit.name)
                last_error = None
            except Exception as exc:
                last_error = exc
                logger.warning("[REDDIT] Public post lookup failed for r/%s: %s", subreddit.name, exc)
                listing = None
            if listing:
                children = listing.get("data", {}).get("children", [])
                for child in children:
                    data = child.get("data", {}) if isinstance(child, dict) else {}
                    if data.get("title") == title and data.get("author") == account.username:
                        return data, None
            if attempt < 4:
                await asyncio.sleep(2)
        return None, last_error

    @staticmethod
    def _fetch_reddit_new_listing(subreddit: str) -> dict[str, Any]:
        safe_subreddit = "".join(ch for ch in subreddit if ch.isalnum() or ch in {"_", "-"})
        if not safe_subreddit:
            raise ValueError("empty subreddit")
        request = urllib.request.Request(
            f"https://www.reddit.com/r/{safe_subreddit}/new.json?limit=10",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "ReelsometRedditVerifier/1.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _fetch_reddit_comments_listing(permalink: str) -> Any:
        url = permalink.strip()
        if url.startswith("/"):
            url = f"https://www.reddit.com{url}"
        if not url.startswith("https://www.reddit.com/"):
            raise ValueError("unsupported reddit permalink")
        url = url.split("?", 1)[0].rstrip("/")
        if not url.endswith(".json"):
            url = f"{url}.json"
        request = urllib.request.Request(
            f"{url}?limit=50",
            headers={"User-Agent": "ReelsometRedditVerifier/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _parse_reddit_public_comments(listing: Any) -> list[dict[str, Any]]:
        if not isinstance(listing, list) or len(listing) < 2:
            return []
        comments_listing = listing[1]
        children = comments_listing.get("data", {}).get("children", []) if isinstance(comments_listing, dict) else []
        comments: list[dict[str, Any]] = []
        for child in children:
            if not isinstance(child, dict) or child.get("kind") != "t1":
                continue
            data = child.get("data", {})
            if not isinstance(data, dict):
                continue
            body = " ".join(str(data.get("body") or "").strip().split())
            author = str(data.get("author") or "").strip()
            if not body or body in {"[deleted]", "[removed]"} or not author:
                continue
            permalink = FarmScheduler._absolute_reddit_permalink(str(data.get("permalink") or ""))
            comments.append({
                "id": str(data.get("name") or data.get("id") or ""),
                "redditCommentId": str(data.get("name") or data.get("id") or ""),
                "redditParentId": str(data.get("parent_id") or ""),
                "author": author,
                "body": body,
                "permalink": permalink,
            })
        return comments

    @staticmethod
    def _absolute_reddit_permalink(permalink: str) -> str | None:
        value = permalink.strip()
        if not value:
            return None
        if value.startswith("/"):
            return f"https://www.reddit.com{value}"
        return value

    @staticmethod
    def _pinterest_board_payload(
        *,
        task_id: str,
        trace_id: str,
        account: PinterestAccount,
        board: PinterestBoard,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "trace_id": trace_id,
            "account": {
                "id": account.id,
                "username": account.username,
            },
            "board": {
                "id": board.id,
                "key": board.key,
                "name": board.name,
                "description": board.description,
                "visibility": board.visibility,
            },
        }

    @staticmethod
    def _pinterest_pin_payload(
        *,
        task_id: str,
        trace_id: str,
        account: PinterestAccount,
        board: PinterestBoard,
        asset: PinterestAsset,
        pin: PinterestPin,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "trace_id": trace_id,
            "account": {
                "id": account.id,
                "username": account.username,
            },
            "pin": {
                "id": pin.id,
                "external_id": pin.external_id,
                "title": pin.title,
                "description": pin.description,
            },
            "board": {
                "id": board.id,
                "key": board.key,
                "name": board.name,
                "description": board.description,
                "visibility": board.visibility,
            },
            "media": {
                "asset_id": asset.id,
                "filename": asset.original_file,
                "phone_storage_path": asset.phone_storage_path,
                "mime_type": asset.mime_type,
                "media_hash": asset.media_hash,
            },
            "policy": {
                "profile_only": True,
                "fill_destination": False,
            },
        }

    @staticmethod
    def _pinterest_result_success(result: dict[str, Any]) -> bool:
        if result.get("success") is False:
            return False
        if result.get("success") is True:
            return True
        status_value = str(result.get("status") or result.get("result") or "").lower()
        return status_value in {"ok", "success", "posted", "active", "created"}

    @staticmethod
    def _pinterest_result_code(result: dict[str, Any]) -> str:
        return str(result.get("status") or result.get("result") or "success")

    @staticmethod
    def _pinterest_error_code(result: dict[str, Any]) -> str:
        return str(result.get("error_code") or result.get("code") or "device_rejected")

    @staticmethod
    def _pinterest_error_message(result: dict[str, Any]) -> str:
        return str(result.get("error_message") or result.get("error") or result.get("message") or "Pinterest task failed")

    # ------------------------------------------------------------------
    # Job: process_pending_videos (every ~30s)
    # ------------------------------------------------------------------

    async def process_pending_videos(self) -> None:
        """Find due pending videos and push them to online devices.

        Centralized queue: only dispatch when scheduled_time <= now.
        One video per device per cycle to avoid flooding the phone.

        Accounts that became paused or blocked between scheduling and
        dispatch are filtered out here so a stale schedule can't cause
        posts to land on an account Instagram is already blocking.
        """
        now = _utcnow()

        async with self.session_factory() as session:
            # Videos that are due NOW (not in advance) AND whose account
            # is still in a postable state. `blocked_until` that has
            # already elapsed is treated as "no longer blocked" so the
            # queue resumes automatically after a temporary action-block
            # window expires even if the unblock job hasn't yet cleared
            # Account.is_blocked.
            stmt = (
                select(Video)
                .join(Account, Account.username == Video.account_username)
                .where(
                    Video.status == "pending",
                    Video.content_type.in_(["reel", "carousel"]),
                    Video.scheduled_time.isnot(None),
                    Video.scheduled_time <= now,
                    Account.is_active.is_(True),
                    Account.is_paused.is_(False),
                    or_(
                        Account.is_blocked.is_(False),
                        and_(
                            Account.blocked_until.isnot(None),
                            Account.blocked_until <= now,
                        ),
                    ),
                )
                .order_by(Video.scheduled_time.asc())
            )
            result = await session.execute(stmt)
            videos = result.scalars().all()

            if not videos:
                return

            # One video per device per cycle
            dispatched_devices: set[int] = set()
            dispatched_count = 0
            for video in videos:
                # Commit the previous iteration's ORM mutations before we do
                # any more SELECTs. Otherwise pending status/device updates
                # from the last dispatch can autoflush into this video's long
                # ghost phase and keep SQLite's writer lock alive longer than
                # intended.
                await session.commit()
                device_id = await self._resolve_device_id(session, video.account_username)
                if device_id is None or device_id in dispatched_devices:
                    continue
                if not self.ws_manager.is_online(device_id):
                    continue
                # Skip if device already has a video in uploading/posting state
                busy = (await session.execute(
                    select(func.count()).select_from(Video).where(
                        Video.device_id == device_id,
                        Video.status.in_(["uploading", "scheduled", "posting"]),
                    )
                )).scalar() or 0
                if busy > 0:
                    continue

                # Hardening step 6: enforce behavioral rate limits per account.
                # Three checks (all fail-open — log + skip, don't error out):
                #  1. min inter-post gap — no post within N seconds of the
                #     previous successful post for this account
                #  2. 3h burst cap — no more than N posted in the last 3h
                #  3. daily cap — already enforced elsewhere but double-check
                # Pass device_id so exempt test rigs (config
                # farm_rate_limit_exempt_devices) bypass the whole check.
                limit_reason = await self._check_rate_limits(
                    session, video.account_username, now, device_id=device_id,
                )
                if limit_reason is not None:
                    logger.info(
                        "[POSTING] Skip %d (@%s): %s — will retry next cycle",
                        video.id, video.account_username, limit_reason,
                    )
                    continue

                try:
                    result = await self._dispatch_video(session, video)
                    if result == "dispatched":
                        dispatched_devices.add(device_id)
                        dispatched_count += 1
                    # "deferred" and "failed" — device slot NOT consumed, next
                    # eligible video gets a shot in the same tick.
                except Exception:
                    logger.exception(
                        "Failed to dispatch video %d (%s)", video.id, video.filename,
                    )

            await session.commit()

        if dispatched_count > 0:
            await self.broadcaster.broadcast("queue:update", {
                "dispatched": dispatched_count,
                "ts": int(time.time() * 1000),
            })

    async def _dispatch_video(self, session: AsyncSession, video: Video) -> str:
        """Push a single video to its account's device via WS bridge.

        Two-phase dispatch:
        1. If the video is not yet on the phone, send a ``video.download``
           command with a signed download URL. The phone downloads via HTTPS
           and reports back with ``video.download_complete``.  The video status
           is set to ``"uploading"`` and we return — the schedule will be sent
           when the download_complete event arrives.
        2. If the video is already on the phone (``uploaded_to_phone=True``),
           send ``cmd.send_schedule`` directly.

        ``scheduledTimeMs`` is epoch milliseconds (Long), not an ISO string.

        Returns
        -------
        str
            One of ``"dispatched"`` (wire command sent OK, caller should mark
            the device slot consumed), ``"deferred"`` (no wire command sent,
            e.g. sibling gap hit — caller should leave the slot free for the
            next eligible video), or ``"failed"`` (wire command attempted but
            errored, or a precondition like missing device/offline prevented
            dispatch).
        """
        if video.content_type == "story":
            logger.warning(
                "[POSTING] Story video %d for @%s is not dispatchable: "
                "Android story posting is not implemented",
                video.id, video.account_username,
            )
            return "deferred"

        # Find the device for this video's account
        device_id = await self._resolve_device_id(session, video.account_username)
        if device_id is None:
            logger.warning(
                "No device linked to account %s — skipping video %d",
                video.account_username, video.id,
            )
            return "failed"

        if not self.ws_manager.is_online(device_id):
            logger.debug(
                "Device %d offline — skipping video %d for %s",
                device_id, video.id, video.account_username,
            )
            return "failed"

        # Sibling gap check (T1): reject dispatches of the same photo_set_id
        # on the same device if a previous dispatch landed inside the gap
        # window. The gap is defined on dispatched_at (real wire event),
        # NOT used_at (generation time).
        if (
            video.content_type == "carousel"
            and not video.uploaded_to_phone
            and device_id not in self.config.farm_rate_limit_exempt_devices
            and self.config.farm_min_photoset_reuse_gap_minutes > 0
        ):
            gap_minutes = self.config.farm_min_photoset_reuse_gap_minutes
            cutoff = _utcnow() - timedelta(minutes=gap_minutes)
            # Find this video's photo_set_id via its existing PhotoSetUsage row
            usage_for_set = (await session.execute(
                select(PhotoSetUsage.set_id).where(PhotoSetUsage.video_id == video.id)
            )).scalar_one_or_none()
            if usage_for_set is not None:
                # Most recent dispatched_at for the same (set_id, device_id) inside the gap window
                recent_dispatch = (await session.execute(
                    select(func.max(PhotoSetUsage.dispatched_at)).where(
                        PhotoSetUsage.set_id == usage_for_set,
                        PhotoSetUsage.device_id == device_id,
                        PhotoSetUsage.dispatched_at.is_not(None),
                        PhotoSetUsage.dispatched_at >= cutoff,
                    )
                )).scalar_one_or_none()
                if recent_dispatch is not None:
                    logger.info(
                        "[POSTING] Deferring video %d @%s on device %d — photo set %d "
                        "was reused at %s, under %d-min gap",
                        video.id, video.account_username, device_id,
                        usage_for_set, recent_dispatch.isoformat(), gap_minutes,
                    )
                    return "deferred"

        # Notify admin before starting — but only on the *posting* phase
        # (uploaded_to_phone=True), not the upstream download phase. The
        # dispatch pipeline calls _dispatch_video twice per video: first
        # to ghost+download to the phone, then to send the schedule. Both
        # tries enter this function and would each fire the alert if we
        # don't gate it. The user only cares "post is starting now" =
        # the schedule phase, since the download phase happens minutes
        # earlier and is invisible from the phone UI.
        if self.telegram_bot is not None and video.uploaded_to_phone:
            try:
                device_name = "?"
                dev = (await session.execute(
                    select(Device).where(Device.id == device_id)
                )).scalar_one_or_none()
                if dev:
                    device_name = dev.name or str(device_id)
                await self.telegram_bot.send_notification(
                    f"📤 Posting @{video.account_username}\n"
                    f"Device: {device_name}\n"
                    f"Video: {video.filename}",
                )
            except Exception:
                pass  # non-critical

        # Phase 1: Video not yet on phone — ghost + send download command
        if not video.uploaded_to_phone:
            carousel_set_uuid: str | None = None
            carousel_exclude_set: set[tuple[str, int]] = set()
            if self.config.ghost_enabled and video.content_type == "carousel":
                usage = (await session.execute(
                    select(PhotoSetUsage).where(PhotoSetUsage.video_id == video.id)
                )).scalar_one_or_none()
                if usage is not None:
                    photo_set = (await session.execute(
                        select(PhotoSet).where(PhotoSet.id == usage.set_id)
                    )).scalar_one_or_none()
                    if photo_set is not None:
                        carousel_set_uuid = photo_set.uuid
                if device_id is not None:
                    _cutoff = _utcnow() - timedelta(hours=72)
                    _recent = (await session.execute(
                        select(
                            DonorUsage.donor_model,
                            DonorUsage.donor_index,
                        ).where(
                            DonorUsage.device_id == device_id,
                            DonorUsage.used_at >= _cutoff,
                        )
                    )).all()
                    carousel_exclude_set = {
                        (row.donor_model, row.donor_index) for row in _recent
                    }

            # Release the read transaction opened by the pre-flight account /
            # device / sibling-gap queries before the long ghost+ffmpeg path.
            # Prefetch carousel ghost inputs above so we do not immediately
            # autobegin a new SQLite transaction and hold it open while ffmpeg
            # runs. The session factory uses expire_on_commit=False, so the
            # loaded Video row stays usable after this boundary.
            await session.commit()

            # Tracks which iPhone donor row was picked for this dispatch.
            # Sentinel ("", -1) means "no donor recorded" — either ghost
            # was disabled, content was reel/story (no anti-collision
            # yet), or the donor pool was empty at ghost time. Written
            # into donor_usage AFTER bridge.send_* acks success, so a
            # failed dispatch does not "spend" a donor.
            picked_donor: tuple[str, int] = ("", -1)

            # Ghost media (unique fingerprint per posting) — only on first dispatch.
            # ghost_media_safe runs ffmpeg synchronously and would block the async
            # event loop for 2+ minutes per video, so we run it in a thread.
            if self.config.ghost_enabled:
                from server.ghost import ghost_media_safe
                ghost_timeout_seconds = 420
                ghost_fail_open_inputs: set[str] = set()
                ghost_allowed_suffixes = (
                    ".mp4", ".mov", ".m4v", ".jpg", ".jpeg", ".png", ".webp",
                )
                ghost_min_size_bytes = 100 * 1024
                ghost_max_size_bytes = 1024 * 1024 * 1024

                def _mark_ghost_failed(reason: str) -> None:
                    video.status = "failed"
                    video.post_result = "ghost_failed"
                    video.post_error = reason
                    video.retry_count = (video.retry_count or 0) + 1

                def _ghost_key(input_file: Path) -> str:
                    try:
                        return str(input_file.resolve())
                    except OSError:
                        return str(input_file)

                def _ghost_consume_fail_open(input_file: Path) -> bool:
                    key = _ghost_key(input_file)
                    if key in ghost_fail_open_inputs:
                        ghost_fail_open_inputs.discard(key)
                        return True
                    return False

                def _ghost_input_ok(input_file: Path) -> bool:
                    key = _ghost_key(input_file)
                    ghost_fail_open_inputs.discard(key)

                    suffix = input_file.suffix.lower()
                    if suffix not in ghost_allowed_suffixes:
                        ghost_fail_open_inputs.add(key)
                        logger.warning(
                            "[POSTING] Ghost pre-flight rejected %s: unsupported extension %s; "
                            "not dispatching original",
                            input_file,
                            suffix or "<none>",
                        )
                        return False

                    try:
                        size_bytes = input_file.stat().st_size
                    except OSError as exc:
                        ghost_fail_open_inputs.add(key)
                        logger.warning(
                            "[POSTING] Ghost pre-flight rejected %s: stat failed (%s); "
                            "not dispatching original",
                            input_file,
                            exc,
                        )
                        return False

                    if size_bytes < ghost_min_size_bytes or size_bytes > ghost_max_size_bytes:
                        ghost_fail_open_inputs.add(key)
                        logger.warning(
                            "[POSTING] Ghost pre-flight rejected %s: size=%d bytes outside [%d, %d]; "
                            "not dispatching original",
                            input_file,
                            size_bytes,
                            ghost_min_size_bytes,
                            ghost_max_size_bytes,
                        )
                        return False

                    return True

                async def _ghost_with_timeout(*ghost_args: Any) -> Path | None:
                    input_file = Path(str(ghost_args[0]))
                    if not _ghost_input_ok(input_file):
                        return None
                    try:
                        return await asyncio.wait_for(
                            asyncio.to_thread(ghost_media_safe, *ghost_args),
                            timeout=ghost_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        ghost_fail_open_inputs.add(_ghost_key(input_file))
                        logger.warning(
                            "[POSTING] Ghost timed out after %ds for %s; "
                            "not dispatching original",
                            ghost_timeout_seconds,
                            input_file,
                        )
                        return None
                    except asyncio.CancelledError:
                        ghost_fail_open_inputs.add(_ghost_key(input_file))
                        logger.warning(
                            "[POSTING] Ghost cancelled for %s; not dispatching original",
                            input_file,
                        )
                        return None

                if video.content_type == "reel":
                    video_path = Path(self.config.data_dir) / "videos" / video.account_username / video.filename
                    if not video_path.exists():
                        reason = f"ghost_input_missing:{video_path}"
                        logger.error("[POSTING] %s", reason)
                        _mark_ghost_failed(reason)
                        return "failed"
                    ghosted = await _ghost_with_timeout(str(video_path))
                    if ghosted:
                        logger.info("[POSTING] Ghosted reel %s for @%s", video.filename, video.account_username)
                    else:
                        reason = f"ghost_failed:{video.filename}"
                        logger.error("[POSTING] Ghost failed for %s; not dispatching original", video.filename)
                        _mark_ghost_failed(reason)
                        return "failed"

                elif video.content_type == "carousel":
                    # Ghost each carousel photo into a per-dispatch dir.
                    #
                    # The ghost pipeline FORCES JPEG output regardless of source
                    # extension because a PNG file with forged iPhone EXIF is a
                    # logical contradiction (Codex 2026-04-13 review). We rename
                    # each image to .jpg in the dispatch dir and update
                    # video.image_filenames so downstream download URLs and the
                    # CarouselDownloadManager on the phone pick up the correct
                    # filenames. The original carousel_sets/*.png files are
                    # left untouched — they are the master copy.
                    if carousel_set_uuid is not None:
                        set_dir = Path(self.config.data_dir) / "carousel_sets" / carousel_set_uuid
                        dispatch_dir = Path(self.config.data_dir) / "carousel_dispatch" / str(video.id)
                        dispatch_dir.mkdir(parents=True, exist_ok=True)
                        images = json.loads(video.image_filenames or "[]")
                        ghosted_count = 0

                        # Single iPhone donor for all photos in this
                        # carousel — real iPhone carousels are one device,
                        # not 4 different iPhones across the slides.
                        #
                        # Anti-collision LRU (Option E): load recently-
                        # used donors for this device within the 72h
                        # retention window, then rejection-sample up
                        # to 8 shuffled seed permutations looking for
                        # one whose predicted donor is NOT in the
                        # exclude set. Shuffle seed mixes video.id +
                        # account_username so two siblings with the
                        # same exclude_set still explore attempts in
                        # different orders (defensive per Codex Q5).
                        # Deterministic per (video, account) → retries
                        # walk the same path.
                        exclude_set = set(carousel_exclude_set)

                        from ghostcli.profiles import peek_donor_pick
                        _shuffle_material = f"{video.id}:{video.account_username}".encode("utf-8")
                        _shuffle_seed = int.from_bytes(
                            hashlib.blake2b(_shuffle_material, digest_size=8).digest(),
                            "big",
                        ) & 0x7FFFFFFF
                        _attempts = list(range(8))
                        random.Random(_shuffle_seed).shuffle(_attempts)

                        carousel_meta_seed: int | None = None
                        for _attempt_idx in _attempts:
                            _material = f"{video.id}:{video.account_username}:{_attempt_idx}".encode("utf-8")
                            _trial_seed = int.from_bytes(
                                hashlib.blake2b(_material, digest_size=8).digest(),
                                "big",
                            ) & 0x7FFFFFFF
                            _trial_donor = peek_donor_pick(_trial_seed)
                            if carousel_meta_seed is None:
                                # First shuffled trial — unconditional
                                # fallback if every attempt collides.
                                carousel_meta_seed = _trial_seed
                                picked_donor = _trial_donor
                            if _trial_donor not in exclude_set:
                                carousel_meta_seed = _trial_seed
                                picked_donor = _trial_donor
                                break
                        else:
                            logger.warning(
                                "[POSTING] Donor exclusion exhausted for video %d @%s on device %s "
                                "(all %d seed attempts collided); accepting collision",
                                video.id, video.account_username, device_id, len(_attempts),
                            )

                        fallback_count = 0

                        async def _ghost_one(img_name: str) -> tuple[str, str]:
                            """Ghost one photo. Returns (new_filename, status).

                            Status is one of:
                              - "ghost":    full ghost pipeline succeeded
                              - "skip":     fail-open path kept the original
                                            shared asset untouched
                              - "fallback": ghost timed out or failed,
                                            but minimal Pillow re-encode
                                            produced a unique-hash JPEG
                              - "fail":     no output written, phone will
                                            fall back to the shared
                                            carousel_sets master

                            Output is always .jpg regardless of source
                            extension — carousel_dispatch is JPEG-only.
                            All photos in the same carousel share one
                            iPhone donor (Make/Model/Software/Lens) via
                            carousel_meta_seed.
                            """
                            src = set_dir / img_name
                            if not src.exists():
                                return img_name, "fail"
                            new_name = Path(img_name).stem + ".jpg"
                            dst = dispatch_dir / new_name
                            result = await _ghost_with_timeout(
                                str(src), str(dst), 0.90, carousel_meta_seed,
                            )
                            if result is not None:
                                return new_name, "ghost"
                            if _ghost_consume_fail_open(src):
                                return img_name, "skip"

                            # Fallback: minimal re-encode so two accounts
                            # that both hit a ghost timeout don't both
                            # end up serving the byte-identical master
                            # from carousel_sets/. Per-file seed mixes
                            # the carousel seed with the filename so
                            # each slide gets its own quality band.
                            from server.ghost import minimal_carousel_reencode
                            file_seed = (
                                carousel_meta_seed ^ (hash(img_name) & 0x7FFFFFFF)
                            ) & 0x7FFFFFFF
                            fallback = await asyncio.to_thread(
                                minimal_carousel_reencode,
                                str(src), str(dst), file_seed,
                            )
                            if fallback is not None:
                                return new_name, "fallback"
                            return img_name, "fail"

                        # Ghost carousel photos in parallel (bounded by ghost_workers)
                        workers = max(1, int(getattr(self.config, "ghost_workers", 4) or 4))
                        new_image_filenames: list[str] = []
                        skipped_count = 0
                        for start in range(0, len(images), workers):
                            batch = images[start : start + workers]
                            results = await asyncio.gather(*(_ghost_one(img) for img in batch))
                            for new_name, status in results:
                                if status == "ghost":
                                    ghosted_count += 1
                                elif status == "skip":
                                    skipped_count += 1
                                elif status == "fallback":
                                    fallback_count += 1
                                new_image_filenames.append(new_name)

                        # Update video.image_filenames with the .jpg names
                        # so the signed URL builder below and the phone's
                        # CarouselDownloadManager both pick up the new names.
                        video.image_filenames = json.dumps(new_image_filenames)

                        logger.info(
                            "[POSTING] Ghosted %d (+%d reencode-fallback, %d fail-open) / %d "
                            "carousel photos for video %d @%s (PNG→JPG)",
                            ghosted_count, fallback_count, skipped_count, len(images),
                            video.id, video.account_username,
                        )

                elif video.content_type == "story":
                    video_path = Path(self.config.data_dir) / "stories" / video.account_username / video.filename
                    if not video_path.exists():
                        video_path = Path(self.config.data_dir) / "videos" / video.account_username / video.filename
                    if not video_path.exists():
                        reason = f"ghost_input_missing:{video_path}"
                        logger.error("[POSTING] %s", reason)
                        _mark_ghost_failed(reason)
                        return "failed"
                    ghosted = await _ghost_with_timeout(str(video_path))
                    if ghosted:
                        logger.info("[POSTING] Ghosted story %s for @%s", video.filename, video.account_username)
                    else:
                        reason = f"ghost_failed:{video.filename}"
                        logger.error("[POSTING] Ghost failed for story %s; not dispatching original", video.filename)
                        _mark_ghost_failed(reason)
                        return "failed"

            # Delay the first ORM mutations until ghosting finishes so
            # SQLAlchemy autoflush does not hold SQLite's writer lock
            # across the long-running ffmpeg phase.
            video.device_id = device_id
            stamped_caption = self._stamp_video_caption(video)

            # Carousel: batch download all photos via single carousel_download command
            if video.content_type == "carousel":
                images = json.loads(video.image_filenames or "[]")
                if not images:
                    logger.error("Carousel video %d has no images", video.id)
                    return "failed"
                # Build signed URL for each carousel asset (reuses shared helper)
                assets = [
                    {
                        "url": generate_carousel_asset_url(video.id, idx, self.config),
                        "filename": img_name,
                    }
                    for idx, img_name in enumerate(images)
                ]

                try:
                    await self.bridge.send_carousel_download(
                        device_id,
                        video_id=video.id,
                        username=video.account_username,
                        assets=assets,
                    )
                    self._last_dispatch_ms[device_id] = int(time.time() * 1000)
                except Exception as exc:
                    logger.error(
                        "Bridge send_carousel_download failed for video %d: %s",
                        video.id, exc,
                    )
                    return "failed"

                # Stamp the dispatched_at clock on the existing PhotoSetUsage row
                # (created at generation time in _maybe_generate_carousel). This is
                # the real "photo set left the VPS" event.
                usage_row = (await session.execute(
                    select(PhotoSetUsage).where(PhotoSetUsage.video_id == video.id)
                )).scalar_one_or_none()
                if usage_row is not None:
                    usage_row.device_id = device_id
                    usage_row.dispatched_at = _utcnow()

                # Bridge ack received → donor is "spent". Record it in
                # donor_usage so future sibling posts on this device skip
                # this (model_key, row_index) pair. Deferred to AFTER the
                # bridge send so a failed dispatch does not commit a row
                # for a never-delivered post (Codex Q4, Plan-agent Q4).
                # Sentinel ("", -1) is the disaster-recovery path where
                # the donor pool was empty — pick_donor_row falls back to
                # _FALLBACK_PROFILES[0] and there is no real donor to
                # record.
                if picked_donor[1] >= 0:
                    session.add(DonorUsage(
                        device_id=device_id,
                        donor_model=picked_donor[0],
                        donor_index=picked_donor[1],
                    ))

                video.status = "uploading"
                logger.info(
                    "[POSTING] Sent carousel download command for video %d (%d assets) to device %d for %s",
                    video.id, len(assets), device_id, video.account_username,
                )
                return "dispatched"

            # Reel/story: single file download
            download_url = generate_download_url(video.id, self.config)
            try:
                await self.bridge.send_video_download(
                    device_id,
                    url=download_url,
                    username=video.account_username,
                    filename=video.filename,
                    video_id=video.id,
                )
                self._last_dispatch_ms[device_id] = int(time.time() * 1000)
            except Exception as exc:
                logger.error(
                    "Bridge send_video_download failed for video %d on device %d: %s",
                    video.id, device_id, exc,
                )
                return "failed"

            # Stamp StoryAssetUsage.dispatched_at (story dispatcher 2026-04-14).
            # Mirrors PhotoSetUsage stamping at the carousel branch above
            # (line ~805). The sibling-gap recent_device CTE prefers
            # dispatched_at over used_at via COALESCE, so accurate
            # stamping here tightens the cross-account dedup window.
            if video.content_type == "story":
                sau_row = (await session.execute(
                    select(StoryAssetUsage).where(
                        StoryAssetUsage.video_id == video.id,
                    )
                )).scalar_one_or_none()
                if sau_row is not None:
                    sau_row.dispatched_at = _utcnow()

            video.status = "uploading"
            logger.info(
                "[POSTING] Sent download command for video %d (%s) to device %d for %s",
                video.id, video.filename, device_id, video.account_username,
            )
            return "dispatched"

        # Phase 2: Video already on phone — tell phone to post NOW
        # scheduledTimeMs=0 means "post immediately" — VPS controls timing,
        # phone has no local queue or scheduling.
        video.device_id = device_id
        stamped_caption = self._stamp_video_caption(video)
        scheduled_ms: int = 0

        payload = {
            "accounts": [
                {
                    "username": video.account_username,
                    "videos": [
                        {
                            "filename": video.filename,
                            "scheduledTimeMs": scheduled_ms,
                            "caption": stamped_caption,
                            "firstComment": video.first_comment or "",
                            "contentType": video.content_type or "reel",
                            "images": json.loads(video.image_filenames) if video.image_filenames else [],
                            "storyElement": json.loads(video.story_element) if video.story_element else None,
                        },
                    ],
                },
            ],
        }

        try:
            result = await self.bridge.send_schedule(device_id, payload)
            self._last_dispatch_ms[device_id] = int(time.time() * 1000)
        except Exception as exc:
            logger.error(
                "Bridge send_schedule failed for video %d on device %d: %s",
                video.id, device_id, exc,
            )
            return "failed"

        # Store phone-side video ID for result matching
        video_ids = result.get("videoIds", [])
        for vid_info in video_ids:
            if vid_info.get("filename") == video.filename:
                video.phone_video_id = vid_info.get("phoneVideoId")
                logger.info(
                    "[POSTING] Mapped VPS video %d → phone video %d",
                    video.id, video.phone_video_id,
                )
                break

        video.status = "scheduled"
        logger.info(
            "[POSTING] Dispatched video %d (%s) to device %d for %s",
            video.id, video.filename, device_id, video.account_username,
        )
        return "dispatched"

    def _build_public_url(self) -> str:
        """Public URL that phones can reach. IP → http://:8443, domain → https://."""
        if not self.config.domain:
            return ""
        import re
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", self.config.domain):
            return f"http://{self.config.domain}:8443"
        return f"https://{self.config.domain}"

    async def prune_farm_logs(self) -> None:
        """Daily retention prune for the farm_logs table.

        Advanced logging step 4 (Codex). DEBUG entries older than 7 days
        and INFO entries older than 30 days are deleted; WARNING/ERROR
        kept indefinitely. Runs once per day via APScheduler.
        """
        from server.log_store import prune_old_logs
        try:
            async with self.session_factory() as session:
                stats = await prune_old_logs(session)
            if any(stats.values()):
                logger.info(
                    "[LOGS] Retention prune: %s",
                    ", ".join(f"{k}={v}" for k, v in stats.items()),
                )
        except Exception:
            logger.exception("[LOGS] Retention prune failed")

    async def prune_donor_usage(self) -> None:
        """Drop donor_usage rows older than 72 hours.

        The anti-collision LRU (Option E) only cares about donors used
        in the recent past — a stale row older than 72h has no forensic
        value and cluttering the table hurts the main SELECT during
        dispatch. Runs once per day via APScheduler.
        """
        from sqlalchemy import delete
        try:
            cutoff = _utcnow() - timedelta(hours=72)
            async with self.session_factory() as session:
                result = await session.execute(
                    delete(DonorUsage)
                    .where(DonorUsage.used_at < cutoff)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                if result.rowcount:
                    logger.info(
                        "[POSTING] Pruned %d stale donor_usage rows", result.rowcount,
                    )
        except Exception:
            logger.exception("[POSTING] donor_usage prune failed")

    async def _resolve_device_id(
        self, session: AsyncSession, account_username: str,
    ) -> int | None:
        """Return the primary device ID for an active account, or None."""
        stmt = (
            select(AccountDevice.device_id)
            .join(Account, Account.username == AccountDevice.account_username)
            .join(Device, Device.id == AccountDevice.device_id)
            .where(AccountDevice.account_username == account_username)
            .where(Account.is_active.is_(True))
            .where(Device.is_active.is_(True))
            .order_by(AccountDevice.is_primary.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        return row

    async def _check_rate_limits(
        self,
        session: AsyncSession,
        account_username: str,
        now: datetime,
        device_id: int | None = None,
    ) -> str | None:
        """Check behavioral rate limits for an account.

        Hardening step 6: returns None if the account is eligible to post
        right now, or a short human-readable reason string if it should be
        skipped. The scheduler logs the reason and moves on — the video
        stays pending and is reconsidered on the next tick, so as soon as
        the window passes the post goes out.

        Three limits, all evaluated against `posted_at` (only successful
        posts count — failures don't consume rate budget):

          1. Min inter-post gap: last successful post must be at least
             `farm_min_inter_post_gap_seconds` ago.
          2. 3h burst cap: no more than `farm_max_posts_per_account_per_3h`
             posts in the trailing 3 hours.
          3. Daily cap: no more than `farm_max_posts_per_account_per_day`
             posts in the trailing 24 hours (duplicates the existing
             daily-slot logic but enforces it at dispatch time too).

        All three are configurable per-account via the Account row if we
        later add override columns — right now they use the global config.

        Exempt devices: if `device_id` is in
        `config.farm_rate_limit_exempt_devices` the three checks are
        skipped entirely and an audit INFO line is emitted. Intended
        for E2E test rigs only. `None in [...]` is `False` in Python,
        so the safety check is intrinsically None-safe.
        """
        exempt = self.config.farm_rate_limit_exempt_devices or []
        if device_id is not None and device_id in exempt:
            logger.info(
                "[RATE_LIMIT] device_id=%s exempt, skipping all rate checks for @%s",
                device_id, account_username,
            )
            return None

        min_gap_s = int(self.config.farm_min_inter_post_gap_seconds)
        max_3h = int(self.config.farm_max_posts_per_account_per_3h)

        # Per-account daily cap override: when the operator sets
        # `accounts.max_posts_per_day` via the UI it MUST win over the
        # global default. Without this lookup the scheduler was clamping
        # every account at `farm_max_posts_per_account_per_day` (default
        # 5) even when the admin had explicitly raised the cap — accounts
        # stuck at "24h daily cap (16/5)" even with per-account set to 24.
        acct_cap = (await session.execute(
            select(Account.max_posts_per_day).where(
                Account.username == account_username,
            )
        )).scalar_one_or_none()
        max_day = int(
            acct_cap
            if acct_cap is not None
            else self.config.farm_max_posts_per_account_per_day
        )

        three_h_ago = now - timedelta(hours=3)
        twenty_four_h_ago = now - timedelta(hours=24)
        min_gap_cutoff = now - timedelta(seconds=min_gap_s)

        # Most recent successful post for this account
        last_posted_at = (await session.execute(
            select(func.max(Video.posted_at)).where(
                Video.account_username == account_username,
                Video.status == "posted",
            )
        )).scalar()

        if last_posted_at is not None:
            # Both sides of this comparison must be tz-naive UTC: `now` comes
            # from `_utcnow()` which is `farm_time.utcnow_naive()` (explicitly
            # naive), and `last_posted_at` is whatever SQLite handed back.
            # Some drivers preserve tzinfo on round-trip, so defensively strip
            # at the boundary. Per Codex review (Option C): normalise here,
            # not globally, to keep the rest of the file on the naive
            # convention without mutating `last_posted_at` upstream.
            cmp_last = (
                last_posted_at.replace(tzinfo=None)
                if last_posted_at.tzinfo is not None
                else last_posted_at
            )
            if cmp_last > min_gap_cutoff:
                remaining = int(
                    (cmp_last + timedelta(seconds=min_gap_s) - now).total_seconds()
                )
                return f"min inter-post gap ({min_gap_s}s); wait {remaining}s"

        posts_3h = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account_username,
                Video.status == "posted",
                Video.posted_at >= three_h_ago,
            )
        )).scalar() or 0
        if posts_3h >= max_3h:
            return f"3h burst cap ({posts_3h}/{max_3h})"

        posts_24h = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account_username,
                Video.status == "posted",
                Video.posted_at >= twenty_four_h_ago,
            )
        )).scalar() or 0
        if posts_24h >= max_day:
            return f"24h daily cap ({posts_24h}/{max_day})"

        return None

    # ------------------------------------------------------------------
    # Silent-account cause classifier (Phase 1, 2026-04-24)
    # ------------------------------------------------------------------

    async def _classify_silent_cause(
        self,
        session: AsyncSession,
        account_username: str,
        now: datetime,
    ) -> tuple[str, str | None, dict]:
        """Classify why ``account_username`` didn't post in the last 3 h.

        Returns ``(cause, fix_action, payload)`` where:

        - ``cause`` is one of the :class:`SilentCause` constants.
        - ``fix_action`` is ``None`` for inform-only causes, or
          ``'clear_scheduled_time'`` to signal the caller should NULL out
          ``Video.scheduled_time`` on the most-overdue pending video so
          ``auto_schedule_videos`` re-slots it on the next 60 s tick.
        - ``payload`` is a dict of cause-specific evidence used for both
          the Telegram message and the ``watchdog:autofix`` WS event.

        Order of checks (cheapest+most-specific first, so any escalation
        is suppressed by a bigger upstream fault):

        1. Account-level boolean state (paused → blocked).
        2. Device offline (status / stale heartbeat).
        3. Rate limits (daily cap → 3h burst → min-inter-post gap).
        4. Queue: most-overdue pending video. None → empty queue.
           ``scheduled_time`` more than :data:`SILENT_STALE_THRESHOLD` in
           the past → stale schedule (auto-fixable).
        5. Fall-through → unknown (escalate).

        This method is read-mostly: no commits, no broadcasts. The only
        side effect is implicit through the queries it issues. The
        ``check_posting_stalls`` caller is responsible for the actual
        ``UPDATE`` and any WS broadcast.
        """
        # 1) Account-level state -------------------------------------------
        account_row = (await session.execute(
            select(
                Account.is_paused,
                Account.is_blocked,
                Account.blocked_until,
                Account.max_posts_per_day,
            ).where(Account.username == account_username)
        )).first()
        if account_row is not None:
            is_paused, is_blocked, blocked_until, acct_cap = account_row
            if is_paused:
                return (
                    SilentCause.ACCOUNT_PAUSED,
                    None,
                    {"account": account_username},
                )
            if is_blocked:
                # An expired blocked_until means the block window is over
                # and the account should resume — fall through to other
                # causes. Active block = blocked_until None or future.
                if blocked_until is None or blocked_until > now:
                    return (
                        SilentCause.ACCOUNT_BLOCKED,
                        None,
                        {
                            "account": account_username,
                            "blocked_until": (
                                blocked_until.isoformat()
                                if blocked_until is not None else None
                            ),
                        },
                    )

        # 2) Device offline -------------------------------------------------
        # An account can be linked to multiple devices; a single offline
        # device is enough to explain a 3h silence — phones are silo'd.
        # We ALSO look at last_seen_at against the configured silent
        # timeout so a device that's stuck "online" in the DB but
        # silent on the wire is correctly classified.
        silent_timeout_s = int(self.config.farm_device_silent_timeout_seconds)
        silent_cutoff = now - timedelta(seconds=silent_timeout_s)
        offline_row = (await session.execute(
            select(
                Device.id, Device.device_id, Device.name,
                Device.status, Device.last_seen_at,
            )
            .join(AccountDevice, AccountDevice.device_id == Device.id)
            .where(
                AccountDevice.account_username == account_username,
                or_(
                    Device.status == "offline",
                    Device.last_seen_at.is_(None),
                    Device.last_seen_at < silent_cutoff,
                ),
            )
            .limit(1)
        )).first()
        if offline_row is not None:
            return (
                SilentCause.DEVICE_OFFLINE,
                None,
                {
                    "account": account_username,
                    "device_id": offline_row[0],
                    "device_serial": offline_row[1],
                    "device_name": offline_row[2],
                    "status": offline_row[3],
                    "last_seen_at": (
                        offline_row[4].isoformat()
                        if offline_row[4] is not None else None
                    ),
                },
            )

        # 3) Rate limits ----------------------------------------------------
        # Daily cap (per-account override beats global default — same logic
        # as _check_rate_limits, mirrored here so we don't re-issue an
        # already-failed dispatch via subprocess).
        max_day = int(
            acct_cap if account_row is not None and acct_cap is not None
            else self.config.farm_max_posts_per_account_per_day
        )
        twenty_four_h_ago = now - timedelta(hours=24)
        posts_24h = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account_username,
                Video.status == "posted",
                Video.posted_at >= twenty_four_h_ago,
            )
        )).scalar() or 0
        if posts_24h >= max_day:
            return (
                SilentCause.DAILY_CAP,
                None,
                {
                    "account": account_username,
                    "posts_24h": int(posts_24h),
                    "limit": max_day,
                },
            )

        # 3h burst cap.
        three_h_ago = now - timedelta(hours=3)
        max_3h = int(self.config.farm_max_posts_per_account_per_3h)
        posts_3h = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account_username,
                Video.status == "posted",
                Video.posted_at >= three_h_ago,
            )
        )).scalar() or 0
        if posts_3h >= max_3h:
            return (
                SilentCause.RATE_LIMIT_3H,
                None,
                {
                    "account": account_username,
                    "posts_3h": int(posts_3h),
                    "limit": max_3h,
                },
            )

        # Min inter-post gap.
        min_gap_s = int(self.config.farm_min_inter_post_gap_seconds)
        last_posted_at = (await session.execute(
            select(func.max(Video.posted_at)).where(
                Video.account_username == account_username,
                Video.status == "posted",
            )
        )).scalar()
        if last_posted_at is not None:
            cmp_last = (
                last_posted_at.replace(tzinfo=None)
                if last_posted_at.tzinfo is not None else last_posted_at
            )
            min_gap_cutoff = now - timedelta(seconds=min_gap_s)
            if cmp_last > min_gap_cutoff:
                wait_s = int(
                    (cmp_last + timedelta(seconds=min_gap_s) - now).total_seconds()
                )
                return (
                    SilentCause.MIN_INTER_POST_GAP,
                    None,
                    {
                        "account": account_username,
                        "wait_seconds": max(0, wait_s),
                        "gap_seconds": min_gap_s,
                    },
                )

        # 4) Queue ----------------------------------------------------------
        most_overdue_row = (await session.execute(
            select(Video.id, Video.scheduled_time, Video.filename)
            .where(
                Video.account_username == account_username,
                Video.status == "pending",
            )
            .order_by(Video.scheduled_time.asc().nullslast())
            .limit(1)
        )).first()

        if most_overdue_row is None:
            # No pending row at all: the queue truly is empty for this
            # account, which is a normal "ran out of content" state.
            return (
                SilentCause.EMPTY_QUEUE,
                None,
                {"account": account_username, "pending_count": 0},
            )

        video_id, scheduled_time, filename = most_overdue_row
        if (
            scheduled_time is not None
            and scheduled_time < now - SILENT_STALE_THRESHOLD
        ):
            overdue_minutes = int(
                (now - scheduled_time).total_seconds() / 60.0
            )
            return (
                SilentCause.STALE_SCHEDULE,
                "clear_scheduled_time",
                {
                    "account": account_username,
                    "video_id": video_id,
                    "filename": filename,
                    "scheduled_time": scheduled_time.isoformat(),
                    "overdue_minutes": overdue_minutes,
                },
            )

        # 5) Fall-through ---------------------------------------------------
        return (
            SilentCause.UNKNOWN,
            None,
            {
                "account": account_username,
                "pending_video_id": video_id,
                "pending_scheduled_time": (
                    scheduled_time.isoformat()
                    if scheduled_time is not None else None
                ),
            },
        )

    # ------------------------------------------------------------------
    # Event: video.download_complete (from phone)
    # ------------------------------------------------------------------

    async def handle_download_complete(
        self,
        device_id: int,
        payload: dict[str, Any],
    ) -> None:
        """Handle a ``video.download_complete`` event from a phone.

        Marks the video as uploaded to phone, then immediately dispatches
        the ``cmd.send_schedule`` command so there is no delay waiting for
        the next poll cycle.

        Parameters
        ----------
        device_id : int
            The device that completed the download.
        payload : dict
            Must contain ``videoId`` (int) and ``success`` (bool).
            May contain ``path`` (str) and ``error`` (str).
        """
        video_id = payload.get("videoId")
        filename = payload.get("filename")
        username = payload.get("username")
        success = payload.get("success", True)
        error = payload.get("error")

        async with self.session_factory() as session:
            # Find video by ID, or by filename+username, or by filename+status alone
            video = None
            if video_id is not None:
                video = (await session.execute(
                    select(Video).where(Video.id == video_id)
                )).scalar_one_or_none()

            if video is None and filename:
                # Strip @ prefix from username if present
                clean_username = username.lstrip("@") if username else None
                if clean_username:
                    video = (await session.execute(
                        select(Video).where(
                            Video.filename == filename,
                            Video.account_username == clean_username,
                            Video.status == "uploading",
                        )
                    )).scalar_one_or_none()

                # Fallback: match by filename + status alone
                if video is None:
                    video = (await session.execute(
                        select(Video).where(
                            Video.filename == filename,
                            Video.status == "uploading",
                        )
                    )).scalar_one_or_none()

            if video is None:
                logger.warning(
                    "video.download_complete: no matching video (id=%s filename=%s username=%s) from device %d",
                    video_id, filename, username, device_id,
                )
                return

            # Guard: skip if already processed (duplicate progress events)
            if video.uploaded_to_phone:
                logger.debug(
                    "video.download_complete: video %d already uploaded, ignoring duplicate",
                    video.id,
                )
                return

            if not success:
                video.status = "failed"
                video.upload_error = error or "Download failed on phone"
                logger.error(
                    "[POSTING] Video %d download failed on device %d: %s",
                    video_id, device_id, error,
                )
                await session.commit()

                # Broadcast failure
                await self.broadcaster.broadcast("queue:update", {
                    "video_id": video_id,
                    "status": "failed",
                    "error": error,
                    "ts": int(time.time() * 1000),
                })
                return

            # Mark as uploaded — schedule dispatch runs as a background task
            # to avoid deadlocking the WS read loop (send_command waits for
            # a response that can only arrive once handle_message returns).
            video.uploaded_to_phone = True
            resolved_video_id = video.id
            await session.commit()

            logger.info(
                "[POSTING] Video %d downloaded to device %d, scheduling dispatch",
                resolved_video_id, device_id,
            )

        # Spawn schedule dispatch outside the WS event handler so the
        # read loop can receive the cmd.send_schedule response.
        task = asyncio.create_task(
            self._background_dispatch(resolved_video_id, device_id),
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _background_dispatch(self, video_id: int, device_id: int) -> None:
        """Dispatch schedule for a video in a background task.

        Runs outside the WS read loop so ``send_command`` can receive the
        response without deadlocking.
        """
        async with self.session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == video_id)
            )).scalar_one_or_none()
            if video is None:
                logger.warning("Background dispatch: video %d not found", video_id)
                return

            try:
                await self._dispatch_video(session, video)
            except Exception:
                logger.exception(
                    "Background dispatch failed for video %d on device %d",
                    video_id, device_id,
                )
            await session.commit()

        await self.broadcaster.broadcast("queue:update", {
            "video_id": video_id,
            "status": video.status if video else "unknown",
            "ts": int(time.time() * 1000),
        })

    # ------------------------------------------------------------------
    # Event: event.carousel_ready (from phone)
    # ------------------------------------------------------------------

    async def handle_carousel_ready(
        self, device_id: int, payload: dict[str, Any],
    ) -> None:
        """Phone finished downloading all carousel assets.

        Marks the carousel video as uploaded_to_phone=True and kicks off
        dispatch (cmd.send_schedule) in a background task.
        """
        video_id = payload.get("videoId")
        success = payload.get("success", True)
        error = payload.get("error")
        downloaded_count = payload.get("downloadedCount", 0)
        total_count = payload.get("totalCount", 0)

        if not video_id:
            logger.warning("carousel_ready: missing videoId from device %d", device_id)
            return

        async with self.session_factory() as session:
            video = (await session.execute(
                select(Video).where(Video.id == video_id)
            )).scalar_one_or_none()

            if video is None:
                logger.warning(
                    "carousel_ready: video %d not found (device %d)",
                    video_id, device_id,
                )
                return

            if video.uploaded_to_phone:
                logger.debug(
                    "carousel_ready: video %d already uploaded, ignoring duplicate",
                    video_id,
                )
                return

            if not success:
                video.status = "failed"
                video.upload_error = error or f"Carousel download failed ({downloaded_count}/{total_count})"
                logger.error(
                    "[POSTING] Carousel %d download failed on device %d: %s",
                    video_id, device_id, error,
                )
                await session.commit()
                await self.broadcaster.broadcast("queue:update", {
                    "video_id": video_id,
                    "status": "failed",
                    "error": error,
                    "ts": int(time.time() * 1000),
                })
                return

            video.uploaded_to_phone = True
            await session.commit()
            logger.info(
                "[POSTING] Carousel %d assets downloaded on device %d (%d/%d), scheduling dispatch",
                video_id, device_id, downloaded_count, total_count,
            )

        # Dispatch in background task (same pattern as handle_download_complete)
        task = asyncio.create_task(
            self._background_dispatch(video_id, device_id),
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Job: collect_post_results (every ~15s)
    # ------------------------------------------------------------------

    async def collect_post_results(self) -> None:
        """Poll each online device for post results and update the database."""
        online_ids = self.ws_manager.get_online_device_ids()
        if not online_ids:
            return

        for device_id in online_ids:
            try:
                await self._collect_from_device(device_id)
            except Exception:
                logger.exception("Result collection failed for device %d", device_id)

    async def _collect_from_device(self, device_id: int) -> None:
        """Fetch post logs from a single device and upsert into DB."""
        if device_id in self._last_poll_ms:
            since_ms = self._last_poll_ms[device_id]
        else:
            since_ms = await self._initial_post_log_since_ms(device_id)
            self._last_poll_ms[device_id] = since_ms

        try:
            data = await self.bridge.get_post_logs(device_id, since_ms=since_ms)
        except Exception as exc:
            logger.warning("get_post_logs failed for device %d: %s", device_id, exc)
            return

        logs: list[dict[str, Any]] = data.get("logs", [])
        if not logs:
            return

        self._last_poll_ms[device_id] = max((int(entry.get("timestamp", 0) or 0) for entry in logs), default=since_ms)
        max_ts = since_ms
        async with self.session_factory() as session:
            for entry in logs:
                await self._upsert_post_log(session, device_id, entry)
                entry_ts = entry.get("timestamp", 0)
                if entry_ts > max_ts:
                    max_ts = entry_ts

            await session.commit()

        self._last_poll_ms[device_id] = max_ts

    async def _initial_post_log_since_ms(self, device_id: int) -> int:
        """Return the persisted poll offset for a device after scheduler restart."""
        async with self.session_factory() as session:
            latest = (await session.execute(
                select(func.max(PostLog.timestamp)).where(
                    PostLog.device_id == device_id,
                )
            )).scalar_one_or_none()
        if latest is None:
            return 0
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return int(latest.timestamp() * 1000)

    @staticmethod
    def _is_a11y_failure(error_msg: str | None) -> bool:
        """True iff `error_msg` looks like an a11y-binding-dead signal."""
        if not error_msg:
            return False
        low = error_msg.lower()
        return any(n in low for n in _A11Y_ERROR_NEEDLES)

    @staticmethod
    def _is_profile_visibility_failure(error_msg: str | None) -> bool:
        """True for Android's known false negative after an IG post lands."""
        if not error_msg:
            return False
        low = error_msg.lower()
        return any(n in low for n in _PROFILE_FALSE_FAILURE_NEEDLES)

    async def _mark_video_posted(
        self,
        session: AsyncSession,
        video: Video,
        posted_at: datetime,
    ) -> bool:
        """Mark a video posted and stamp dependent usage/insights rows.

        Returns True only when this call changed a non-posted video into
        a posted one. Callers use that to avoid double-counting account
        totals when a later duplicate phone log arrives.
        """
        was_posted = (
            video.status == "posted"
            and video.post_result == "success"
            and video.posted_at is not None
        )

        video.status = "posted"
        video.posted_at = video.posted_at or posted_at
        video.post_result = "success"
        video.post_error = None
        video.upload_error = None

        if was_posted:
            return False

        if video.content_type == "carousel":
            psu_row = (await session.execute(
                select(PhotoSetUsage).where(PhotoSetUsage.video_id == video.id)
            )).scalar_one_or_none()
            if psu_row is not None:
                psu_row.posted_at = posted_at
        elif video.content_type == "story":
            sau_row = (await session.execute(
                select(StoryAssetUsage).where(StoryAssetUsage.video_id == video.id)
            )).scalar_one_or_none()
            if sau_row is not None:
                sau_row.posted_at = posted_at

        interval_hours = self._pick_decay_bracket(0.0)
        await self._upsert_insights_plan(
            session,
            video,
            anchor_at=posted_at,
            next_due_at=posted_at + timedelta(hours=interval_hours),
            priority_score=1.0,
            reset_failures=True,
        )
        return True

    async def _ignore_failure_logs_for_confirmed_video(
        self,
        session: AsyncSession,
        video: Video,
    ) -> int:
        """Convert stored failures for a confirmed post into ignored rows."""
        rows = (await session.execute(
            select(PostLog).where(
                PostLog.video_id == video.id,
                PostLog.result.in_(["failed", "timeout", "action_blocked"]),
            )
        )).scalars().all()
        for row in rows:
            row.result = "ignored_failure"
        return len(rows)

    async def reconcile_profile_post_count(
        self,
        session: AsyncSession,
        *,
        account: Account,
        device_id: int,
        old_posts_count: int,
        new_posts_count: int,
    ) -> list[int]:
        """Use a profile post-count increase to rescue known false failures.

        Android sometimes posts successfully, then fails its own profile-grid
        verification with "post not visible on profile after 60s".  Only that
        already-failed case should be reconciled here.  A raw profile counter
        increase while a phone task is still scheduled/uploading/posting can
        come from an older post and must not emit an early success notification
        before Instagram's Share flow finishes.
        """
        if new_posts_count <= old_posts_count:
            return []

        now = _utcnow()
        delta = max(1, min(new_posts_count - old_posts_count, 3))
        window_start = now - timedelta(minutes=45)
        window_end = now + timedelta(minutes=10)
        profile_false_failure_filter = or_(*[
            PostLog.error_message.ilike(f"%{needle}%")
            for needle in _PROFILE_FALSE_FAILURE_NEEDLES
        ])
        failed_video_ids = (
            select(PostLog.video_id)
            .where(
                PostLog.video_id.is_not(None),
                PostLog.device_id == device_id,
                PostLog.account_username == account.username,
                PostLog.result.in_(["failed", "timeout", "action_blocked"]),
                profile_false_failure_filter,
            )
        )

        candidates = (await session.execute(
            select(Video)
            .where(
                Video.account_username == account.username,
                Video.device_id == device_id,
                Video.posted_at.is_(None),
                Video.content_type.in_(["reel", "carousel"]),
                Video.status == "failed",
                Video.post_result.in_(["failed", "timeout", "action_blocked"]),
                Video.id.in_(failed_video_ids),
                Video.scheduled_time.is_not(None),
                Video.scheduled_time >= window_start,
                Video.scheduled_time <= window_end,
            )
            .order_by(Video.scheduled_time.desc(), Video.id.desc())
            .limit(delta)
        )).scalars().all()

        marked: list[int] = []
        for video in candidates:
            posted_at = now
            previous_status = video.status
            changed = await self._mark_video_posted(session, video, posted_at)
            if not changed:
                continue

            ignored_failures = await self._ignore_failure_logs_for_confirmed_video(
                session,
                video,
            )
            account.total_posted = (account.total_posted or 0) + 1
            if ignored_failures:
                account.total_failed = max(
                    0,
                    (account.total_failed or 0) - ignored_failures,
                )
            account.last_posted_at = posted_at

            session.add(PostLog(
                video_id=video.id,
                device_id=device_id,
                account_username=account.username,
                result="success",
                error_message=None,
                duration_ms=None,
                phone_log_id=None,
            ))
            self._a11y_fail_counts.pop(device_id, None)
            self._a11y_alerted.discard(device_id)
            marked.append(video.id)
            logger.info(
                "[POSTING] Profile count confirmed @%s post: posts %d→%d, "
                "video %d (%s→posted), ignored_failures=%d",
                account.username,
                old_posts_count,
                new_posts_count,
                video.id,
                previous_status,
                ignored_failures,
            )
            await self._notify_post_success(account.username, video.filename, 0)

        return marked

    def _claim_post_notification_slot(self, username: str) -> bool:
        if not username or self.telegram_bot is None:
            return False
        now_ts = time.time()
        last_ts = self._last_notify_ts.get(username, 0.0)
        if (now_ts - last_ts) < self._notify_cooldown:
            return False
        self._last_notify_ts[username] = now_ts
        return True

    async def _notify_post_success(
        self,
        username: str,
        filename: str,
        duration_ms: int,
    ) -> None:
        if not self._claim_post_notification_slot(username):
            return
        try:
            await self.telegram_bot.notify_post_success(
                username,
                filename,
                duration_ms,
            )
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)

    async def _notify_post_failure(
        self,
        username: str,
        filename: str,
        error_msg: str,
    ) -> None:
        if not self._claim_post_notification_slot(username):
            return
        try:
            await self.telegram_bot.notify_post_failure(
                username,
                filename,
                error_msg,
            )
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)

    async def _resolve_video_for_phone_result(
        self,
        session: AsyncSession,
        *,
        device_id: int,
        phone_video_id: int,
        username: str,
        entry: dict[str, Any],
    ) -> Video | None:
        """Resolve a phone-side video id to one VPS row, tolerating phone DB resets."""
        candidates = (await session.execute(
            select(Video).where(
                Video.phone_video_id == phone_video_id,
                Video.device_id == device_id,
            )
        )).scalars().all()
        if not candidates:
            return None

        if username:
            username_matches = [
                video for video in candidates
                if video.account_username == username
            ]
            if username_matches:
                candidates = username_matches

        if len(candidates) == 1:
            return candidates[0]

        entry_ts_ms = int(entry.get("timestamp", 0) or 0)
        entry_dt = (
            datetime.utcfromtimestamp(entry_ts_ms / 1000)
            if entry_ts_ms > 0 else None
        )
        result = (entry.get("result") or "").lower()

        def status_penalty(video: Video) -> int:
            if result == "success":
                return 0 if video.status == "posted" else 1
            if result in ("failed", "timeout", "action_blocked"):
                return 0 if video.status != "posted" else 1
            return 0

        def nearest_seconds(video: Video) -> float:
            if entry_dt is None:
                return 0.0
            anchors = [
                video.posted_at,
                video.scheduled_time,
                video.updated_at,
                video.created_at,
            ]
            distances = [
                abs((anchor - entry_dt).total_seconds())
                for anchor in anchors
                if anchor is not None
            ]
            return min(distances) if distances else float("inf")

        candidates.sort(
            key=lambda video: (
                nearest_seconds(video),
                status_penalty(video),
                -(video.id or 0),
            )
        )
        chosen = candidates[0]
        logger.info(
            "[POSTING] Resolved reused phone_video_id=%s on device=%d to "
            "video=%d among candidates=%s",
            phone_video_id,
            device_id,
            chosen.id,
            [video.id for video in candidates],
        )
        return chosen

    async def _upsert_post_log(
        self,
        session: AsyncSession,
        device_id: int,
        entry: dict[str, Any],
    ) -> None:
        """Insert a post log record and update the corresponding video/account."""
        phone_log_id = entry.get("id")
        video_id = entry.get("videoId")
        username = entry.get("accountUsername") or entry.get("username", "")
        result = (entry.get("result") or "").lower()  # normalize: "SUCCESS" → "success"
        error_msg = entry.get("error")
        if error_msg is None:
            error_msg = entry.get("errorMessage")
        if error_msg is None:
            error_msg = entry.get("message")
        if isinstance(error_msg, str):
            error_msg = error_msg.strip() or None
        if error_msg is None:
            if result == "action_blocked":
                error_msg = "Action blocked by Instagram"
            elif result == "failed":
                error_msg = "Device reported a failed post without error details"
            elif result == "timeout":
                error_msg = "Posting timed out on device"
        duration_ms = entry.get("durationMs")

        # Skip duplicates by phone_log_id.
        # Guard: phone Room DB can get wiped (destructive migration,
        # reinstall), resetting the autoincrement to 1.  Old PostLog
        # rows with the same phone_log_id would then false-positive
        # as duplicates.  Compare every existing VPS-insert timestamp
        # for that phone log id with the phone's entry timestamp. If any
        # row is close, this is a replay; if all are far away, it is a
        # phone-side id collision after DB reset.
        if phone_log_id is not None:
            dup_stmt = select(PostLog.timestamp).where(
                PostLog.phone_log_id == phone_log_id,
                PostLog.device_id == device_id,
            )
            dup_rows = (await session.execute(dup_stmt)).all()
            if dup_rows:
                entry_ts_ms = int(entry.get("timestamp", 0) or 0)
                entry_dt = datetime.utcfromtimestamp(entry_ts_ms / 1000) if entry_ts_ms else None
                if entry_dt is None:
                    return
                for dup_row in dup_rows:
                    existing_dt = dup_row.timestamp
                    if (
                        existing_dt
                        and abs((entry_dt - existing_dt).total_seconds()) < 86400
                    ):
                        return

        # Resolve VPS video BEFORE creating the PostLog so we use the
        # correct FK target. Phone reports its phone-side videoId which
        # is NOT a valid FK to videos.id — using it raw caused the
        # FOREIGN KEY constraint flood when a phone retained stale rows
        # for VPS-deleted videos (2026-04-25 incident).
        video: Video | None = None
        if video_id is not None:
            video = await self._resolve_video_for_phone_result(
                session,
                device_id=device_id,
                phone_video_id=video_id,
                username=username,
                entry=entry,
            )
        if video is None and entry.get("filename") and username:
            video = (await session.execute(
                select(Video).where(
                    Video.filename == entry["filename"],
                    Video.account_username == username,
                    Video.status.in_(["scheduled", "uploading"]),
                )
            )).scalar_one_or_none()
        if video is None and video_id is not None:
            candidate = (await session.execute(
                select(Video).where(Video.id == video_id)
            )).scalar_one_or_none()
            if candidate is not None:
                same_username = not username or candidate.account_username == username
                same_device = candidate.device_id is None or candidate.device_id == device_id
                phone_id_compatible = candidate.phone_video_id in (None, video_id)
                if same_username and same_device and phone_id_compatible:
                    video = candidate

        video_was_confirmed_posted = (
            video is not None
            and video.status == "posted"
            and video.post_result == "success"
            and video.posted_at is not None
        )
        incoming_failure_for_confirmed_video = (
            video_was_confirmed_posted
            and result in ("failed", "timeout", "action_blocked")
        )
        log_result = "ignored_failure" if incoming_failure_for_confirmed_video else result
        if incoming_failure_for_confirmed_video:
            logger.info(
                "[POSTING] Ignoring stale failure for already-confirmed video %d "
                "(@%s): result=%s error=%s",
                video.id,
                username,
                result,
                error_msg,
            )

        log = PostLog(
            video_id=video.id if video is not None else None,
            device_id=device_id,
            account_username=username,
            result=log_result,
            error_message=error_msg,
            duration_ms=duration_ms,
            phone_log_id=phone_log_id,
        )
        session.add(log)

        success_counted = result == "success" and not video_was_confirmed_posted
        if video is not None:
            if result == "success":
                posted_at = _utcnow()
                success_counted = await self._mark_video_posted(
                    session,
                    video,
                    posted_at,
                )
            elif incoming_failure_for_confirmed_video:
                pass
            elif result == "action_blocked":
                video.status = "failed"
                video.post_result = "action_blocked"
                video.post_error = error_msg
            elif result in ("failed", "timeout"):
                video.status = "failed"
                video.post_result = result
                video.post_error = error_msg
                video.retry_count = (video.retry_count or 0) + 1
            if duration_ms is not None:
                video.post_duration_ms = duration_ms

            # Clean up ghosted carousel dispatch files on terminal outcomes so
            # we don't fill disk with per-post ghost copies.
            if (
                video.content_type == "carousel"
                and result in ("success", "failed", "action_blocked", "timeout")
            ):
                try:
                    dispatch_dir = Path(self.config.data_dir) / "carousel_dispatch" / str(video.id)
                    if dispatch_dir.exists():
                        shutil.rmtree(dispatch_dir, ignore_errors=True)
                        logger.info(
                            "[POSTING] Cleaned up carousel_dispatch/%d", video.id,
                        )
                except Exception:
                    logger.exception(
                        "[POSTING] Failed to clean carousel_dispatch/%d", video.id,
                    )

        # Update account stats
        if username:
            acct_stmt = select(Account).where(Account.username == username)
            acct_result = await session.execute(acct_stmt)
            account = acct_result.scalar_one_or_none()
            if account is not None:
                if result == "success" and success_counted:
                    account.total_posted = (account.total_posted or 0) + 1
                    account.last_posted_at = _utcnow()
                elif (
                    result in ("failed", "timeout", "action_blocked")
                    and not incoming_failure_for_confirmed_video
                ):
                    account.total_failed = (account.total_failed or 0) + 1

                if result == "action_blocked" and not incoming_failure_for_confirmed_video:
                    # Auto-quarantine disabled per operator request 2026-05-02:
                    # action_blocked signals are noisy and over-aggressive. We
                    # still log + alert Telegram so the human knows, but do
                    # NOT mutate is_blocked / blocked_until / notes. Operator
                    # decides manually whether to pause.
                    logger.warning(
                        "[POSTING] %s reported action_blocked: %s",
                        username, error_msg,
                    )
                    # Two-pronged Telegram dedup (2026-05-05):
                    # (1) Skip alerts for stale buffered events. When
                    #     phone reconnects after a WS gap it re-pushes
                    #     its PostLog buffer; Android timestamp on the
                    #     entry vs VPS now > 2 min means the operator
                    #     has already seen / forgotten this event.
                    # (2) 1h per-account cooldown so the same fresh
                    #     event spammed by a flapping FSM only pages
                    #     once.
                    phone_ts_ms = int(entry.get("timestamp", 0) or 0)
                    age_s = (
                        (time.time() * 1000.0 - phone_ts_ms) / 1000.0
                        if phone_ts_ms > 0 else 0.0
                    )
                    is_stale_replay = age_s > 120.0
                    last_alert = self._last_action_block_alert_at.get(username, 0.0)
                    on_cooldown = (time.time() - last_alert) < 3600.0
                    if is_stale_replay:
                        logger.info(
                            "[POSTING] Skipping action_blocked TG alert "
                            "for @%s: stale replay (%.0fs old)",
                            username, age_s,
                        )
                    elif on_cooldown:
                        logger.info(
                            "[POSTING] Skipping action_blocked TG alert "
                            "for @%s: 1h dedup cooldown active",
                            username,
                        )
                    elif self.telegram_bot is not None:
                        self._last_action_block_alert_at[username] = time.time()
                        try:
                            await self.telegram_bot.notify_post_action_blocked(
                                username, error_msg or "IG action blocked",
                            )
                        except Exception:
                            logger.exception("Failed to send action_blocked TG alert")

        # A11y failure quarantine (2026-04-24):
        #
        # When a post fails with "A11y service not running" (Android
        # executor's explicit signal that AccessibilityService binding
        # is dead), we track consecutive failures per device in memory.
        # After `farm_a11y_failure_threshold` (default 2) consecutive
        # a11y failures on the same device AND heartbeat confirms
        # `accessibilityConnected == false`, we:
        #   - flip Device.status to 'a11y_failure' (free-form str, no
        #     migration needed; process_pending_videos already skips
        #     anything != 'online' through ws_manager.is_online())
        #   - send a Telegram alert with the ready-to-paste ADB fix
        # Auto-recovery lives in health_check: when a heartbeat
        # arrives with a11yLiveBound==true, we flip back to 'online'.
        #
        # The cross-check with heartbeat is load-bearing — a phone
        # might transiently report "A11y service not running" during
        # a UI glitch while the binding is actually healthy. We only
        # quarantine when BOTH signals agree.
        if (
            result in ("failed", "timeout")
            and self._is_a11y_failure(error_msg)
            and not incoming_failure_for_confirmed_video
        ):
            n = self._a11y_fail_counts.get(device_id, 0) + 1
            self._a11y_fail_counts[device_id] = n
            hb = self.ws_manager._device_info.get(device_id) or {}
            # New authoritative field (app >= 2026-04-24) first,
            # fall back to the legacy accessibilityConnected.
            a11y_live = hb.get("a11yLiveBound")
            if a11y_live is None:
                a11y_live = hb.get("accessibilityConnected", True)
            a11y_live = bool(a11y_live)
            logger.warning(
                "[POSTING] A11y failure detected (device=%d acct=@%s "
                "streak=%d/%d heartbeat_a11y=%s)",
                device_id, username, n,
                self.config.farm_a11y_failure_threshold, a11y_live,
            )
            if (
                n >= self.config.farm_a11y_failure_threshold
                and not a11y_live
            ):
                dev = await session.get(Device, device_id)
                if dev is not None and dev.status != "a11y_failure":
                    prev_status = dev.status
                    dev.status = "a11y_failure"
                    dev.last_error = (
                        f"AccessibilityService not running "
                        f"(streak={n}, heartbeat_a11y=False)"
                    )
                    logger.error(
                        "[POSTING] 🚨 Device %d (%s) QUARANTINED — "
                        "a11y dead %dx, prev=%s",
                        device_id, dev.name, n, prev_status,
                    )
                    if (
                        device_id not in self._a11y_alerted
                        and self.telegram_bot is not None
                    ):
                        self._a11y_alerted.add(device_id)
                        try:
                            await self.telegram_bot.send_notification(
                                f"🚨 <b>Device quarantined — a11y dead</b>\n"
                                f"Device: <b>{dev.name}</b> (id={device_id})\n"
                                f"{n} consecutive post failures with "
                                f"'A11y service not running'.\n"
                                f"Phone heartbeat confirms binding is "
                                f"dead.\n\n"
                                f"🔧 <b>What happens next</b>\n"
                                f"1. On-phone watchdog retries self-heal "
                                f"every 30 s (needs "
                                f"<code>WRITE_SECURE_SETTINGS</code>).\n"
                                f"2. If self-heal fails 2× in a row, "
                                f"the phone posts a high-priority "
                                f"notification: "
                                f"<b>«Reelsomet: сервис отключён»</b>.\n"
                                f"3. Ask the client to unlock the "
                                f"phone and tap that notification — "
                                f"Android opens Accessibility Settings "
                                f"pre-scrolled to Reelsomet; one "
                                f"toggle restores the service.\n\n"
                                f"Dispatch to this device is paused "
                                f"until heartbeat reports a11y live."
                            )
                        except Exception:
                            logger.exception(
                                "[POSTING] a11y quarantine TG send failed",
                            )
        elif (
            result == "success"
            or incoming_failure_for_confirmed_video
            or (
                result in ("failed", "timeout")
                and not self._is_a11y_failure(error_msg)
            )
        ):
            # Any good outcome (or a NON-a11y failure) resets the
            # counter. A successful post on a quarantined device is
            # auto-resurrected via health_check's heartbeat check;
            # here we just clear the tracking.
            self._a11y_fail_counts.pop(device_id, None)
            self._a11y_alerted.discard(device_id)

        # Broadcast result
        await self.broadcaster.broadcast("post:result", {
            "device_id": device_id,
            "video_id": video_id,
            "username": username,
            "result": log_result,
            "error": error_msg,
        })

        # Telegram notification (rate-limited: 1 per account per 5 min).
        # Retryable phone failures are kept in DB, but not paged to Telegram
        # until the configured retry budget is exhausted. Otherwise a transient
        # picker timeout can produce a red alert minutes before the same video
        # lands successfully and gets reconciled by profile post count.
        filename = entry.get("filename", f"video:{video_id}")
        retryable_failure_pending = (
            log_result in ("failed", "timeout")
            and video is not None
            and (video.retry_count or 0) < self.config.farm_max_auto_retries
        )
        if retryable_failure_pending:
            logger.info(
                "[POSTING] Suppressing retryable Telegram failure for @%s "
                "video %s: retry_count=%d/%d error=%s",
                username,
                filename,
                video.retry_count or 0,
                self.config.farm_max_auto_retries,
                error_msg,
            )
        elif log_result == "success" and (success_counted or video is None):
            await self._notify_post_success(username, filename, duration_ms or 0)
        elif log_result == "action_blocked":
            await self._notify_post_failure(
                username,
                filename,
                "Action blocked by Instagram",
            )
        elif log_result in ("failed", "timeout"):
            await self._notify_post_failure(
                username,
                filename,
                error_msg or "Unknown error",
            )

    # ------------------------------------------------------------------
    # Job: health_check (every ~60s)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Job: probe_silent_devices (every 60s) — Phase 4, 2026-04-24
    # ------------------------------------------------------------------

    async def probe_silent_devices(self) -> dict[str, Any]:
        """Active healthcheck probe for online but silent devices.

        Sends ``cmd.healthcheck`` with a random ``echoToken`` to every
        online device that hasn't dispatched in the last 90 s. Three
        outcomes per device:

        * **Reply with matching token within 5 s** → record RTT, refresh
          ``Device.last_seen_at`` if the reply payload includes it,
          and check ``a11yLiveBound`` for accessibility-binding sanity.
        * **Reply with mismatched token** → stale (e.g. previous
          probe's reply caught up after we'd given up). Skip — the next
          probe will re-issue.
        * **Timeout / connection error** → record `None` in RTT
          history. After three consecutive failures, force the WS
          closed (1011 internal error) so the phone reconnects, and
          page the operator.

        Returns a small dict for testability:
            ``{probed: int, healthy: int, suspect: int, forced_close: int}``

        This is the single source of truth for "phone is REALLY alive
        end-to-end". Heartbeats prove the OkHttp reader thread is up;
        only a healthcheck reply proves the app handler is responsive.
        """
        now_ms = int(time.time() * 1000)
        probed = 0
        healthy = 0
        suspect = 0
        forced_close = 0

        for device_id in list(self.ws_manager.get_online_device_ids()):
            # Skip devices we just dispatched to — recent traffic is
            # itself a healthcheck (the dispatch reply already proved
            # the app handler is responsive).
            last_dispatch = self._last_dispatch_ms.get(device_id, 0)
            if last_dispatch and (now_ms - last_dispatch) < 90_000:
                continue

            probed += 1
            token = secrets.token_urlsafe(9)
            try:
                reply = await self.bridge.healthcheck(
                    device_id, token, timeout=5.0,
                )
            except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
                logger.warning(
                    "probe_silent_devices: device %d healthcheck failed: %s",
                    device_id, exc,
                )
                self._record_rtt(device_id, None)
                if await self._on_probe_fail(device_id):
                    forced_close += 1
                else:
                    suspect += 1
                continue
            except Exception as exc:
                logger.exception(
                    "probe_silent_devices: device %d unexpected error: %s",
                    device_id, exc,
                )
                self._record_rtt(device_id, None)
                continue

            # Verify echoToken — protects against stale replies that
            # arrived after we'd given up on a previous probe.
            if reply.get("echoToken") != token:
                logger.warning(
                    "probe_silent_devices: device %d echoToken mismatch "
                    "(got %r, expected %r) — treating as stale, skipping",
                    device_id, reply.get("echoToken"), token,
                )
                continue

            reply_at = reply.get("replyAt")
            if isinstance(reply_at, (int, float)):
                rtt_ms = max(0.0, float(now_ms) - float(reply_at))
            else:
                rtt_ms = 0.0
            self._record_rtt(device_id, rtt_ms)
            self._device_health[device_id] = "online"
            self._suspect_alerted.discard(device_id)
            healthy += 1

            # Refresh last_seen_at — this reply proves the phone is
            # alive end-to-end, so the heartbeat clock should be
            # reset even if the next periodic heartbeat is delayed.
            try:
                async with self.session_factory.begin() as session:
                    from server.models import Device as _Device
                    from sqlalchemy import update as _sql_update
                    await session.execute(
                        _sql_update(_Device)
                        .where(_Device.id == device_id)
                        .values(last_seen_at=_utcnow())
                        .execution_options(synchronize_session=False)
                    )
            except Exception as exc:
                logger.warning(
                    "probe_silent_devices: failed to refresh "
                    "last_seen_at for device %d: %s",
                    device_id, exc,
                )

            # A11y status — page once if the phone reports its a11y
            # binding is dead. Phone has its own self-heal so we don't
            # spam every 60 s.
            if not bool(reply.get("a11yLiveBound", True)):
                await self._on_a11y_dead(device_id, reply)
            else:
                self._a11y_dead_alerted.discard(device_id)

        return {
            "probed": probed,
            "healthy": healthy,
            "suspect": suspect,
            "forced_close": forced_close,
        }

    def _record_rtt(self, device_id: int, rtt_ms: float | None) -> None:
        """Append an RTT sample (or ``None`` for a timeout) to the
        per-device rolling history (max length 5)."""
        self._rtt_history[device_id].append(rtt_ms)

    async def _on_probe_fail(self, device_id: int) -> bool:
        """Decide what to do after a single failed healthcheck.

        Returns ``True`` if the device was force-closed (3 consecutive
        timeouts), ``False`` if we just flipped it to ``suspect`` and
        sent a one-shot Telegram page.
        """
        history = self._rtt_history.get(device_id)
        if history is None:
            return False
        # 3 consecutive None entries == three lost probes in a row.
        # Force the socket closed; the phone's reconnect loop wakes up
        # the moment the close arrives, which is the recovery path.
        last_three = list(history)[-3:]
        if len(last_three) >= 3 and all(s is None for s in last_three):
            logger.error(
                "probe_silent_devices: device %d 3x healthcheck timeout — "
                "forcing WS close",
                device_id,
            )
            self._device_health[device_id] = "offline_forced"
            try:
                await self.ws_manager.close(
                    device_id,
                    code=1011,
                    reason="probe_timeout_3x",
                )
            except Exception as exc:
                logger.warning(
                    "probe_silent_devices: ws_manager.close on device %d "
                    "raised %s",
                    device_id, exc,
                )
            # Reset history so the next reconnect starts clean.
            history.clear()
            self._suspect_alerted.discard(device_id)
            if self.telegram_bot is not None:
                try:
                    name = await self._device_name(device_id) or str(device_id)
                    await self.telegram_bot.notify_device_offline(name)
                except Exception as exc:
                    logger.warning(
                        "probe_silent_devices: notify_device_offline "
                        "failed for %d: %s",
                        device_id, exc,
                    )
            return True

        # Single or double failure: flip to "suspect" and (once per
        # streak) page the operator. Force-close on the SAME tick so
        # the phone's reconnect loop kicks in immediately rather than
        # after a 75s OkHttp readTimeout.
        prev = self._device_health.get(device_id)
        self._device_health[device_id] = "suspect"
        if device_id not in self._suspect_alerted:
            self._suspect_alerted.add(device_id)
            if self.telegram_bot is not None:
                try:
                    name = await self._device_name(device_id) or str(device_id)
                    await self.telegram_bot.send_notification(
                        f"⚠️ Device <b>{name}</b> failed an active "
                        f"healthcheck — channel may be wedged. Forcing "
                        f"reconnect."
                    )
                except Exception as exc:
                    logger.warning(
                        "probe_silent_devices: suspect notification "
                        "failed for %d: %s", device_id, exc,
                    )
        try:
            await self.ws_manager.close(
                device_id,
                code=1011,
                reason="probe_timeout_suspect",
            )
        except Exception as exc:
            logger.warning(
                "probe_silent_devices: ws_manager.close (suspect) "
                "for device %d raised %s",
                device_id, exc,
            )
        return False

    async def _on_a11y_dead(self, device_id: int, reply: dict) -> None:
        """Page once when the phone reports a11yLiveBound=False."""
        if device_id in self._a11y_dead_alerted:
            return
        self._a11y_dead_alerted.add(device_id)
        if self.telegram_bot is None:
            return
        try:
            name = await self._device_name(device_id) or str(device_id)
            ago = reply.get("a11yLastEventAgoMs")
            ago_msg = (
                f"a11y last event {int(ago) // 1000}s ago"
                if isinstance(ago, (int, float)) and ago > 0
                else "a11y dead"
            )
            await self.telegram_bot.send_notification(
                f"⚠️ Device <b>{name}</b>: {ago_msg}. "
                f"Phone-side self-heal will retry; please "
                f"check Accessibility Settings if this persists."
            )
        except Exception as exc:
            logger.warning(
                "_on_a11y_dead: alert send for %d failed: %s",
                device_id, exc,
            )

    async def _device_name(self, device_id: int) -> str | None:
        """Resolve a friendly name for a device id (used in alerts)."""
        try:
            async with self.session_factory() as session:
                row = (await session.execute(
                    select(Device.name).where(Device.id == device_id)
                )).scalar_one_or_none()
            return row
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Job: health_check (every 60s)
    # ------------------------------------------------------------------

    async def health_check(self) -> None:
        """Verify devices are still responsive; mark stale ones as offline.

        Also detects devices that were offline in DB but are now reachable
        via WebSocket, marking them online and sending a notification.
        """
        silent_timeout = self.config.farm_device_silent_timeout_seconds
        now = _utcnow()

        went_offline: list[str] = []
        came_online: list[str] = []

        a11y_recovered: list[str] = []

        async with self.session_factory() as session:
            # --- Snapshot offline device IDs before any mutations ---
            offline_stmt = select(Device).where(Device.status == "offline")
            offline_result = await session.execute(offline_stmt)
            previously_offline = list(offline_result.scalars().all())

            # --- Check a11y-quarantined devices — auto-recover on HB ---
            a11y_quarantined = (await session.execute(
                select(Device).where(Device.status == "a11y_failure")
            )).scalars().all()
            for device in a11y_quarantined:
                hb = self.ws_manager._device_info.get(device.id) or {}
                # Prefer the authoritative field; fall back for older
                # APKs that don't ship a11yLiveBound yet.
                live = hb.get("a11yLiveBound")
                if live is None:
                    live = hb.get("accessibilityConnected", False)
                if bool(live):
                    device.status = "online"
                    device.last_error = None
                    self._a11y_fail_counts.pop(device.id, None)
                    self._a11y_alerted.discard(device.id)
                    a11y_recovered.append(device.name)
                    logger.info(
                        "[POSTING] Device %d (%s) auto-recovered from "
                        "a11y_failure — heartbeat reports a11y live",
                        device.id, device.name,
                    )

            # --- Check devices marked online in DB ---
            stmt = select(Device).where(Device.status == "online")
            result = await session.execute(stmt)
            devices = result.scalars().all()

            for device in devices:
                if not self.ws_manager.is_online(device.id):
                    device.status = "offline"
                    logger.info(
                        "Device %d (%s) marked offline — WebSocket disconnected",
                        device.id, device.name,
                    )
                    await self.broadcaster.broadcast("device:disconnected", {
                        "device_id": device.id,
                        "device_name": device.name,
                    })
                    went_offline.append(device.name)
                    continue

                # Check last_seen_at freshness (both naive UTC datetimes)
                if device.last_seen_at is not None:
                    elapsed = now - device.last_seen_at
                    elapsed_s = elapsed.total_seconds()
                    if elapsed_s > silent_timeout:
                        device.status = "offline"
                        logger.warning(
                            "Device %d (%s) silent for %.0fs — marking offline",
                            device.id, device.name, elapsed_s,
                        )
                        await self.broadcaster.broadcast("device:disconnected", {
                            "device_id": device.id,
                            "device_name": device.name,
                            "silent_seconds": round(elapsed_s),
                        })
                        went_offline.append(device.name)

            # --- Check previously-offline devices that are now reachable ---
            for device in previously_offline:
                if self.ws_manager.is_online(device.id):
                    device.status = "online"
                    device.last_seen_at = now
                    logger.info(
                        "Device %d (%s) came back online",
                        device.id, device.name,
                    )
                    await self.broadcaster.broadcast("device:connected", {
                        "device_id": device.id,
                        "device_name": device.name,
                    })
                    came_online.append(device.name)

            await session.commit()

        # Telegram notifications
        if self.telegram_bot is not None:
            for name in went_offline:
                try:
                    await self.telegram_bot.notify_device_offline(name)
                except Exception as exc:
                    logger.warning("Telegram offline notification failed: %s", exc)
            for name in came_online:
                try:
                    await self.telegram_bot.notify_device_online(name)
                except Exception as exc:
                    logger.warning("Telegram online notification failed: %s", exc)
            for name in a11y_recovered:
                try:
                    await self.telegram_bot.send_notification(
                        f"✅ Device <b>{name}</b> auto-recovered — "
                        f"AccessibilityService binding restored."
                    )
                except Exception as exc:
                    logger.warning("Telegram a11y-recover alert failed: %s", exc)

        # Check disk space
        try:
            usage = shutil.disk_usage(self.config.data_dir)
            free_gb = usage.free / (1024**3)
            if free_gb < 0.5:  # Less than 500MB free
                logger.critical("LOW DISK SPACE: %.1f GB free", free_gb)
                if self.telegram_bot is not None:
                    await self.telegram_bot.send_notification(
                        "\u26a0\ufe0f <b>Low disk space!</b>\n"
                        f"Only <b>{free_gb:.1f} GB</b> free on VPS.\n"
                        f"Delete old videos or expand storage."
                    )
        except OSError as exc:
            logger.warning("Disk space check failed: %s", exc)

    # ------------------------------------------------------------------
    # Job: auto_generate_videos (every ~120s)
    # ------------------------------------------------------------------

    async def auto_generate_videos(self) -> None:
        """Auto-generate vid_bait videos for accounts that need more content.

        For each active account with posting_enabled, checks how many
        pending/scheduled videos exist. If fewer than the number of remaining
        posting slots for today+tomorrow, generates more via FFmpeg.
        """
        from server.video_gen import generate_vid_bait

        data_dir = Path(self.config.data_dir)

        # Load scenarios once
        scenarios_path = data_dir / "recreator" / "scenarios.json"
        scenarios: list[dict[str, Any]] = []
        if scenarios_path.exists():
            try:
                scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        # Find font
        font_path: str | None = None
        fonts_dir = data_dir / "fonts"
        for name in ("Montserrat-Bold.ttf", "montserrat-bold.ttf"):
            fp = fonts_dir / name
            if fp.exists():
                font_path = str(fp)
                break

        async with self.session_factory() as session:
            # Active accounts with posting enabled
            stmt = select(Account).where(
                Account.is_active.is_(True),
                Account.is_paused.is_(False),
                Account.is_blocked.is_(False),
                Account.posting_enabled.is_(True),
            )
            result = await session.execute(stmt)
            accounts = result.scalars().all()

        for account in accounts:
            try:
                await self._generate_for_account(
                    account, data_dir, scenarios, font_path,
                )
            except Exception:
                logger.exception(
                    "[GENERATION] Failed for %s", account.username,
                )
            # Carousel generation (separate from vid_bait)
            try:
                async with self.session_factory() as session:
                    await self._maybe_generate_carousel(account, session)
            except Exception:
                logger.exception(
                    "[GENERATION] Carousel generation failed for %s", account.username,
                )
            # Story generation moved to the dedicated `auto_generate_stories`
            # APScheduler job (story dispatcher, 2026-04-14). It runs every
            # 60s on its own interval and uses the LRU-with-freshness picker
            # + per-device cooldown. Do NOT call story generation from the
            # reel generation loop — the two content types now live in
            # independent slot spaces per _schedule_for_account content_type
            # branch, and mixing them here would reintroduce the silent
            # cross-content-type slot collision that the rewrite fixed.

    async def _generate_for_account(
        self,
        account: Account,
        data_dir: Path,
        scenarios: list[dict[str, Any]],
        font_path: str | None,
    ) -> None:
        """Generate videos for one account if it needs more content."""
        from server.video_gen import generate_vid_bait

        # T8: raw-video mode. Tri-state check against the per-account
        # Account.use_scenarios column, falling back to the farm-wide
        # ``farm_use_scenarios_default`` when the column is NULL. When
        # resolved to False the account opts out of the vid_bait
        # generation path entirely — auto_generate_videos will still
        # be called every 10 min, but this account will dispatch
        # whatever is already in ``data_dir/videos/<username>/``.
        resolved_use_scenarios = (
            account.use_scenarios
            if account.use_scenarios is not None
            else self.config.farm_use_scenarios_default
        )
        if not resolved_use_scenarios:
            logger.info(
                "[GENERATION] Skipping @%s (use_scenarios=False, raw-video mode)",
                account.username,
            )
            return

        username = account.username
        model_name = account.recreator_model or "baddie"
        posting_times = _parse_posting_times(account.posting_times) or \
            self.config.farm_default_posting_times or []

        if not posting_times:
            return

        # Find vid_bait clips, sorted newest-first by mtime.
        # When the operator drops a fresh reference into the folder we want
        # it picked sooner than a clip that's already been cycled through
        # for weeks — see weighted pick on `random.choices` below.
        vid_dir = data_dir / "models" / model_name / "vid_bait"
        if not vid_dir.is_dir():
            return

        # Load per-clip flags from photo_catalog.json. Best-effort: catalog can
        # be missing or malformed; fall back to default behavior.
        paused_filenames: set[str] = set()
        skip_text_overlay_filenames: set[str] = set()
        catalog_path = data_dir / "models" / model_name / "photo_catalog.json"
        if catalog_path.is_file():
            try:
                raw = json.loads(catalog_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for entry in raw:
                        if (
                            entry.get("folder") == "vid_bait"
                            and entry.get("paused") is True
                            and entry.get("filename")
                        ):
                            paused_filenames.add(entry["filename"])
                        if (
                            entry.get("folder") == "vid_bait"
                            and entry.get("skip_text_overlay") is True
                            and entry.get("filename")
                        ):
                            skip_text_overlay_filenames.add(entry["filename"])
            except Exception:
                pass

        clips = sorted(
            [f for f in vid_dir.iterdir()
             if f.suffix.lower() in (".mp4", ".mov", ".webm")
             and f.name not in paused_filenames],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not clips:
            return

        # Count pending + scheduled + uploading videos (not yet posted)
        async with self.session_factory() as session:
            count_result = await session.execute(
                select(func.count()).select_from(Video).where(
                    Video.account_username == username,
                    Video.status.in_(["pending", "scheduled", "uploading"]),
                )
            )
            queued_count: int = count_result.scalar() or 0

        # Keep 4 videos ahead so the queue covers the next ~4 hourly slots
        # (with hourly posting_times and 10-min sibling spread, this prevents
        # the empty-queue stall observed 2026-05-01).
        target_queue_depth = 4
        if queued_count >= target_queue_depth:
            return

        needed = target_queue_depth - queued_count

        logger.info(
            "[GENERATION] %s needs %d videos (queued=%d, desired=%d)",
            username, needed, queued_count, target_queue_depth,
        )

        output_dir = data_dir / "videos" / username
        output_dir_issue = self._check_generation_output_dir(output_dir)
        if output_dir_issue is not None:
            logger.error(
                "[GENERATION] Output directory blocked for %s: %s (%s)",
                username,
                output_dir,
                output_dir_issue,
            )
            await self._notify_generation_blocked(
                username=username,
                output_dir=output_dir,
                issue=output_dir_issue,
            )
            return

        for i in range(needed):
            # Weighted pick biased toward newest references. `clips` was
            # sorted mtime-desc above so index 0 == newest. Linear weights
            # `[n, n-1, ..., 1]` give the freshest clip an ~N× boost over
            # the oldest, which is exactly what the operator expects when
            # they drop new references in — without starving the older
            # library (every clip keeps a non-zero chance of rotation).
            n_clips = len(clips)
            weights = list(range(n_clips, 0, -1))
            clip = random.choices(clips, weights=weights, k=1)[0]

            # Pick scenario text unless this source clip is marked as raw.
            scenario_text = ""
            if clip.name in skip_text_overlay_filenames:
                logger.info(
                    "[GENERATION] %s uses raw vid_bait clip without scenario text: %s",
                    username,
                    clip.name,
                )
            elif scenarios:
                sc = random.choice(scenarios)
                scenario_text = sc.get("text", "")

            text_lines = [line.strip() for line in scenario_text.split("\n")
                          if line.strip()] if scenario_text else []

            job_id = uuid.uuid4().hex[:16]
            output_path = output_dir / f"{job_id}.mp4"

            try:
                await generate_vid_bait(
                    video_path=clip,
                    output_path=output_path,
                    text_lines=text_lines,
                    font_path=font_path,
                    font_size=52,
                )
            except Exception:
                logger.exception(
                    "[GENERATION] FFmpeg failed for %s (clip=%s)", username, clip.name,
                )
                continue

            # Create DB records
            async with self.session_factory() as session:
                run = GenerationRun(
                    status="completed",
                    format="vid_bait",
                    requested_count=1,
                    completed_count=1,
                    started_at=_utcnow(),
                    finished_at=_utcnow(),
                )
                session.add(run)
                await session.flush()

                video = Video(
                    filename=f"{job_id}.mp4",
                    original_path=str(output_path),
                    account_username=username,
                    caption=scenario_text[:200] if scenario_text else "",
                    status="pending",
                    generation_run_id=run.id,
                    created_at=_utcnow(),
                    updated_at=_utcnow(),
                )
                session.add(video)
                await session.commit()

            logger.info(
                "[GENERATION] Generated %s for %s (clip=%s)",
                output_path.name, username, clip.name,
            )

    # ------------------------------------------------------------------
    # Carousel generation
    # ------------------------------------------------------------------

    async def _maybe_generate_carousel(
        self,
        account: Account,
        session: AsyncSession,
    ) -> None:
        """If a photo set is available, create a carousel post with Grok caption."""
        if not account.recreator_model:
            return

        # Check if queue already has a carousel pending
        carousel_count = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account.username,
                Video.content_type == "carousel",
                Video.status.in_(["pending", "scheduled", "uploading"]),
            )
        )).scalar() or 0
        if carousel_count > 0:
            return

        # Find an unused photo set for this model.
        #
        # Two dedup rules combined:
        #
        #   1. Per-set hard cap — if `max_uses_per_account` is set on a
        #      PhotoSet, that set is permanently excluded for accounts
        #      that have already posted it that many times (max_uses=1
        #      = one-shot). Ignores the global cooldown.
        #
        #   2. Global cooldown — for sets with NULL max_uses, the legacy
        #      rolling window (`carousel_set_cooldown_days`) applies:
        #      a set used within the window is temporarily excluded.
        #
        # Implementation: one subquery per rule, union'd via OR inside
        # the NOT IN. Both subqueries return set_ids that should be
        # blocked for this account.
        cooldown_days = self.config.carousel_set_cooldown_days
        cooldown_date = _utcnow() - timedelta(days=cooldown_days)

        # Subquery 1: sets that hit their per-set max_uses_per_account
        # cap for this account. GROUP BY + HAVING counts usage rows per
        # set and compares against the cap column.
        hard_capped_sets = (
            select(PhotoSetUsage.set_id)
            .join(PhotoSet, PhotoSet.id == PhotoSetUsage.set_id)
            .where(
                PhotoSetUsage.account_username == account.username,
                PhotoSet.max_uses_per_account.is_not(None),
            )
            .group_by(PhotoSetUsage.set_id, PhotoSet.max_uses_per_account)
            .having(func.count(PhotoSetUsage.id) >= PhotoSet.max_uses_per_account)
        )

        # Subquery 2: sets on cooldown for this account (only applies
        # to sets with NULL max_uses_per_account — sets that opted into
        # the per-set cap bypass the cooldown entirely).
        cooldown_sets = (
            select(PhotoSetUsage.set_id)
            .join(PhotoSet, PhotoSet.id == PhotoSetUsage.set_id)
            .where(
                PhotoSetUsage.account_username == account.username,
                PhotoSetUsage.used_at > cooldown_date,
                PhotoSet.max_uses_per_account.is_(None),
            )
        )

        # Deterministic order (Codex Q10): without ORDER BY, SQLite's
        # choice of which eligible row to return is arbitrary and
        # changes between backend versions. ORDER BY id ASC makes the
        # selection reproducible — the oldest still-eligible set goes
        # first, which also spreads usage across the whole catalog over
        # time instead of hammering whichever set SQLite happens to
        # return first.
        available_set = (await session.execute(
            select(PhotoSet).where(
                PhotoSet.model == account.recreator_model,
                PhotoSet.is_active.is_(True),
                ~PhotoSet.id.in_(hard_capped_sets),
                ~PhotoSet.id.in_(cooldown_sets),
            ).order_by(PhotoSet.id.asc()).limit(1)
        )).scalar_one_or_none()

        if not available_set:
            return

        # Get images
        images = (await session.execute(
            select(PhotoSetImage)
            .where(PhotoSetImage.set_id == available_set.id)
            .order_by(PhotoSetImage.sort_order)
            .limit(self.config.carousel_max_photos)
        )).scalars().all()

        if len(images) < self.config.carousel_min_photos:
            return

        # Generate caption via LLM (model-scoped seed pool first, fall back to global)
        caption = await self._generate_carousel_caption(
            account.username, len(images),
            json.loads(available_set.tags) if available_set.tags else [],
            model=available_set.model,
        )

        image_filenames = [img.filename for img in images]
        video = Video(
            filename=f"carousel_{available_set.uuid[:8]}.jpg",
            account_username=account.username,
            content_type="carousel",
            image_filenames=json.dumps(image_filenames),
            caption=caption,
            status="pending",
            uploaded_to_phone=False,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session.add(video)
        await session.flush()

        session.add(PhotoSetUsage(
            set_id=available_set.id,
            video_id=video.id,
            account_username=account.username,
        ))
        await session.commit()
        logger.info(
            "[GENERATION] Created carousel post for @%s from set '%s' (%d photos, caption='%s')",
            account.username, available_set.name, len(images), caption[:50],
        )

    # ------------------------------------------------------------------
    # Story asset generation (single-file photo/video story posts)
    # ------------------------------------------------------------------

    async def _pick_story_asset_for_account(
        self,
        session: AsyncSession,
        account_username: str,
        model: str | None,
        device_id: int | None,
        cooldown_cutoff: datetime,
    ) -> StoryAsset | None:
        """Deterministic LRU-with-freshness picker for story assets.

        Priority order:
          Tier 1 — never-used-on-account (acct.last_used IS NULL)
            within tier: StoryAsset.created_at DESC (newest unused first)
          Tier 2 — used-on-account
            within tier: acct.last_used ASC (stalest-used first)
          Final tiebreak: random()

        Filters:
          - is_active = True
          - model = account.recreator_model (when set; None = any)
          - NOT in same-device recent-use window (device_id + cutoff)
          - respect StoryAsset.max_uses_per_account per-account hard cap

        Returns None when the pool is exhausted. Caller MUST handle
        None by leaving the slot empty — do not silently reuse.
        """
        acct_sub = (
            select(
                StoryAssetUsage.asset_id.label("asset_id"),
                func.max(StoryAssetUsage.used_at).label("last_used"),
                func.count().label("uses"),
            )
            .where(StoryAssetUsage.account_username == account_username)
            .group_by(StoryAssetUsage.asset_id)
            .subquery()
        )

        stmt = (
            select(StoryAsset)
            .outerjoin(acct_sub, acct_sub.c.asset_id == StoryAsset.id)
            .where(
                StoryAsset.is_active.is_(True),
                or_(
                    StoryAsset.max_uses_per_account.is_(None),
                    func.coalesce(acct_sub.c.uses, 0)
                        < StoryAsset.max_uses_per_account,
                ),
            )
        )
        if model:
            stmt = stmt.where(StoryAsset.model == model)

        if device_id is not None:
            recent_device_sub = (
                select(StoryAssetUsage.asset_id)
                .where(
                    StoryAssetUsage.device_id == device_id,
                    func.coalesce(
                        StoryAssetUsage.dispatched_at,
                        StoryAssetUsage.used_at,
                    ) >= cooldown_cutoff,
                )
                .distinct()
                .subquery()
            )
            stmt = stmt.outerjoin(
                recent_device_sub,
                recent_device_sub.c.asset_id == StoryAsset.id,
            ).where(recent_device_sub.c.asset_id.is_(None))

        # ORDER BY: tier column (0 for never-used, 1 for used),
        # then newest-unused first (created_at DESC), then stalest-
        # used first (last_used ASC), then created_at DESC tiebreak,
        # then random() final jitter so sibling accounts with identical
        # pool state still disagree.
        stmt = stmt.order_by(
            case((acct_sub.c.last_used.is_(None), 0), else_=1),
            case(
                (acct_sub.c.last_used.is_(None), StoryAsset.created_at),
            ).desc(),
            acct_sub.c.last_used.asc(),
            StoryAsset.created_at.desc(),
            func.random(),
        ).limit(1)

        return (await session.execute(stmt)).scalar_one_or_none()

    async def auto_generate_stories(self) -> None:
        """Fill empty story slots for all story-enabled accounts.

        Runs every `farm_poll_interval_seconds` (default 30s) via
        APScheduler with ``max_instances=1`` so two concurrent runs
        never overlap. For each active account with a configured
        `recreator_model`, picks one eligible `StoryAsset` via the
        LRU-with-freshness picker, copies it into `stories/<account>/`,
        creates a pending `Video(content_type="story")` row, and
        stamps `StoryAssetUsage.device_id` at generation time.

        The sibling-dedup invariant relies on same-transaction
        visibility: account A's usage row is flushed (not committed)
        before the picker runs for account B, so B's recent_device
        CTE sees A's pick and excludes it.
        """
        async with self.session_factory() as session:
            accts_result = await session.execute(
                select(Account).where(
                    Account.is_active.is_(True),
                    Account.is_paused.is_(False),
                    Account.is_blocked.is_(False),
                    Account.posting_enabled.is_(True),
                )
            )
            for account in accts_result.scalars().all():
                try:
                    await self._generate_story_for_account(session, account)
                except Exception:
                    logger.exception(
                        "[GENERATION] story generation failed for @%s",
                        account.username,
                    )
            await session.commit()

    async def _generate_story_for_account(
        self,
        session: AsyncSession,
        account: Account,
    ) -> None:
        """Create one pending story Video for `account` if eligible.

        Eligibility gates (all must pass):
          1. `account.recreator_model` is set (otherwise skip silently).
          2. Account has fewer than 2 pending story rows in the queue
             (queue headroom cap to prevent stockpiling).
          3. The LRU picker returns a non-None asset (per-device
             cooldown + per-account hard cap + model filter).
          4. The source file exists on disk under
             `data_dir/story_assets/<filename>`.

        On success:
          - Copies the asset to `data_dir/stories/<account>/<uuid8>_<filename>`
            so the existing dispatch glob finds it and downstream ghost
            re-fingerprints it per-video (matches the old behavior).
          - Creates `Video(content_type="story", status="pending")`.
          - Creates `StoryAssetUsage` with `device_id` stamped (the
            sibling-dedup CTE depends on this).
          - Resolves `story_element` using the account-aware resolvers
            (`_resolve_story_element_probability` + `_resolve_story_element_type`)
            so the dead-resolver bug is fixed.
          - `session.flush()` at the end ensures the next account in the
            same tick sees this usage row via its `recent_device` CTE.

        Replaces the legacy `_maybe_generate_story` (story dispatcher
        rewrite, 2026-04-14). Does NOT commit — caller
        (`auto_generate_stories`) owns the outer commit fence.
        """
        if not account.recreator_model:
            return

        # Queue headroom cap — match legacy behavior (2 pending stories).
        pending_stories = (await session.execute(
            select(func.count()).select_from(Video).where(
                Video.account_username == account.username,
                Video.content_type == "story",
                Video.status == "pending",
            )
        )).scalar() or 0
        if pending_stories >= 2:
            return

        device_id = await self._resolve_device_id(session, account.username)
        cooldown_cutoff = _utcnow() - timedelta(
            minutes=self.config.farm_min_storyasset_reuse_gap_minutes,
        )

        asset = await self._pick_story_asset_for_account(
            session,
            account_username=account.username,
            model=account.recreator_model,
            device_id=device_id,
            cooldown_cutoff=cooldown_cutoff,
        )
        if asset is None:
            # Pool exhausted for this (account, device, cooldown) — leave
            # the slot empty and wait for the next tick. Warning-level so
            # operators can see exhaustion in prod logs without triggering
            # alerts (expected intermittently when the catalog is small).
            logger.info(
                "[GENERATION] story pool exhausted for @%s on device %s",
                account.username, device_id,
            )
            return

        # Copy the source file into stories/<account>/ with a unique
        # prefix so two successive uses of the same asset never collide
        # on disk. The copy is cheap (<10 MB) and keeps the downstream
        # dispatch glob + ghost pipeline unchanged.
        src = Path(self.config.data_dir) / "story_assets" / asset.filename
        if not src.exists():
            logger.warning(
                "[GENERATION] Story asset #%d file missing on disk: %s",
                asset.id, src,
            )
            return

        stories_dir = Path(self.config.data_dir) / "stories" / account.username
        stories_dir.mkdir(parents=True, exist_ok=True)
        dst_name = f"{uuid.uuid4().hex[:8]}_{asset.filename}"
        dst = stories_dir / dst_name
        try:
            await asyncio.to_thread(shutil.copy2, str(src), str(dst))
        except Exception:
            logger.exception(
                "[GENERATION] Failed to copy story asset %s → %s", src, dst,
            )
            return

        # Resolve story element via the account-aware resolvers so
        # per-account `story_element_probability` + weights are honored.
        try:
            story_element = await self._generate_story_element_for_account(account)
        except Exception:
            logger.warning(
                "[GENERATION] story_element resolver failed for @%s — posting plain story",
                account.username,
                exc_info=True,
            )
            story_element = None

        video = Video(
            filename=dst_name,
            original_path=str(dst),
            account_username=account.username,
            content_type="story",
            caption=asset.caption_fallback or "",
            status="pending",
            uploaded_to_phone=False,
            story_element=json.dumps(story_element) if story_element else None,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session.add(video)
        await session.flush()

        session.add(StoryAssetUsage(
            asset_id=asset.id,
            video_id=video.id,
            account_username=account.username,
            device_id=device_id,
        ))
        # Flush (not commit) so the sibling-dedup CTE sees this row in
        # the same transaction when the next account's picker runs.
        await session.flush()

        logger.info(
            "[GENERATION] Created story post for @%s from asset '%s' "
            "(media_type=%s, element=%s, device_id=%s)",
            account.username, asset.filename, asset.media_type,
            (story_element or {}).get("type", "none"), device_id,
        )

    async def _generate_carousel_caption(
        self, username: str, photo_count: int, tags: list[str],
        model: str | None = None,
    ) -> str:
        """Generate carousel caption via LLM. Falls back to config default.

        Caption seeds are scoped per model: prefer seeds tagged with the
        photo set's model, fall back to global (model IS NULL) seeds.
        """
        from server.llm_client import _call_openai_compat, _call_anthropic, PROVIDER_DEFAULTS

        # Pick a seed prompt: model-scoped first, then global as fallback
        seed_text = ""
        try:
            async with self.session_factory() as session:
                seed = None
                if model:
                    seed = (await session.execute(
                        select(CaptionSeed)
                        .where(
                            CaptionSeed.is_active.is_(True),
                            CaptionSeed.model == model,
                        )
                        .order_by(func.random())
                        .limit(1)
                    )).scalar_one_or_none()
                if seed is None:
                    seed = (await session.execute(
                        select(CaptionSeed)
                        .where(
                            CaptionSeed.is_active.is_(True),
                            CaptionSeed.model.is_(None),
                        )
                        .order_by(func.random())
                        .limit(1)
                    )).scalar_one_or_none()
                if seed:
                    seed_text = seed.seed_prompt
        except Exception:
            pass

        system_prompt = (
            f"You are a social media manager for an AI OFM model @{username}. "
            f"Reply with ONLY the final caption (1-3 sentences + emojis). No hashtags."
        )
        user_prompt = (
            f"Write a carousel caption for {photo_count} photos. "
            f"Tags: {', '.join(tags) if tags else 'lifestyle'}."
        )
        if seed_text:
            user_prompt += f" Direction: {seed_text}"

        # Fast path: no LLM provider → silently use the static fallback so the
        # farm keeps posting. Log a WARNING only when farm_llm_required is set
        # (staging/QA environments that expect an LLM to be configured).
        if not self.config.llm_provider:
            if getattr(self.config, "farm_llm_required", False):
                logger.warning(
                    "[GENERATION] LLM unset; using carousel caption fallback for @%s",
                    username,
                )
            return self.config.carousel_caption_fallback

        try:
            provider = self.config.llm_provider or "grok"
            base_url = self.config.llm_base_url or PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")
            if provider == "anthropic":
                result = await _llm_call_with_retry(
                    _call_anthropic,
                    base_url, self.config.llm_api_key, self.config.llm_model,
                    system_prompt, user_prompt, 30.0,
                )
            else:
                result = await _llm_call_with_retry(
                    _call_openai_compat,
                    base_url, self.config.llm_api_key, self.config.llm_model,
                    system_prompt, user_prompt, 30.0,
                )
            if result and len(result.strip()) > 3:
                return result.strip()
        except Exception as exc:
            # Successful fallback is not ERROR-worthy — the farm keeps posting.
            logger.warning(
                "[GENERATION] LLM carousel caption failed for @%s (%s: %s); using fallback",
                username, type(exc).__name__, exc,
            )

        return self.config.carousel_caption_fallback

    # ------------------------------------------------------------------
    # Story element generation (Poll/Question stickers via Grok)
    # ------------------------------------------------------------------

    # TODO: wire into _maybe_generate_story after T2 merges.
    # T2 is adding _maybe_generate_story, which will call the three
    # resolvers below to decide (a) whether to roll a new story at all
    # (_resolve_story_element_probability), (b) which element type the
    # story carries (_resolve_story_element_type), and the daily cap is
    # enforced in _schedule_for_account via _resolve_story_max_per_day
    # so the schedule side stays authoritative regardless of generator
    # ordering.
    def _resolve_story_max_per_day(self, account: Account) -> int:
        """Return the per-account story cap, falling back to the global default.

        NULL on the account column means 'inherit'. Enforced in
        `_schedule_for_account`, not at generation time — the scheduler
        is the single authority on posting-slot budget so a generator
        that races with a cap change cannot over-schedule.
        """
        if account.story_max_per_day is not None:
            return int(account.story_max_per_day)
        return int(self.config.farm_max_stories_per_account_per_day)

    def _resolve_story_element_probability(self, account: Account) -> float:
        """Return the per-account story-element probability, fallback to global."""
        if account.story_element_probability is not None:
            return float(account.story_element_probability)
        return float(self.config.story_element_probability)

    def _resolve_story_element_type(self, account: Account) -> str:
        """Pick 'poll' / 'question' / 'text' using per-account weights.

        Falls back to `story_element_weights_default` when the account
        has no override. Weights sum is computed on the fly; any element
        can be disabled by setting its weight to 0. A cumulative
        `random.random() * total` loop is used (not `random.choices`) so
        the test suite can mock `random.random` to deterministic boundary
        values without fighting a C-implemented weighted sampler.

        If the combined weights sum to <= 0 (degenerate config) the
        fallback is the last key in insertion order so we return a valid
        element type instead of raising.
        """
        weights_json = account.story_element_weights or self.config.story_element_weights_default
        try:
            weights = json.loads(weights_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "[GENERATION] Invalid story_element_weights for @%s, falling back to default",
                account.username,
            )
            weights = json.loads(self.config.story_element_weights_default)

        items = list(weights.items())
        if not items:
            # Pathological: empty dict. Fall back to 'text' so callers
            # always get a valid element type string.
            return "text"

        total = float(sum(w for _, w in items))
        if total <= 0:
            return items[-1][0]

        r = random.random() * total
        acc = 0.0
        for key, weight in items:
            acc += float(weight)
            if r < acc:
                return key
        # Floating-point rounding fallback: return the last key.
        return items[-1][0]

    async def _generate_story_element(self, username: str) -> dict | None:
        """Legacy wrapper — uses global config probability + 50/50 poll/question.

        Kept as a thin wrapper for pre-existing callers in the test suite
        (test_llm_fallbacks.py / test_llm_invariants.py) and any other site
        that doesn't have an Account object on hand. New code should call
        `_generate_story_element_for_account(account)` which honors the
        per-account probability + weights resolvers.
        """
        if random.random() > self.config.story_element_probability:
            return None  # Plain story, no sticker
        element_type = random.choice(["poll", "question"])
        return await self._generate_story_element_with_type(username, element_type)

    async def _generate_story_element_for_account(
        self, account: Account,
    ) -> dict | None:
        """Account-aware story element generator (story-dispatcher 2026-04-14).

        Wires `_resolve_story_element_probability` (per-account override)
        and `_resolve_story_element_type` (per-account weights) into the
        LLM pipeline. Fixes the dead-resolver bug where the previous
        `_generate_story_element` always read the global probability and
        hard-coded `random.choice(["poll","question"])`.

        Returns `None` when:
          - the probability roll decides "plain story, no sticker", OR
          - the resolved type is `text` (no Android FSM handler yet — D7
            in the spec; revisit when the handler lands).
        """
        prob = self._resolve_story_element_probability(account)
        if random.random() > prob:
            return None

        element_type = self._resolve_story_element_type(account)
        if element_type == "text":
            logger.info(
                "[GENERATION] skipping text element for @%s (no FSM handler yet)",
                account.username,
            )
            return None

        return await self._generate_story_element_with_type(
            account.username, element_type,
        )

    async def _generate_story_element_with_type(
        self, username: str, element_type: str,
    ) -> dict | None:
        """LLM pipeline extracted from the legacy `_generate_story_element`.

        Takes an explicit `element_type` (already resolved by the caller)
        so we don't re-roll weights inside here. Handles:
          - LLM-unset fast path → static fallback (story_poll_fallback /
            story_question_fallback)
          - LLM provider call with retry
          - Fallback on empty/short LLM responses
          - Exception fallback to static text

        Returns `{"type": "poll"|"question", "text": ..., "options": [...]}`.
        """
        from server.llm_client import _call_openai_compat, _call_anthropic, PROVIDER_DEFAULTS

        if element_type == "poll":
            system_prompt = (
                "You are a social media manager for an AI OFM model's Instagram story. "
                "Write a flirty, engaging poll. "
                "Format your response EXACTLY as: QUESTION | OPTION1 | OPTION2. "
                "Nothing else. No quotes, no explanations."
            )
            user_prompt = f"Write a poll for @{username}'s Instagram story."
            fallback_text = self.config.story_poll_fallback  # e.g. "Like this? | Yes | No"
        else:
            system_prompt = (
                "You are a social media manager for an AI OFM model's Instagram story. "
                "Write ONE short, flirty, provocative question for a Question sticker. "
                "Reply with ONLY the question (no quotes, no explanations)."
            )
            user_prompt = f"Write a question for @{username}'s Instagram story."
            fallback_text = self.config.story_question_fallback  # e.g. "Ask me anything 👀"

        # Fast path: LLM unset → use the static fallback so stories still post.
        if not self.config.llm_provider:
            if getattr(self.config, "farm_llm_required", False):
                logger.warning(
                    "[GENERATION] LLM unset; using story %s fallback for @%s",
                    element_type, username,
                )
            result = fallback_text
        else:
            try:
                provider = self.config.llm_provider or "grok"
                base_url = self.config.llm_base_url or PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")
                if provider == "anthropic":
                    result = await _llm_call_with_retry(
                        _call_anthropic,
                        base_url, self.config.llm_api_key, self.config.llm_model,
                        system_prompt, user_prompt, 30.0,
                    )
                else:
                    result = await _llm_call_with_retry(
                        _call_openai_compat,
                        base_url, self.config.llm_api_key, self.config.llm_model,
                        system_prompt, user_prompt, 30.0,
                    )
                if not result or len(result.strip()) < 3:
                    result = fallback_text
            except Exception as exc:
                # Story still posts with the static fallback — WARNING, not ERROR.
                logger.warning(
                    "[GENERATION] LLM story %s failed for @%s (%s: %s); using fallback",
                    element_type, username, type(exc).__name__, exc,
                )
                result = fallback_text

        result = result.strip()

        if element_type == "poll":
            # Parse "QUESTION | OPTION1 | OPTION2"
            parts = [p.strip() for p in result.split("|")]
            if len(parts) < 3:
                parts = [p.strip() for p in fallback_text.split("|")]
            return {
                "type": "poll",
                "text": parts[0][:80],
                "options": [p[:40] for p in parts[1:3]],
            }
        else:
            return {
                "type": "question",
                "text": result[:150],
            }

    # `_maybe_populate_story_element` removed in the 2026-04-14 story
    # dispatcher rewrite. Story element generation now happens inline
    # in `_generate_story_for_account` at row-creation time, so there
    # are never pending stories with NULL element waiting for post-hoc
    # population. The LLM fallback/retry logic lives in
    # `_generate_story_element_with_type`.

    def _check_generation_output_dir(self, output_dir: Path) -> str | None:
        """Return a writeability issue for the output dir, if any."""
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"mkdir failed: {exc}"

        probe_path = output_dir / f".reelsomet-write-probe-{uuid.uuid4().hex}"
        try:
            probe_path.write_bytes(b"")
        except OSError as exc:
            try:
                stat_info = output_dir.stat()
                ownership = (
                    f"mode={oct(stat_info.st_mode & 0o777)} "
                    f"uid={stat_info.st_uid} gid={stat_info.st_gid}"
                )
            except OSError as stat_exc:
                ownership = f"stat unavailable: {stat_exc}"
            return f"{exc}; {ownership}"
        finally:
            try:
                probe_path.unlink()
            except OSError:
                pass

        return None

    async def _notify_generation_blocked(
        self,
        username: str,
        output_dir: Path,
        issue: str,
    ) -> None:
        """Send a rate-limited alert when generation cannot write output."""
        key = f"generation:{username}"
        now_ts = time.time()
        last_ts = self._last_notify_ts.get(key, 0.0)
        if self.telegram_bot is None or (now_ts - last_ts) < self._notify_cooldown:
            return

        self._last_notify_ts[key] = now_ts
        try:
            await self.telegram_bot.send_notification(
                "⚠️ <b>Video generation blocked</b>\n"
                f"Account: @{html.escape(username)}\n"
                f"Path: <code>{html.escape(str(output_dir))}</code>\n"
                f"Issue: {html.escape(issue[:300])}",
            )
        except Exception as exc:
            logger.warning("Telegram generation alert failed: %s", exc)

    # ------------------------------------------------------------------
    # Job: auto_schedule_videos (every ~60s)
    # ------------------------------------------------------------------

    async def auto_schedule_videos(self) -> None:
        """Assign scheduled_time to pending videos that lack one.

        Runs two passes per account — once for reels using
        `posting_times` / `farm_default_posting_times` and the reel
        cap, once for stories using `story_posting_times` /
        `farm_default_story_posting_times` and the per-account story
        cap resolved via `_resolve_story_max_per_day`. This prevents
        story videos from being slotted into reel wall-clock slots
        (silent bug before the 2026-04-14 story-dispatcher rewrite).
        """
        default_reel_times = self.config.farm_default_posting_times or []
        default_story_times = self.config.farm_default_story_posting_times or []
        max_reels_per_day = self.config.farm_max_posts_per_account_per_day
        jitter_std = self.config.farm_schedule_jitter_std_seconds

        async with self.session_factory() as session:
            # Active accounts with posting enabled
            accts_stmt = select(Account).where(
                Account.is_active.is_(True),
                Account.is_paused.is_(False),
                Account.is_blocked.is_(False),
                Account.posting_enabled.is_(True),
            )
            accts_result = await session.execute(accts_stmt)
            accounts = accts_result.scalars().all()

            for account in accounts:
                # Reel pass: per-account `posting_times` or global fallback.
                reel_times = (
                    _parse_posting_times(account.posting_times)
                    or default_reel_times
                )
                if reel_times:
                    try:
                        await self._schedule_for_account(
                            session, account, reel_times,
                            max_reels_per_day, jitter_std,
                            content_type="reel",
                        )
                    except Exception:
                        logger.exception(
                            "Auto-schedule (reel) failed for account %s",
                            account.username,
                        )

                # Story scheduling is disabled until Android story posting is
                # implemented. Leaving story rows unscheduled prevents PNG
                # assets from entering the Reel posting flow.

            await session.commit()

    async def _schedule_for_account(
        self,
        session: AsyncSession,
        account: Account,
        default_posting_times: list[str],
        max_per_day: int,
        jitter_std: int,
        content_type: str = "reel",
    ) -> None:
        """Assign scheduled_time to unscheduled pending videos for one account.

        Uses a stride-based uniform distribution across the next
        ``farm_schedule_horizon_days`` days of per-account slots:

        1. Compute every future slot inside the horizon.
        2. Subtract slots already booked by videos of this account (coarse
           minute-precision collision detection).
        3. Derive a stride = available_slots // pending_count so N pending
           videos land at slot[0], slot[stride], slot[2*stride], ... —
           spreading the backlog evenly instead of packing it onto the
           first N consecutive slots.
        4. Sibling spread (intra-slot offset) is still applied on top.

        `content_type` controls BOTH which per-account column is read
        (posting_times vs story_posting_times) AND which Video rows are
        filtered/matched against the slot computation. Reels and stories
        live in separate slot spaces so a 09:00 reel and a 10:00 story
        never collide in the booked-slot set.
        """
        # Parse account-level or default posting times, branching on
        # content_type so stories pull from `story_posting_times`.
        if content_type == "story":
            times_str = (
                _parse_posting_times(account.story_posting_times)
                or default_posting_times
            )
        else:
            times_str = (
                _parse_posting_times(account.posting_times)
                or default_posting_times
            )

        # Reel cap honors `account.max_posts_per_day` override; stories
        # always use the already-resolved cap (passed in as `max_per_day`)
        # because `_resolve_story_max_per_day` already merged the
        # per-account override into the scalar.
        if content_type == "story":
            per_day_limit = max_per_day
        else:
            per_day_limit = (
                account.max_posts_per_day
                if account.max_posts_per_day is not None
                else max_per_day
            )
        if per_day_limit <= 0:
            return

        horizon_days = max(1, int(self.config.farm_schedule_horizon_days))
        now = _utcnow()
        horizon_end = now + timedelta(days=horizon_days)

        # Count videos that already have a scheduled_time anywhere in the
        # horizon window (not just today). The legacy "today-only" count
        # misbehaved once the horizon grew: a 7-day cap must be compared
        # against 7 days of already-booked videos.
        #
        # Filter by `content_type` so reels and stories track their
        # horizon budgets independently (story dispatcher, 2026-04-14).
        already_count_stmt = (
            select(func.count())
            .select_from(Video)
            .where(
                Video.account_username == account.username,
                Video.content_type == content_type,
                Video.scheduled_time.is_not(None),
                Video.scheduled_time >= now,
                Video.scheduled_time < horizon_end,
                Video.status.in_(["pending", "scheduled", "posting", "posted"]),
            )
        )
        already_count: int = (await session.execute(already_count_stmt)).scalar() or 0

        horizon_cap = per_day_limit * horizon_days
        cap_remaining = max(0, horizon_cap - already_count)
        if cap_remaining == 0:
            return

        # Stagger sibling accounts on the same device within a posting slot
        # so phone-side dispatching ripples out over several minutes instead
        # of all siblings colliding on the exact same wall-clock minute and
        # blocking each other through the "one video per device per cycle"
        # rule in process_pending_videos. Inactive / paused / blocked
        # siblings do NOT consume a position slot — otherwise deleting or
        # pausing an account would leave a hole in the spread ordering.
        #
        # NOTE: must be computed BEFORE building the booked-slot set below,
        # because the booked values need to be projected back into the same
        # coordinate space as ``all_slots`` (which are raw base slots, pre
        # spread_offset). See the projection comment further down.
        spread_seconds = max(0, self.config.farm_intra_slot_spread_seconds)
        effective_jitter = 0 if spread_seconds > 0 else jitter_std
        spread_offset = timedelta(0)
        if spread_seconds > 0:
            dev_id = await self._resolve_device_id(session, account.username)
            if dev_id is not None:
                siblings_rows = (await session.execute(
                    select(AccountDevice.account_username)
                    .join(Account, Account.username == AccountDevice.account_username)
                    .where(
                        AccountDevice.device_id == dev_id,
                        Account.is_active.is_(True),
                        Account.posting_enabled.is_(True),
                        Account.is_blocked.is_(False),
                        Account.is_paused.is_(False),
                    )
                    .order_by(AccountDevice.id.asc())
                )).all()
                siblings = [row[0] for row in siblings_rows]
                try:
                    position = siblings.index(account.username)
                except ValueError:
                    position = 0
                spread_offset = timedelta(seconds=position * spread_seconds)

        # Collect already-booked scheduled_time values so we can subtract
        # them from the candidate slot list. Two unscheduled videos must
        # NEVER be assigned a time that's already in use — the legacy code
        # did not perform this check and could collide on popular slots.
        already_booked_rows = (await session.execute(
            select(Video.scheduled_time).where(
                Video.account_username == account.username,
                Video.content_type == content_type,
                Video.scheduled_time.is_not(None),
                Video.scheduled_time >= now,
                Video.scheduled_time < horizon_end,
                Video.status.in_(["pending", "scheduled", "posting", "posted"]),
            )
        )).scalars().all()
        # When we project booked scheduled_times back to base-slot space,
        # subtract this account's current spread_offset so the comparison
        # operates on the same coordinate as ``all_slots`` (which are raw
        # base slots before the per-account spread_offset is added).
        #
        # Edge case: if an account's position in the sibling list changes
        # between runs (new sibling added, another removed), the old
        # bookings were made with a DIFFERENT spread_offset. Projecting
        # with today's offset won't match them exactly — which is fine,
        # because the bookings are still stored with their old offsets
        # and new bookings will skip the slots where the new-offset
        # projection lands. Collisions can only recur if the position
        # index happens to change in a way that maps onto a previous
        # slot ± offset, which is a negligible corner case.
        #
        # When spread_offset == timedelta(0) (no spread active) the
        # projection is a no-op — equivalent to the pre-T9 behavior.
        booked_set_base = {
            (t - spread_offset).replace(second=0, microsecond=0)
            for t in already_booked_rows
            if t is not None
        }

        # Compute future slots across the full horizon, then subtract the
        # already-booked ones (coarse minute precision, projected into the
        # base-slot coordinate space).
        all_slots = self._compute_slots(
            times_str,
            now,
            effective_jitter,
            self.config.farm_timezone,
            days=horizon_days,
        )
        available_slots = [
            s for s in all_slots
            if s.replace(second=0, microsecond=0) not in booked_set_base
        ]
        if not available_slots:
            return

        # Cap pending work by both the horizon daily-cap budget AND the
        # number of available slots. If pending > slots we stride=1 and
        # whatever does not fit stays unscheduled until the next tick.
        remaining_slots = max(
            0, min(cap_remaining, len(available_slots)),
        )
        if remaining_slots == 0:
            return

        # Find unscheduled pending videos — newest first (LIFO). When the
        # operator uploads a new video, they expect it to go out BEFORE
        # the stale backlog they've been sitting on. Without this, freshly
        # uploaded media waits behind whatever old pending rows haven't
        # drained yet and the queue feels like it's posting yesterday's
        # content. Tiebreak on id to keep the order stable when two
        # videos share a created_at (bulk uploads).
        unscheduled_stmt = (
            select(Video)
            .where(
                Video.account_username == account.username,
                Video.content_type == content_type,
                Video.status == "pending",
                Video.scheduled_time.is_(None),
            )
            .order_by(Video.created_at.desc(), Video.id.desc())
            .limit(remaining_slots)
        )
        unscheduled_result = await session.execute(unscheduled_stmt)
        videos = unscheduled_result.scalars().all()
        if not videos:
            return

        pending_count = len(videos)
        if pending_count == 0:
            return

        # Stride-based uniform distribution. Example: 4 pending videos, 21
        # available slots -> stride = 21 // 4 = 5, picks slot[0], slot[5],
        # slot[10], slot[15]. When pending >= slots, stride collapses to 1
        # and we pack consecutively (packing fallback).
        if pending_count >= len(available_slots):
            stride = 1
        else:
            stride = max(1, len(available_slots) // pending_count)
        selected_slots: list[datetime] = []
        for i in range(pending_count):
            idx = i * stride
            if idx >= len(available_slots):
                break
            selected_slots.append(available_slots[idx])

        for video, slot_time in zip(videos, selected_slots):
            video.scheduled_time = slot_time + spread_offset
            logger.info(
                "[POSTING] Auto-scheduled video %d for %s at %s"
                " (slot=%s, spread=%ds, stride=%d, horizon_days=%d)",
                video.id, account.username, video.scheduled_time.isoformat(),
                slot_time.isoformat(), int(spread_offset.total_seconds()),
                stride, horizon_days,
            )

    def _compute_slots(
        self,
        times_str: list[str],
        now: datetime,
        jitter_std: int,
        timezone_name: str | None,
        days: int = 2,
    ) -> list[datetime]:
        """Compute future posting time slots (with jitter).

        Returns all future slots across the next ``days`` days starting
        from local-today. The caller decides how many of those slots to
        consume for the current batch of pending videos.
        """
        return farm_time.compute_future_slots_utc(
            times_str=times_str,
            now_utc=now,
            timezone_name=timezone_name,
            jitter_std=jitter_std,
            days=days,
        )

    # ------------------------------------------------------------------
    # Job: auto_start_engagement (every 5 min)
    # ------------------------------------------------------------------

    async def auto_start_engagement(self) -> None:
        """Auto-start engagement sessions for enabled accounts."""
        now = _utcnow()

        async with self.session_factory() as session:
            # Find enabled, active, unpaused, unblocked accounts
            stmt = select(Account).where(
                Account.engagement_enabled.is_(True),
                Account.is_active.is_(True),
                Account.is_paused.is_(False),
                Account.is_blocked.is_(False),
            )
            result = await session.execute(stmt)
            accounts = result.scalars().all()

        if not accounts:
            return

        for account in accounts:
            try:
                await self._start_engagement_for_account(account, now)
            except Exception:
                logger.exception(
                    "[ENGAGEMENT] Failed to start engagement for %s",
                    account.username,
                )

    async def _start_engagement_for_account(
        self, account: Account, now: datetime,
    ) -> None:
        """Attempt to start an engagement session for a single account."""
        username = account.username
        sessions_per_day = account.engagement_sessions_day or 1

        async with self.session_factory() as session:
            # Skip if already has a running session
            running_stmt = select(func.count()).select_from(EngagementSession).where(
                EngagementSession.account_username == username,
                EngagementSession.status == "running",
            )
            running_count = (await session.execute(running_stmt)).scalar() or 0
            if running_count > 0:
                logger.debug(
                    "[ENGAGEMENT] %s already has a running session — skip", username,
                )
                return

            # Check cooldown: last_engagement_at must be older than 1 hour
            if account.last_engagement_at is not None:
                cooldown_hours = 1.0
                elapsed = now - account.last_engagement_at
                if elapsed < timedelta(hours=cooldown_hours):
                    logger.debug(
                        "[ENGAGEMENT] %s cooldown not elapsed (%.0fm ago) — skip",
                        username, elapsed.total_seconds() / 60,
                    )
                    return

            # Check daily session count — anchored on farm-local
            # midnight, not raw UTC (Codex iter 5 Q5 2026-04-14).
            # Previously `now.replace(hour=0, ...)` produced UTC
            # midnight, which drifted the per-account daily cap by
            # several hours from the operator's sense of "today"
            # on UTC+N VPSs.
            today_start, _today_end = farm_time.local_day_bounds_utc(
                now, self.config.farm_timezone,
            )
            today_count_stmt = select(func.count()).select_from(EngagementSession).where(
                EngagementSession.account_username == username,
                EngagementSession.started_at >= today_start,
            )
            today_count = (await session.execute(today_count_stmt)).scalar() or 0
            if today_count >= sessions_per_day:
                logger.debug(
                    "[ENGAGEMENT] %s reached daily limit (%d/%d) — skip",
                    username, today_count, sessions_per_day,
                )
                return

            # Resolve device
            device_id = await self._resolve_device_id(session, username)
            if device_id is None:
                logger.debug("[ENGAGEMENT] No device for %s — skip", username)
                return

            if not self.ws_manager.is_online(device_id):
                logger.debug("[ENGAGEMENT] Device %d offline for %s — skip", device_id, username)
                return

            # Skip if DEVICE already has a running engagement session
            # (only one engagement can run per device — new one aborts the old)
            device_running_stmt = select(func.count()).select_from(EngagementSession).where(
                EngagementSession.device_id == device_id,
                EngagementSession.status == "running",
            )
            device_running = (await session.execute(device_running_stmt)).scalar() or 0
            if device_running > 0:
                logger.debug(
                    "[ENGAGEMENT] Device %d already running engagement — skip %s",
                    device_id, username,
                )
                return

            # Load engagement targets and SHUFFLE before dispatch.
            #
            # Without shuffle the phone iterates `channels[currentChannelIndex]`
            # starting from index 0 (EngagementStateMachine.kt:228) and the
            # 30-min daily budget expires after ~15 channels — meaning
            # channels[15..N-1] never get visited. Shuffling per-session
            # gives every target an equal chance of being touched over a
            # week and breaks the deterministic "always engages with the
            # same first 15 accounts" pattern that's a behavioral fingerprint.
            #
            # Note: we shuffle the LIST of EngagementTarget rows, not their
            # IDs — the phone consumes the list verbatim in the order we
            # send it. Done in Python (random.shuffle) instead of SQL
            # ORDER BY RANDOM() so the seed is fresh per call rather than
            # something the SQLite query planner could memoize.
            targets_stmt = select(EngagementTarget).where(
                EngagementTarget.account_username == username,
                EngagementTarget.is_active.is_(True),
            )
            targets_result = await session.execute(targets_stmt)
            targets = list(targets_result.scalars().all())
            random.shuffle(targets)

        # Build payload matching phone contract (camelCase)
        payload = {
            "accountUsername": account.username,
            "channels": [
                {
                    "targetUsername": t.target_username,
                    "maxReels": t.max_reels,
                    "shouldFollow": t.should_follow,
                }
                for t in targets
            ],
            "dailyBudgetMinutes": account.engagement_daily_budget if account.engagement_daily_budget is not None else 30,
            "actionProbabilities": {
                "likeProbability": account.engagement_like_prob if account.engagement_like_prob is not None else 0.7,
                "commentProbability": (
                    account.engagement_comment_prob
                    if account.engagement_comment_prob is not None
                    else 0.3
                ),
                "replyProbability": account.engagement_reply_prob if account.engagement_reply_prob is not None else 0.1,
                "shareProbability": account.engagement_share_prob if account.engagement_share_prob is not None else 0.05,
            },
            "timings": {
                "watchMinMs": 3000,
                "watchMaxMs": 8000,
                "actionCooldownMinMs": 1000,
                "actionCooldownMaxMs": 3000,
                "channelCooldownMinMs": 5000,
                "channelCooldownMaxMs": 15000,
            },
            "llmEndpoint": self._build_public_url(),
            "useVisionLlm": True,
            "interested": 0.0,
        }

        # Send command to device (outside DB session)
        try:
            await self.bridge.start_engagement(device_id, payload)
        except Exception as exc:
            logger.error(
                "[ENGAGEMENT] Bridge start_engagement failed for %s on device %d: %s",
                username, device_id, exc,
            )
            return

        # Create session record and update account
        async with self.session_factory() as session:
            eng_session = EngagementSession(
                device_id=device_id,
                account_username=username,
                status="running",
                started_at=now,
            )
            session.add(eng_session)

            acct_stmt = select(Account).where(Account.username == username)
            acct_result = await session.execute(acct_stmt)
            acct = acct_result.scalar_one_or_none()
            if acct is not None:
                acct.last_engagement_at = now

            await session.commit()

        logger.info(
            "[ENGAGEMENT] Started engagement session for %s on device %d",
            username, device_id,
        )

    # ------------------------------------------------------------------
    # Job: collect_engagement_results (every 30s)
    # ------------------------------------------------------------------

    async def collect_engagement_results(self) -> None:
        """Poll running engagement sessions and sync actions from devices."""
        # Find running sessions
        async with self.session_factory() as session:
            stmt = select(EngagementSession).where(
                EngagementSession.status == "running",
            )
            result = await session.execute(stmt)
            running_sessions = result.scalars().all()

        if not running_sessions:
            return

        # Check if engagement was disabled — abort running sessions on device
        async with self.session_factory() as session:
            for eng_session in running_sessions:
                acct = (await session.execute(
                    select(Account).where(Account.username == eng_session.account_username),
                )).scalar_one_or_none()
                if acct is not None and not acct.engagement_enabled:
                    device_id, error = await abort_engagement_on_device(
                        account_username=eng_session.account_username,
                        session=session,
                        bridge=self.bridge,
                        ws=self.ws_manager,
                        device_id=eng_session.device_id,
                    )
                    if error is not None:
                        logger.warning(
                            "[ENGAGEMENT] Could not abort session %d for %s: %s",
                            eng_session.id, eng_session.account_username, error,
                        )
                        continue
                    logger.info(
                        "[ENGAGEMENT] Sent abort to device %d (engagement disabled for %s)",
                        device_id, eng_session.account_username,
                    )
                    eng_session.status = "aborted"
                    eng_session.finished_at = _utcnow()
                    await session.commit()

        # Re-fetch after potential aborts
        async with self.session_factory() as session:
            running_sessions = (await session.execute(
                select(EngagementSession).where(EngagementSession.status == "running"),
            )).scalars().all()

        if not running_sessions:
            return

        any_update = False
        for eng_session in running_sessions:
            try:
                updated = await self._collect_engagement_from_session(eng_session)
                if updated:
                    any_update = True
            except Exception:
                logger.exception(
                    "[ENGAGEMENT] Result collection failed for session %d",
                    eng_session.id,
                )

        if any_update:
            await self.broadcaster.broadcast("engagement:update", {
                "ts": int(time.time() * 1000),
            })

    async def _collect_engagement_from_session(
        self, eng_session: EngagementSession,
    ) -> bool:
        """Collect actions for a single running engagement session. Returns True if any update."""
        device_id = eng_session.device_id
        if device_id is None or not self.ws_manager.is_online(device_id):
            return False

        session_id = eng_session.id
        since_ms = self._last_engagement_poll_ms.get(device_id, 0)

        # Poll actions from device
        try:
            actions_data = await self.bridge.get_engagement_actions(
                device_id, since_ms=since_ms,
            )
        except Exception as exc:
            logger.warning(
                "[ENGAGEMENT] get_engagement_actions failed for device %d: %s",
                device_id, exc,
            )
            return False

        actions: list[dict[str, Any]] = actions_data.get("actions", [])
        max_ts = since_ms

        # Upsert actions
        if actions:
            async with self.session_factory() as session:
                for entry in actions:
                    phone_action_id = entry.get("id")
                    entry_ts = entry.get("timestamp", 0)
                    if entry_ts > max_ts:
                        max_ts = entry_ts

                    # Deduplicate by phone_action_id
                    if phone_action_id is not None:
                        dup_stmt = select(EngagementAction.id).where(
                            EngagementAction.phone_action_id == phone_action_id,
                            EngagementAction.device_id == device_id,
                        )
                        dup = await session.execute(dup_stmt)
                        if dup.scalar_one_or_none() is not None:
                            continue

                    action = EngagementAction(
                        phone_action_id=phone_action_id,
                        session_id=session_id,
                        device_id=device_id,
                        account_username=eng_session.account_username,
                        target_username=entry.get("targetUsername"),
                        action_type=entry.get("actionType", "unknown"),
                        reel_caption=entry.get("reelCaption"),
                        comment_text=entry.get("commentText"),
                        success=entry.get("success", True),
                        performed_at=datetime.utcfromtimestamp(entry_ts / 1000)
                        if entry_ts
                        else None,
                    )
                    session.add(action)

                await session.commit()

            self._last_engagement_poll_ms[device_id] = max_ts

        # Check if engagement is still active on device
        try:
            status_data = await self.bridge.get_engagement_status(device_id)
        except Exception as exc:
            logger.warning(
                "[ENGAGEMENT] get_engagement_status failed for device %d: %s",
                device_id, exc,
            )
            return bool(actions)

        is_active = status_data.get("active", False)

        # Update live stats from device status (regardless of completion)
        async with self.session_factory() as session:
            es_stmt = select(EngagementSession).where(
                EngagementSession.id == session_id,
            )
            es_result = await session.execute(es_stmt)
            es = es_result.scalar_one_or_none()
            if es is not None:
                es.channels_visited = status_data.get("channelsVisited", es.channels_visited or 0)
                es.reels_watched = status_data.get("reelsWatched", es.reels_watched or 0)
                es.total_likes = status_data.get("totalLikes", es.total_likes or 0)
                es.total_comments = status_data.get("totalComments", es.total_comments or 0)

                if not is_active and es.status == "running":
                    es.status = "completed"
                    es.finished_at = _utcnow()

                    # Aggregate action stats from DB actions for final counts
                    stats = await self._aggregate_engagement_stats(session, session_id)
                    es.total_likes = max(es.total_likes or 0, stats.get("likes", 0))
                    es.total_comments = max(es.total_comments or 0, stats.get("comments", 0))
                    es.total_replies = stats.get("replies", 0)
                    es.total_shares = stats.get("shares", 0)
                    es.total_follows = stats.get("follows", 0)
                    es.reels_watched = max(es.reels_watched or 0, stats.get("total", 0))

                    if es.started_at is not None:
                        es.duration_ms = int(
                            (es.finished_at - es.started_at).total_seconds() * 1000,
                        )

                    logger.info(
                        "[ENGAGEMENT] Session %d for %s completed "
                        "(likes=%d, comments=%d, duration=%dms)",
                        session_id, es.account_username,
                        es.total_likes, es.total_comments, es.duration_ms or 0,
                    )

            await session.commit()

        if not is_active:
            return True

        return bool(actions)

    async def _aggregate_engagement_stats(
        self, session: AsyncSession, session_id: int,
    ) -> dict[str, int]:
        """Aggregate action counts for a completed engagement session."""
        stmt = select(EngagementAction).where(
            EngagementAction.session_id == session_id,
            EngagementAction.success.is_(True),
        )
        result = await session.execute(stmt)
        actions = result.scalars().all()

        stats: dict[str, int] = {
            "likes": 0, "comments": 0, "replies": 0,
            "shares": 0, "follows": 0, "total": 0,
        }
        for a in actions:
            stats["total"] += 1
            at = a.action_type
            if at == "like":
                stats["likes"] += 1
            elif at == "comment":
                stats["comments"] += 1
            elif at == "reply":
                stats["replies"] += 1
            elif at == "share":
                stats["shares"] += 1
            elif at == "follow":
                stats["follows"] += 1

        return stats

    # ------------------------------------------------------------------
    # Job: auto_start_insights (every 10 min)
    # ------------------------------------------------------------------

    async def refresh_insights_plan_priorities(self) -> None:
        """Recompute plan priority scores from staleness, volatility, and age."""
        now = _utcnow()
        halflife_hours = max(float(self.config.insights_freshness_halflife_hours or 0.0), 0.0)
        volatility_weight = float(self.config.insights_volatility_weight or 0.0)

        async with self.session_factory() as session:
            rows = (await session.execute(
                select(InsightsCollectionPlan, Video)
                .join(Video, Video.id == InsightsCollectionPlan.video_id)
            )).all()

            for plan, video in rows:
                if video.insights_retired:
                    continue

                age_hours = self._video_age_hours(video, now)
                interval_hours = self._pick_decay_bracket(age_hours)
                plan.target_interval_hours = interval_hours

                overdue_seconds = max(
                    0.0,
                    (now - plan.next_due_at).total_seconds(),
                )
                plan.last_staleness_ms = int(overdue_seconds * 1000)

                if video.insights_last_collected_at is None:
                    staleness = 1.0
                elif overdue_seconds > 0:
                    staleness = 1.0 + (overdue_seconds / max(interval_hours * 3600.0, 1.0))
                else:
                    staleness = 0.0

                recent_plays = list((await session.execute(
                    select(InsightsSnapshot.plays)
                    .where(
                        InsightsSnapshot.video_id == video.id,
                        InsightsSnapshot.plays.is_not(None),
                    )
                    .order_by(InsightsSnapshot.collected_at.desc(), InsightsSnapshot.id.desc())
                    .limit(2)
                )).scalars().all())
                volatility = 0.0
                if len(recent_plays) >= 2 and recent_plays[0] is not None and recent_plays[1] is not None:
                    volatility = abs(recent_plays[0] - recent_plays[1]) / max(recent_plays[1], 1)

                freshness_boost = (
                    math.exp(-(age_hours / halflife_hours))
                    if halflife_hours > 0
                    else 0.0
                )
                plan.priority_score = (
                    staleness
                    * (1.0 + (volatility_weight * volatility))
                    * (1.0 + freshness_boost)
                )

            await session.commit()

    def _build_insights_payload(
        self,
        account: Account,
        videos: list[Video],
    ) -> dict[str, Any]:
        max_reels = max(
            1,
            int(account.insights_max_reels or self.config.insights_reels_per_session or 1),
        )
        target_videos = videos[:max_reels]
        known_video_ids = [video.id for video in target_videos]
        return {
            "accounts": [
                {
                    "username": account.username,
                    "targetReels": [
                        {
                            "videoId": video.id,
                            "positionHint": int(video.insights_last_position or 0),
                            "captionHash": video.insights_caption_hash or "",
                            "marker": video.insights_video_marker or "",
                        }
                        for video in target_videos
                    ],
                },
            ],
            "sessionId": str(uuid.uuid4()),
            "maxReelsPerAccount": max_reels,
            "knownVideoIds": known_video_ids,
        }

    async def auto_start_insights(self) -> None:
        """Start at most one plan-driven insights session on an online device."""
        online_device_ids = list(self.ws_manager.get_online_device_ids())
        if not online_device_ids:
            return

        now = _utcnow()
        coalesce_cutoff = now + timedelta(
            minutes=self.config.insights_coalesce_window_minutes,
        )
        top_k = max(1, int(self.config.insights_reels_per_session or 1))
        min_gap = timedelta(minutes=self.config.insights_min_session_gap_minutes)

        for device_id in online_device_ids:
            try:
                status = await self.bridge.get_status(device_id)
                if status.get("activeMode", "NONE") != "NONE":
                    continue
            except Exception as exc:
                logger.warning(
                    "[INSIGHTS] Status check failed for device %d: %s",
                    device_id, exc,
                )
                continue

            async with self.session_factory() as session:
                if await self._device_has_imminent_posting(session, device_id, now):
                    continue

                rows = (await session.execute(
                    select(InsightsCollectionPlan, Video, Account)
                    .join(Video, Video.id == InsightsCollectionPlan.video_id)
                    .join(
                        AccountDevice,
                        and_(
                            AccountDevice.account_username == InsightsCollectionPlan.account_username,
                            AccountDevice.device_id == device_id,
                            AccountDevice.is_primary.is_(True),
                        ),
                    )
                    .join(Account, Account.username == InsightsCollectionPlan.account_username)
                    .where(
                        Video.status == "posted",
                        Video.insights_retired.is_(False),
                        Account.insights_enabled.is_(True),
                        Account.is_active.is_(True),
                        Account.is_paused.is_(False),
                        Account.is_blocked.is_(False),
                        or_(
                            InsightsCollectionPlan.next_due_at <= coalesce_cutoff,
                            InsightsCollectionPlan.priority_score >= 1.0,
                        ),
                    )
                    .order_by(
                        InsightsCollectionPlan.priority_score.desc(),
                        InsightsCollectionPlan.next_due_at.asc(),
                    )
                    .limit(top_k)
                )).all()

                if not rows:
                    continue

                grouped: dict[str, dict[str, Any]] = {}
                for plan, video, account in rows:
                    bucket = grouped.setdefault(account.username, {
                        "account": account,
                        "videos": [],
                        "count": 0,
                        "priority": 0.0,
                        "due_at": plan.next_due_at,
                        "plans": [],
                    })
                    bucket["videos"].append(video)
                    bucket["plans"].append(plan)
                    bucket["count"] += 1
                    bucket["priority"] += float(plan.priority_score or 0.0)
                    if plan.next_due_at < bucket["due_at"]:
                        bucket["due_at"] = plan.next_due_at

                winning_bucket = sorted(
                    grouped.values(),
                    key=lambda item: (
                        -item["count"],
                        -item["priority"],
                        item["due_at"],
                        item["account"].username,
                    ),
                )[0]
                account = winning_bucket["account"]
                if account.last_insights_at is not None and (now - account.last_insights_at) < min_gap:
                    continue

                payload = self._build_insights_payload(account, winning_bucket["videos"])

                try:
                    await self.bridge.start_insights(device_id, payload)
                except Exception as exc:
                    logger.error(
                        "[INSIGHTS] Bridge start_insights failed for %s on device %d: %s",
                        account.username, device_id, exc,
                    )
                    continue

                account.last_insights_at = now
                for plan in winning_bucket["plans"]:
                    plan.consecutive_failures = (plan.consecutive_failures or 0) + 1
                await session.commit()

                logger.info(
                    "[INSIGHTS] Started plan-driven collection for %s on device %d (%d reels)",
                    account.username, device_id, len(payload["knownVideoIds"]),
                )
                break

    async def retire_ghost_reels(self) -> None:
        """Retire reels that are effectively dead or never surfaced."""
        now = _utcnow()
        dead_threshold = int(self.config.insights_dead_streak_threshold)
        missing_threshold = int(self.config.insights_missing_from_feed_threshold) + 1

        async with self.session_factory() as session:
            rows = (await session.execute(
                select(Video, InsightsCollectionPlan)
                .outerjoin(
                    InsightsCollectionPlan,
                    InsightsCollectionPlan.video_id == Video.id,
                )
                .where(
                    Video.status == "posted",
                    Video.insights_retired.is_(False),
                )
            )).all()

            retired_count = 0
            for video, plan in rows:
                reason: str | None = None
                if (video.insights_dead_streak or 0) >= dead_threshold:
                    reason = "dead_streak"
                elif (
                    plan is not None
                    and (video.insights_collection_count or 0) == 0
                    and (plan.consecutive_failures or 0) >= missing_threshold
                ):
                    reason = "missing_from_feed"
                if reason is None:
                    continue

                video.insights_retired = True
                video.insights_retired_at = now
                video.insights_retired_reason = reason
                if plan is not None:
                    await session.delete(plan)
                retired_count += 1

            if retired_count:
                logger.info("[INSIGHTS] Retired %d ghost reels", retired_count)
            await session.commit()

    async def prune_insights(self) -> None:
        """Downsample old raw insights into daily rollups and prune archives."""
        now = _utcnow()
        raw_cutoff = now - timedelta(days=self.config.insights_retention_full_days)
        daily_cutoff = _insights_day_bucket(now - timedelta(
            days=self.config.insights_retention_full_days + self.config.insights_retention_daily_days,
        ))

        async with self.session_factory() as session:
            raw_rows = list((await session.execute(
                select(InsightsSnapshot)
                .where(InsightsSnapshot.collected_at < raw_cutoff)
                .order_by(
                    InsightsSnapshot.video_id.asc(),
                    InsightsSnapshot.collected_at.asc(),
                    InsightsSnapshot.id.asc(),
                )
            )).scalars().all())

            grouped: dict[tuple[int, datetime], list[InsightsSnapshot]] = {}
            for snapshot in raw_rows:
                if snapshot.video_id is None or snapshot.collected_at is None:
                    continue
                bucket_key = (snapshot.video_id, _insights_day_bucket(snapshot.collected_at))
                grouped.setdefault(bucket_key, []).append(snapshot)

            for (video_id, bucket_day), samples in grouped.items():
                first = samples[0]
                last = samples[-1]
                daily = (await session.execute(
                    select(InsightsSnapshotDaily).where(
                        InsightsSnapshotDaily.video_id == video_id,
                        InsightsSnapshotDaily.date == bucket_day,
                    )
                )).scalar_one_or_none()
                if daily is None:
                    daily = InsightsSnapshotDaily(video_id=video_id, date=bucket_day)
                    session.add(daily)

                daily.plays_end_of_day = last.plays
                daily.likes_delta = _delta_or_zero(first.likes, last.likes)
                daily.comments_delta = _delta_or_zero(first.comments, last.comments)
                daily.shares_delta = _delta_or_zero(first.shares, last.shares)
                daily.saves_delta = _delta_or_zero(first.saves, last.saves)
                daily.reach_delta = _delta_or_zero(first.reach, last.reach)
                daily.avg_watch_time_seconds = last.avg_watch_time_seconds
                daily.skip_rate_percent = last.skip_rate_percent

            for snapshot in raw_rows:
                await session.delete(snapshot)

            stale_daily_rows = list((await session.execute(
                select(InsightsSnapshotDaily).where(
                    InsightsSnapshotDaily.date < daily_cutoff,
                )
            )).scalars().all())
            for row in stale_daily_rows:
                await session.delete(row)

            await session.commit()

    async def _legacy_auto_start_insights_DISABLED_2(self) -> None:
        """Auto-trigger insights collection for enabled accounts."""
        now = _utcnow()

        async with self.session_factory() as session:
            stmt = select(Account).where(
                Account.insights_enabled.is_(True),
                Account.is_active.is_(True),
                Account.is_paused.is_(False),
                Account.is_blocked.is_(False),
            )
            result = await session.execute(stmt)
            accounts = result.scalars().all()

        if not accounts:
            return

        for account in accounts:
            try:
                await self._start_insights_for_account(account, now)
            except Exception:
                logger.exception(
                    "[INSIGHTS] Failed to start insights for %s",
                    account.username,
                )

    async def _legacy_start_insights_for_account_DISABLED(
        self, account: Account, now: datetime,
    ) -> None:
        """Attempt to start insights collection for a single account."""
        username = account.username
        interval_hours = account.insights_interval_hours or 6.0

        # Check cooldown
        if account.last_insights_at is not None:
            elapsed = now - account.last_insights_at
            if elapsed < timedelta(hours=interval_hours):
                logger.debug(
                    "[INSIGHTS] %s interval not elapsed (%.1fh ago, need %.1fh) — skip",
                    username,
                    elapsed.total_seconds() / 3600,
                    interval_hours,
                )
                return

        async with self.session_factory() as session:
            # Resolve device
            device_id = await self._resolve_device_id(session, username)

        if device_id is None:
            logger.debug("[INSIGHTS] No device for %s — skip", username)
            return

        if not self.ws_manager.is_online(device_id):
            logger.debug("[INSIGHTS] Device %d offline for %s — skip", device_id, username)
            return

        # Check no other FSM is active on the device (optional: best-effort)
        try:
            status = await self.bridge.get_status(device_id)
            active_mode = status.get("activeMode", "NONE")
            if active_mode != "NONE":
                logger.debug(
                    "[INSIGHTS] Device %d busy (mode=%s) for %s — skip",
                    device_id, active_mode, username,
                )
                return
        except Exception as exc:
            logger.warning(
                "[INSIGHTS] Status check failed for device %d (%s) — proceeding anyway: %s",
                device_id, username, exc,
            )

        # Build payload
        max_reels = account.insights_max_reels or 10
        payload = {
            "accounts": [
                {
                    "username": username,
                    "knownVideoIds": [],
                    "skipReels": 0,
                },
            ],
            "maxReelsPerAccount": max_reels,
        }

        # Send command to device
        try:
            await self.bridge.start_insights(device_id, payload)
        except Exception as exc:
            logger.error(
                "[INSIGHTS] Bridge start_insights failed for %s on device %d: %s",
                username, device_id, exc,
            )
            return

        # Update account
        async with self.session_factory() as session:
            acct_stmt = select(Account).where(Account.username == username)
            acct_result = await session.execute(acct_stmt)
            acct = acct_result.scalar_one_or_none()
            if acct is not None:
                acct.last_insights_at = now
            await session.commit()

        logger.info(
            "[INSIGHTS] Started insights collection for %s on device %d",
            username, device_id,
        )

    # ------------------------------------------------------------------
    # Job: collect_insights_results (every 30s)
    # ------------------------------------------------------------------

    async def collect_insights_results(self) -> None:
        """Poll devices running insights collection and sync snapshots."""
        online_ids = self.ws_manager.get_online_device_ids()
        if not online_ids:
            return

        any_update = False
        for device_id in online_ids:
            try:
                updated = await self._collect_insights_from_device(device_id)
                if updated:
                    any_update = True
            except Exception:
                logger.exception(
                    "[INSIGHTS] Result collection failed for device %d", device_id,
                )

        if any_update:
            await self.broadcaster.broadcast("insights:update", {
                "ts": int(time.time() * 1000),
            })

    async def _collect_insights_from_device(self, device_id: int) -> bool:
        """Collect insights from a single device. Returns True if any update."""
        # Check if insights collection is active on this device
        try:
            status_data = await self.bridge.get_insights_status(device_id)
        except Exception:
            return False

        is_active = status_data.get("active", False)
        if not is_active:
            return False

        since_ms = self._last_insights_poll_ms.get(device_id, 0)

        try:
            data = await self.bridge.get_insights(device_id, since_ms=since_ms)
        except Exception as exc:
            logger.warning(
                "[INSIGHTS] get_insights failed for device %d: %s", device_id, exc,
            )
            return False

        snapshots: list[dict[str, Any]] = data.get("snapshots", [])
        if not snapshots:
            return False

        max_ts = since_ms
        async with self.session_factory() as session:
            for entry in snapshots:
                phone_snapshot_id = entry.get("id")
                entry_ts = entry.get("timestamp", 0)
                if entry_ts > max_ts:
                    max_ts = entry_ts

                # Deduplicate by phone_snapshot_id
                if phone_snapshot_id is not None:
                    dup_stmt = select(InsightsSnapshot.id).where(
                        InsightsSnapshot.phone_snapshot_id == phone_snapshot_id,
                        InsightsSnapshot.device_id == device_id,
                    )
                    dup = await session.execute(dup_stmt)
                    if dup.scalar_one_or_none() is not None:
                        continue

                snapshot = InsightsSnapshot(
                    phone_snapshot_id=phone_snapshot_id,
                    device_id=device_id,
                    # Phone sends `accountUsername` (Android camelCase
                    # convention, matches post_log path at L2196);
                    # keep `username` as legacy fallback.
                    account_username=(
                        entry.get("accountUsername")
                        or entry.get("username", "")
                    ),
                    caption_snippet=entry.get("captionSnippet"),
                    reel_position=entry.get("reelPosition"),
                    plays=entry.get("plays"),
                    likes=entry.get("likes"),
                    comments=entry.get("comments"),
                    shares=entry.get("shares"),
                    saves=entry.get("saves"),
                    reposts=entry.get("reposts"),
                    reach=entry.get("reach"),
                    engaged=entry.get("engaged"),
                    profile_visits=entry.get("profileVisits"),
                    follows=entry.get("follows"),
                    watch_time_seconds=entry.get("watchTimeSeconds"),
                    avg_watch_time_seconds=entry.get("avgWatchTimeSeconds"),
                    skip_rate_percent=entry.get("skipRatePercent"),
                    followers_percent=entry.get("followersPercent"),
                    non_followers_percent=entry.get("nonFollowersPercent"),
                    retention_curve_json=(
                        entry.get("retentionCurveJson")
                        if isinstance(entry.get("retentionCurveJson"), str)
                        else json.dumps(entry.get("retentionCurve"))
                        if entry.get("retentionCurve") is not None
                        else None
                    ),
                    insights_screen_hash=entry.get("insightsScreenHash") or entry.get("screenHash"),
                    collected_at=(
                        datetime.fromtimestamp(entry_ts / 1000, tz=timezone.utc).replace(tzinfo=None)
                        if entry_ts
                        else _utcnow()
                    ),
                )
                published_label = entry.get("publishedLabel") or entry.get("published_label")

                await self._update_video_stats(
                    session,
                    snapshot,
                    published_label=published_label,
                )
                session.add(snapshot)

            await session.commit()

        self._last_insights_poll_ms[device_id] = max_ts
        return True

    async def _update_video_stats(
        self,
        session: AsyncSession,
        snapshot: InsightsSnapshot,
        *,
        published_label: str | None = None,
    ) -> None:
        """Match a snapshot to a video, update audit fields, and sync metrics."""
        video, strategy, confidence = await self._match_video(
            session,
            snapshot,
            published_label=published_label,
        )
        snapshot.match_strategy = strategy
        snapshot.match_confidence = confidence
        if video is None:
            snapshot.video_id = None
            return

        snapshot.video_id = video.id
        caption_hash = _sha256_text(snapshot.caption_snippet)
        marker_id = extract_watermark(snapshot.caption_snippet)
        marker = encode_watermark(marker_id) if marker_id is not None else None
        collected_at = snapshot.collected_at or _utcnow()

        has_metric_delta = self._snapshot_has_metric_delta(video, snapshot)
        if has_metric_delta:
            video.insights_dead_streak = 0
        else:
            video.insights_dead_streak = (video.insights_dead_streak or 0) + 1

        self._apply_snapshot_metrics(video, snapshot)
        video.insights_last_collected_at = collected_at
        if snapshot.reel_position is not None:
            video.insights_last_position = snapshot.reel_position

        if strategy in {"marker", "hash_label"}:
            if caption_hash:
                video.insights_caption_hash = caption_hash
            if published_label:
                video.insights_published_label = published_label
            if marker:
                video.insights_video_marker = marker
            video.insights_collection_count = (video.insights_collection_count or 0) + 1

        await self._upsert_insights_plan(
            session,
            video,
            anchor_at=collected_at,
            priority_score=0.0,
            reset_failures=True,
        )
