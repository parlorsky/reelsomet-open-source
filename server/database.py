"""Database utilities: engine, session factory, table creation."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from server.models import Base

logger = logging.getLogger(__name__)


def build_engine(database_path: str) -> AsyncEngine:
    """Create an async SQLAlchemy engine for the given SQLite path.

    Important detail (2026-04-25): SQLite's `PRAGMA busy_timeout` is
    set **per-connection**, not globally. Without a per-connect hook,
    every new aiosqlite connection from the pool starts with
    `busy_timeout=0` (no wait → "database is locked" on any
    contention with the scheduler's writer). The migration code at
    `_migrate()` only set the PRAGMA on its own one-shot connection.
    The hook below ensures every connection the pool opens uses the
    farm-wide values: WAL journal, 15 s busy wait, NORMAL synchronous
    (durability vs throughput tradeoff already chosen elsewhere).
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite+aiosqlite:///{path}"
    engine = create_async_engine(db_url, echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # 15 s busy timeout — covers the longest write paths in the
            # scheduler (insights snapshot upserts, post-log batch
            # writes). Anything longer is a real deadlock and should
            # surface as an error instead of hanging the request.
            cursor.execute("PRAGMA busy_timeout=15000")
            # WAL is also set in `_migrate()` at startup, but applying
            # it here is idempotent and protects fresh DBs whose first
            # connection happens before migration.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    """Create all tables from the ORM models."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified")
    await _migrate(engine)


async def _migrate(engine: AsyncEngine) -> None:
    """Run lightweight schema migrations (ALTER TABLE for new columns).

    Each migration is idempotent — safe to run on every startup.
    """
    migrations = [
        # v2: add posting_enabled to accounts (default True)
        ("accounts", "posting_enabled", "ALTER TABLE accounts ADD COLUMN posting_enabled BOOLEAN NOT NULL DEFAULT 1"),
        # v3: add phone_video_id to videos (maps VPS video → phone video for result matching)
        ("videos", "phone_video_id", "ALTER TABLE videos ADD COLUMN phone_video_id INTEGER"),
        # v4: Instagram credentials on accounts
        ("accounts", "ig_password", "ALTER TABLE accounts ADD COLUMN ig_password TEXT"),
        ("accounts", "ig_2fa_secret", "ALTER TABLE accounts ADD COLUMN ig_2fa_secret VARCHAR(64)"),
        # v5: Profile stats (scraped from Instagram)
        ("accounts", "followers", "ALTER TABLE accounts ADD COLUMN followers INTEGER NOT NULL DEFAULT 0"),
        ("accounts", "following", "ALTER TABLE accounts ADD COLUMN following INTEGER NOT NULL DEFAULT 0"),
        ("accounts", "posts_count", "ALTER TABLE accounts ADD COLUMN posts_count INTEGER NOT NULL DEFAULT 0"),
        # v6: Carousel support
        ("videos", "content_type", "ALTER TABLE videos ADD COLUMN content_type VARCHAR(32) NOT NULL DEFAULT 'reel'"),
        ("videos", "image_filenames", "ALTER TABLE videos ADD COLUMN image_filenames TEXT DEFAULT NULL"),
        # v7: Story interactive elements
        ("videos", "story_element", "ALTER TABLE videos ADD COLUMN story_element TEXT DEFAULT NULL"),
        # v8: Caption seeds — model scoping (each model has its own caption seed pool)
        ("caption_seeds", "model", "ALTER TABLE caption_seeds ADD COLUMN model VARCHAR(64) DEFAULT NULL"),
        # v9: Per-account post cap on photo sets (NULL = fall back to
        # global cooldown carousel_set_cooldown_days; 1 = one-shot never
        # reuse; any N = post at most N times per account ever)
        ("photo_sets", "max_uses_per_account", "ALTER TABLE photo_sets ADD COLUMN max_uses_per_account INTEGER DEFAULT NULL"),
        # v10: honest dispatch/post timestamps on photo_set_usage (T1).
        ("photo_set_usage", "device_id", "ALTER TABLE photo_set_usage ADD COLUMN device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL"),
        ("photo_set_usage", "dispatched_at", "ALTER TABLE photo_set_usage ADD COLUMN dispatched_at DATETIME DEFAULT NULL"),
        ("photo_set_usage", "posted_at", "ALTER TABLE photo_set_usage ADD COLUMN posted_at DATETIME DEFAULT NULL"),
        # v11: Per-account story cadence overrides (T4).
        ("accounts", "story_max_per_day", "ALTER TABLE accounts ADD COLUMN story_max_per_day INTEGER"),
        ("accounts", "story_element_probability", "ALTER TABLE accounts ADD COLUMN story_element_probability REAL"),
        ("accounts", "story_element_weights", "ALTER TABLE accounts ADD COLUMN story_element_weights TEXT"),
        # v12: Per-account raw-video mode (T8).
        ("accounts", "use_scenarios", "ALTER TABLE accounts ADD COLUMN use_scenarios BOOLEAN"),
        # v13: Per-account story wall-clock schedule (story dispatcher, 2026-04-14).
        ("accounts", "story_posting_times", "ALTER TABLE accounts ADD COLUMN story_posting_times TEXT"),

        # v14: Insights overhaul (2026-04-24) — identity + lifecycle on Video.
        # See server/models.py Video class for the design rationale.
        ("videos", "insights_video_marker", "ALTER TABLE videos ADD COLUMN insights_video_marker VARCHAR(48)"),
        ("videos", "insights_caption_hash", "ALTER TABLE videos ADD COLUMN insights_caption_hash VARCHAR(64)"),
        ("videos", "insights_published_label", "ALTER TABLE videos ADD COLUMN insights_published_label VARCHAR(128)"),
        ("videos", "insights_published_at", "ALTER TABLE videos ADD COLUMN insights_published_at DATETIME"),
        ("videos", "insights_retired", "ALTER TABLE videos ADD COLUMN insights_retired BOOLEAN NOT NULL DEFAULT 0"),
        ("videos", "insights_retired_at", "ALTER TABLE videos ADD COLUMN insights_retired_at DATETIME"),
        ("videos", "insights_retired_reason", "ALTER TABLE videos ADD COLUMN insights_retired_reason TEXT"),
        ("videos", "insights_last_position", "ALTER TABLE videos ADD COLUMN insights_last_position INTEGER"),
        ("videos", "insights_last_collected_at", "ALTER TABLE videos ADD COLUMN insights_last_collected_at DATETIME"),
        ("videos", "insights_collection_count", "ALTER TABLE videos ADD COLUMN insights_collection_count INTEGER NOT NULL DEFAULT 0"),
        ("videos", "insights_dead_streak", "ALTER TABLE videos ADD COLUMN insights_dead_streak INTEGER NOT NULL DEFAULT 0"),

        # v14: InsightsSnapshot — identity audit + screen-hash drift detector.
        ("insights_snapshots", "match_strategy", "ALTER TABLE insights_snapshots ADD COLUMN match_strategy VARCHAR(24)"),
        ("insights_snapshots", "match_confidence", "ALTER TABLE insights_snapshots ADD COLUMN match_confidence REAL"),
        ("insights_snapshots", "retention_curve_json", "ALTER TABLE insights_snapshots ADD COLUMN retention_curve_json TEXT"),
        ("insights_snapshots", "insights_screen_hash", "ALTER TABLE insights_snapshots ADD COLUMN insights_screen_hash VARCHAR(64)"),
        # Legacy safety — some older DBs may be missing these (ORM had them, _migrate didn't)
        ("insights_snapshots", "watch_time_seconds", "ALTER TABLE insights_snapshots ADD COLUMN watch_time_seconds REAL"),
        ("insights_snapshots", "avg_watch_time_seconds", "ALTER TABLE insights_snapshots ADD COLUMN avg_watch_time_seconds REAL"),
        ("insights_snapshots", "skip_rate_percent", "ALTER TABLE insights_snapshots ADD COLUMN skip_rate_percent REAL"),
    ]
    async with engine.begin() as conn:
        for table, column, ddl in migrations:
            # PRAGMA cannot accept bound parameters, so the table
            # name is f-string-interpolated. Today the values are
            # all hardcoded literals from the `migrations` list,
            # but make the safety invariant explicit so a future
            # migration that accidentally interpolates a variable
            # breaks loudly instead of silently bypassing
            # parameterisation (Codex iter 10 hardening).
            assert table.isidentifier() and len(table) <= 64, (
                f"Migration table name must be a plain identifier: {table!r}"
            )
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            columns = [row[1] for row in result.fetchall()]
            if column not in columns:
                await conn.execute(text(ddl))
                logger.info("Migration: added %s.%s", table, column)


    # Create new tables if they don't exist (idempotent)
    new_tables = [
        """CREATE TABLE IF NOT EXISTS photo_sets (
            id INTEGER PRIMARY KEY,
            uuid VARCHAR(36) NOT NULL UNIQUE,
            name VARCHAR(256) NOT NULL,
            model VARCHAR(64),
            tags TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS photo_set_images (
            id INTEGER PRIMARY KEY,
            set_id INTEGER NOT NULL REFERENCES photo_sets(id) ON DELETE CASCADE,
            filename VARCHAR(512) NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS photo_set_usage (
            id INTEGER PRIMARY KEY,
            set_id INTEGER NOT NULL REFERENCES photo_sets(id),
            video_id INTEGER NOT NULL REFERENCES videos(id),
            account_username VARCHAR(128) NOT NULL,
            used_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS caption_seeds (
            id INTEGER PRIMARY KEY,
            model VARCHAR(64),
            category VARCHAR(64) NOT NULL,
            seed_prompt TEXT NOT NULL,
            language VARCHAR(8) DEFAULT 'en',
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        # Advanced logging step 1 (Codex roadmap): canonical farm_logs table.
        # Single source of truth for all structured events from VPS scheduler,
        # WS bridge, and Android FSMs (forwarded via event.log batches).
        # ts/ingest_ts in epoch milliseconds. fields_json holds structured extras.
        # The UNIQUE(device_id, event_id) index dedupes resubmits during WS reconnects.
        """CREATE TABLE IF NOT EXISTS farm_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            ingest_ts INTEGER NOT NULL,
            level TEXT NOT NULL,
            source TEXT NOT NULL,
            activity TEXT NOT NULL DEFAULT 'GENERIC',
            device_id INTEGER,
            account TEXT,
            trace_id TEXT,
            message TEXT NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '{}'
        )""",
        # Phones page (2026-05-06): per-(device, fsm_kind) live state row.
        # Updated on every POSTING_STATE event ingested by fsm_state.py.
        # Single source of truth for "what is each phone doing right now"
        # — the Phones admin page reads this for the live-state pane and
        # joins to farm_logs (by trace_id) for the per-step timeline.
        """CREATE TABLE IF NOT EXISTS device_fsm_state (
            device_id        INTEGER NOT NULL,
            fsm_kind         TEXT    NOT NULL,
            current_state    TEXT    NOT NULL,
            state_entered_at INTEGER NOT NULL,
            trace_id         TEXT,
            account          TEXT,
            task_id          TEXT,
            last_event_id    TEXT    NOT NULL,
            last_message     TEXT,
            updated_at       INTEGER NOT NULL,
            PRIMARY KEY (device_id, fsm_kind)
        )""",
        # Anti-collision LRU for ghost pipeline iPhone donors (Option E).
        # (model_key, row_index) references flat positions in the donor pool
        # cache in ghostcli.profiles. Rows expire after 72h and are dropped
        # by FarmScheduler.prune_donor_usage.
        """CREATE TABLE IF NOT EXISTS donor_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
            donor_model TEXT NOT NULL,
            donor_index INTEGER NOT NULL,
            used_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        # Story asset catalog: single-file (photo or video) entries that
        # the scheduler picks one per account per generation cycle to
        # produce story posts. Parallel to PhotoSet (carousels) but
        # strictly one media file per row.
        """CREATE TABLE IF NOT EXISTS story_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename VARCHAR(512) NOT NULL,
            media_type VARCHAR(16) NOT NULL,
            model VARCHAR(64),
            tags TEXT,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            caption_fallback TEXT,
            max_uses_per_account INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""",
        # Per-account usage history for StoryAsset. Mirrors
        # PhotoSetUsage but with device_id and dispatched_at/posted_at
        # columns for finer-grained lifecycle tracking (T4 cadence cap
        # will query these).
        """CREATE TABLE IF NOT EXISTS story_asset_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL REFERENCES story_assets(id) ON DELETE CASCADE,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            account_username VARCHAR(128) NOT NULL,
            device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
            used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            dispatched_at DATETIME,
            posted_at DATETIME
        )""",
        # Insights overhaul (2026-04-24). Hot-path lookup table for
        # "which reel is due to be collected next?"; written to on
        # every posted Video and after every InsightsSnapshot.
        """CREATE TABLE IF NOT EXISTS insights_collection_plan (
            video_id INTEGER PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            account_username VARCHAR(128) NOT NULL,
            next_due_at DATETIME NOT NULL,
            priority_score REAL NOT NULL DEFAULT 0,
            last_staleness_ms INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            target_interval_hours REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME
        )""",
        # Downsampled archive: raw InsightsSnapshot rows >7 days old
        # roll up into one row per (video_id, date) with EOD counters
        # and daily deltas. Driven by the prune_insights job.
        """CREATE TABLE IF NOT EXISTS insights_snapshot_daily (
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            date DATETIME NOT NULL,
            plays_end_of_day INTEGER,
            likes_delta INTEGER NOT NULL DEFAULT 0,
            comments_delta INTEGER NOT NULL DEFAULT 0,
            shares_delta INTEGER NOT NULL DEFAULT 0,
            saves_delta INTEGER NOT NULL DEFAULT 0,
            reach_delta INTEGER NOT NULL DEFAULT 0,
            avg_watch_time_seconds REAL,
            skip_rate_percent REAL,
            PRIMARY KEY (video_id, date)
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username VARCHAR(128) NOT NULL UNIQUE,
            display_name VARCHAR(128),
            model VARCHAR(64),
            device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            profile_link_url TEXT,
            profile_link_verified_at DATETIME,
            last_health_at DATETIME,
            last_login_check_at DATETIME,
            last_bootstrap_at DATETIME,
            app_installed BOOLEAN NOT NULL DEFAULT 0,
            gallery_permission_granted BOOLEAN NOT NULL DEFAULT 0,
            last_permission_check_at DATETIME,
            observed_account_label VARCHAR(256),
            observed_ui_signature TEXT,
            last_error_code VARCHAR(64),
            last_error_message TEXT,
            needs_attention_reason TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(status IN ('active', 'paused', 'login_required', 'needs_attention', 'blocked', 'disabled'))
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_scheduler_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL UNIQUE REFERENCES pinterest_accounts(id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT 0,
            timezone VARCHAR(64) NOT NULL DEFAULT 'America/New_York',
            target_pins_per_day INTEGER NOT NULL DEFAULT 10,
            min_pins_per_day INTEGER,
            max_pins_per_day INTEGER,
            posting_windows_json TEXT NOT NULL DEFAULT '[]',
            min_gap_minutes INTEGER NOT NULL DEFAULT 40,
            jitter_minutes INTEGER NOT NULL DEFAULT 10,
            max_retries INTEGER NOT NULL DEFAULT 3,
            retry_delay_minutes INTEGER NOT NULL DEFAULT 30,
            pause_after_failures INTEGER NOT NULL DEFAULT 3,
            device_conflict_policy VARCHAR(32) NOT NULL DEFAULT 'wait',
            reelsomet_guard_minutes INTEGER NOT NULL DEFAULT 20,
            safe_mode_enabled BOOLEAN NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(target_pins_per_day >= 0),
            CHECK(min_gap_minutes >= 0),
            CHECK(jitter_minutes >= 0),
            CHECK(reelsomet_guard_minutes >= 0)
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id VARCHAR(128) NOT NULL UNIQUE,
            model VARCHAR(64),
            platform VARCHAR(32) NOT NULL DEFAULT 'pinterest',
            source_name VARCHAR(256),
            source_path TEXT,
            manifest_hash VARCHAR(64) NOT NULL,
            manifest_json TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            created_pins_count INTEGER NOT NULL DEFAULT 0,
            created_assets_count INTEGER NOT NULL DEFAULT 0,
            created_boards_count INTEGER NOT NULL DEFAULT 0,
            invalid_rows_count INTEGER NOT NULL DEFAULT 0,
            validation_errors_json TEXT NOT NULL DEFAULT '[]',
            imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(platform = 'pinterest')
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER REFERENCES pinterest_imports(id) ON DELETE SET NULL,
            model VARCHAR(64),
            original_file VARCHAR(512) NOT NULL,
            storage_path TEXT NOT NULL,
            phone_storage_path TEXT,
            phone_staged_at DATETIME,
            media_hash VARCHAR(64) NOT NULL,
            mime_type VARCHAR(64) NOT NULL,
            width INTEGER,
            height INTEGER,
            file_size_bytes INTEGER,
            status VARCHAR(32) NOT NULL DEFAULT 'ready',
            last_error TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_boards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES pinterest_accounts(id) ON DELETE CASCADE,
            source_import_id INTEGER REFERENCES pinterest_imports(id) ON DELETE SET NULL,
            key VARCHAR(128) NOT NULL,
            name VARCHAR(256) NOT NULL,
            description TEXT NOT NULL,
            visibility VARCHAR(16) NOT NULL DEFAULT 'public',
            status VARCHAR(32) NOT NULL DEFAULT 'imported',
            pinterest_board_url TEXT,
            pinterest_board_external_id VARCHAR(128),
            confirmed_at DATETIME,
            last_create_attempt_at DATETIME,
            last_error_code VARCHAR(64),
            last_error_message TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, key),
            UNIQUE(account_id, name),
            CHECK(visibility IN ('public', 'secret')),
            CHECK(status IN ('imported', 'needs_create', 'creating', 'active', 'failed', 'needs_attention', 'archived'))
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_pins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_id VARCHAR(128) NOT NULL,
            account_id INTEGER NOT NULL REFERENCES pinterest_accounts(id) ON DELETE CASCADE,
            board_id INTEGER NOT NULL REFERENCES pinterest_boards(id) ON DELETE CASCADE,
            asset_id INTEGER NOT NULL REFERENCES pinterest_assets(id) ON DELETE CASCADE,
            source_import_id INTEGER REFERENCES pinterest_imports(id) ON DELETE SET NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            order_index INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'ready',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at DATETIME,
            posting_started_at DATETIME,
            posted_at DATETIME,
            pinterest_pin_url TEXT,
            last_error_code VARCHAR(64),
            last_error_message TEXT,
            last_attempt_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, external_id),
            CHECK(status IN ('ready', 'posting', 'posted', 'failed', 'retry_waiting', 'needs_attention', 'cancelled'))
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_post_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id VARCHAR(128) NOT NULL UNIQUE,
            trace_id VARCHAR(128) NOT NULL UNIQUE,
            task_type VARCHAR(64) NOT NULL,
            account_id INTEGER REFERENCES pinterest_accounts(id) ON DELETE SET NULL,
            board_id INTEGER REFERENCES pinterest_boards(id) ON DELETE SET NULL,
            pin_id INTEGER REFERENCES pinterest_pins(id) ON DELETE SET NULL,
            device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'running',
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME,
            result_code VARCHAR(64),
            error_code VARCHAR(64),
            error_message TEXT,
            latest_state VARCHAR(128),
            latest_action VARCHAR(128),
            next_action VARCHAR(128),
            screenshot_id VARCHAR(128),
            raw_result_json TEXT,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(task_type IN ('pinterest.health_check', 'pinterest.bootstrap_permissions', 'pinterest.ensure_board', 'pinterest.publish_pin', 'pinterest.verify_pin')),
            CHECK(status IN ('queued', 'running', 'success', 'failed', 'needs_attention', 'cancelled', 'timeout'))
        )""",
        """CREATE TABLE IF NOT EXISTS pinterest_task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id VARCHAR(128) NOT NULL UNIQUE,
            attempt_id INTEGER REFERENCES pinterest_post_attempts(id) ON DELETE CASCADE,
            task_id VARCHAR(128) NOT NULL,
            trace_id VARCHAR(128) NOT NULL,
            device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
            account_id INTEGER REFERENCES pinterest_accounts(id) ON DELETE SET NULL,
            board_id INTEGER REFERENCES pinterest_boards(id) ON DELETE SET NULL,
            pin_id INTEGER REFERENCES pinterest_pins(id) ON DELETE SET NULL,
            ts_ms INTEGER NOT NULL,
            fsm_kind VARCHAR(128) NOT NULL,
            state VARCHAR(128) NOT NULL,
            state_entered_at_ms INTEGER,
            action_name VARCHAR(128),
            action_target VARCHAR(256),
            action_started_at_ms INTEGER,
            action_finished_at_ms INTEGER,
            action_result VARCHAR(64),
            next_action_name VARCHAR(128),
            next_action_target VARCHAR(256),
            next_action_at_ms INTEGER,
            screen_activity VARCHAR(256),
            screen_hash VARCHAR(128),
            screenshot_id VARCHAR(128),
            message TEXT NOT NULL,
            fields_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    async with engine.begin() as conn:
        for ddl in new_tables:
            await conn.execute(text(ddl))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_caption_seeds_model ON caption_seeds(model)"
        ))
        # farm_logs indexes — every column we filter/sort by gets a partial-index
        # on (col, ts DESC) so the LogsPage queries hit the index regardless of
        # which filter is active. Codex schema.
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_farm_logs_device_event "
            "ON farm_logs(device_id, event_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_ts ON farm_logs(ts DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_device_ts ON farm_logs(device_id, ts DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_account_ts ON farm_logs(account, ts DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_donor_usage_device_used "
            "ON donor_usage(device_id, used_at)"
        ))
        # Composite index for the sibling-gap check in _dispatch_video:
        # MAX(dispatched_at) WHERE set_id=? AND device_id=? AND dispatched_at >= cutoff
        # is the hot path. Table grows one row per carousel generation but
        # queries hit it on every carousel dispatch.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_psu_set_device_dispatched "
            "ON photo_set_usage(set_id, device_id, dispatched_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_sau_asset_device_dispatched "
            "ON story_asset_usage(asset_id, device_id, dispatched_at)"
        ))
        # LRU picker indexes (2026-04-14 story dispatcher):
        # - account+asset+used_at supports the acct CTE (per-account
        #   MAX(used_at) + COUNT(*) subquery).
        # - device+used_at supports the recent_device CTE (same-device
        #   cooldown exclusion).
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_sau_acct_asset_used "
            "ON story_asset_usage(account_username, asset_id, used_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_sau_device_used "
            "ON story_asset_usage(device_id, used_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_trace_ts ON farm_logs(trace_id, ts DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_activity_ts ON farm_logs(activity, ts DESC)"
        ))
        # Insights overhaul indexes (2026-04-24):
        # - `ix_insights_plan_due` is the scheduler's hot-path pick-next
        #   query: ORDER BY next_due_at, priority_score DESC LIMIT K.
        # - `ix_insights_plan_account_due` helps the per-account UI
        #   views.
        # - `ix_snapshots_video_time` + `ix_snapshots_account_time`
        #   serve per-video sparklines and per-account trend windows.
        # - `ix_videos_marker` / `ix_videos_caption_hash` are the
        #   identity-match lookup keys for _upsert_insights_snapshot.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_insights_plan_due "
            "ON insights_collection_plan(next_due_at, priority_score DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_insights_plan_account_due "
            "ON insights_collection_plan(account_username, next_due_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_snapshots_video_time "
            "ON insights_snapshots(video_id, collected_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_snapshots_account_time "
            "ON insights_snapshots(account_username, collected_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_videos_marker "
            "ON videos(insights_video_marker)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_videos_caption_hash "
            "ON videos(insights_caption_hash)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_snapshot_daily_date "
            "ON insights_snapshot_daily(date)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_farm_logs_level_ts ON farm_logs(level, ts DESC)"
        ))
        # Phones page (2026-05-06): index on updated_at for "all phones right now" snapshot query.
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_device_fsm_state_updated "
            "ON device_fsm_state(updated_at DESC)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_accounts_device "
            "ON pinterest_accounts(device_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_assets_media_hash "
            "ON pinterest_assets(media_hash)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_assets_import_id "
            "ON pinterest_assets(import_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_boards_account_status "
            "ON pinterest_boards(account_id, status)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_boards_source_import "
            "ON pinterest_boards(source_import_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_pins_pick "
            "ON pinterest_pins(account_id, status, priority, order_index, created_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_pins_board_status "
            "ON pinterest_pins(board_id, status)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_pins_asset "
            "ON pinterest_pins(asset_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_pins_source_import "
            "ON pinterest_pins(source_import_id)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_pins_next_retry "
            "ON pinterest_pins(next_retry_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_attempts_account_started "
            "ON pinterest_post_attempts(account_id, started_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_attempts_pin_started "
            "ON pinterest_post_attempts(pin_id, started_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_attempts_board_started "
            "ON pinterest_post_attempts(board_id, started_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_attempts_device_started "
            "ON pinterest_post_attempts(device_id, started_at)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_attempts_status "
            "ON pinterest_post_attempts(status)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_events_trace_ts "
            "ON pinterest_task_events(trace_id, ts_ms)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_events_task_ts "
            "ON pinterest_task_events(task_id, ts_ms)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_events_device_ts "
            "ON pinterest_task_events(device_id, ts_ms)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_events_pin_ts "
            "ON pinterest_task_events(pin_id, ts_ms)"
        ))
        await conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_pinterest_events_board_ts "
            "ON pinterest_task_events(board_id, ts_ms)"
        ))


async def enable_wal_mode(engine: AsyncEngine) -> None:
    """Enable WAL journal mode and tuning PRAGMAs for concurrent access."""
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA busy_timeout=5000"))
        await conn.execute(text("PRAGMA journal_size_limit=67108864"))  # 64 MB WAL limit
    logger.info("WAL mode enabled (busy_timeout=5000, journal_size_limit=64MB)")
