"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from server import farm_time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, update
from starlette.middleware.base import BaseHTTPMiddleware

from server.config import VPSConfig
from server.database import build_engine, build_session_factory, create_tables, enable_wal_mode
from server.license_gate import is_licensed
from server.log_handler import WebSocketLogHandler
from server.scheduler import FarmScheduler
from server.screen_relay import ScreenRelay
from server.telegram import VPSTelegramBot
from server.ws.admin_broadcaster import AdminBroadcaster
from server.ws.admin_handler import admin_ws_router
from server.ws.bridge import DeviceBridge
from server.ws.handler import ws_router
from server.ws.manager import DeviceConnectionManager
from server.ws.screen_handler import screen_ws_router

# Maximum request body size: 500 MB
_MAX_BODY_SIZE = 500 * 1024 * 1024
_LAST_SEEN_FLUSH_INTERVAL_SECONDS = 10.0


class _LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    """Reject request bodies larger than _MAX_BODY_SIZE."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                parsed_content_length = int(content_length)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if parsed_content_length > _MAX_BODY_SIZE:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large (max 500 MB)"},
                )
        return await call_next(request)

logger = logging.getLogger(__name__)


async def _flush_last_seen_buffer(app: FastAPI) -> None:
    """Flush buffered device heartbeats with one batched UPDATE."""
    lock: asyncio.Lock = app.state.last_seen_lock
    async with lock:
        if not app.state.last_seen_buffer:
            return
        pending: dict[int, datetime] = dict(app.state.last_seen_buffer)
        app.state.last_seen_buffer.clear()

    try:
        from server.models import Device

        session_factory = app.state.db_session_factory
        device_ids = list(pending)
        async with session_factory.begin() as session:
            await session.execute(
                update(Device)
                .where(Device.id.in_(device_ids))
                .values(
                    last_seen_at=case(
                        pending,
                        value=Device.id,
                        else_=Device.last_seen_at,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
    except Exception as exc:
        async with lock:
            for device_id, touched_at in pending.items():
                current = app.state.last_seen_buffer.get(device_id)
                if current is None or touched_at > current:
                    app.state.last_seen_buffer[device_id] = touched_at
        logger.warning(
            "batched last_seen_at flush failed for %d devices: %s",
            len(pending),
            exc,
        )


async def _last_seen_flush_loop(app: FastAPI) -> None:
    """Periodically flush coalesced heartbeat timestamps."""
    try:
        while True:
            await asyncio.sleep(_LAST_SEEN_FLUSH_INTERVAL_SECONDS)
            await _flush_last_seen_buffer(app)
    except asyncio.CancelledError:
        await _flush_last_seen_buffer(app)
        raise


# Cleanup cadence for staged manual-prep files. The per-file TTL is
# 30 min (set in server.api.manual_prep), so a 10 min sweep gives at
# most ~10 min of dead-disk overhang per stale file.
_MANUAL_PREP_CLEANUP_INTERVAL_SECONDS = 600


async def _manual_prep_cleanup_loop(app: FastAPI) -> None:
    """Periodically delete expired manual-prep staged files."""
    from server.api.manual_prep import cleanup_manual_prep_dir
    config: VPSConfig = app.state.config
    try:
        while True:
            try:
                await cleanup_manual_prep_dir(config)
            except Exception as exc:
                logger.warning("manual_prep cleanup failed: %s", exc)
            await asyncio.sleep(_MANUAL_PREP_CLEANUP_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        raise


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle."""
    config: VPSConfig = app.state.config

    # --- Database engine (async SQLite via aiosqlite) ---
    engine = build_engine(config.database_path)
    app.state.db_engine = engine
    app.state.db_session_factory = build_session_factory(engine)
    await create_tables(engine)
    await enable_wal_mode(engine)
    logger.info("Database ready: %s", config.database_path)
    app.state.last_seen_buffer: dict[int, datetime] = {}
    app.state.last_seen_lock = asyncio.Lock()
    app.state.last_seen_flush_task = asyncio.create_task(_last_seen_flush_loop(app))
    app.state.manual_prep_cleanup_task = asyncio.create_task(_manual_prep_cleanup_loop(app))

    # --- Admin WebSocket log handler ---
    broadcaster: AdminBroadcaster = app.state.admin_broadcaster
    log_handler = WebSocketLogHandler(broadcaster)
    log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger("server").addHandler(log_handler)

    # Manual prep dispatch is fire-and-forget: validate fast in the
    # endpoint, then offload ghost+WS dispatch to a task. The task
    # needs to be able to broadcast failures to admin clients (so the
    # modal/Telegram notifier learns about them); wire that here, AFTER
    # the broadcaster is bound to a local variable.
    from server.api.manual_prep import attach_broadcaster as _mp_attach
    _mp_attach(broadcaster)

    # --- Telegram bot (optional, non-fatal) ---
    manager: DeviceConnectionManager = app.state.ws_manager
    telegram_bot: VPSTelegramBot | None = None
    if config.telegram_bot_token and config.telegram_bot_token not in ("", "placeholder"):
        try:
            bot = VPSTelegramBot(config, app.state.db_session_factory, manager)
            await bot.start()
            app.state.telegram_bot = bot
            telegram_bot = bot
        except Exception as exc:
            logger.warning("Telegram bot failed to start (invalid token?): %s", exc)
    else:
        logger.info("Telegram bot not configured, skipping")

    # --- Farm scheduler ---
    bridge = DeviceBridge(manager)
    scheduler = FarmScheduler(
        session_factory=app.state.db_session_factory,
        ws_manager=manager,
        bridge=bridge,
        config=config,
        broadcaster=broadcaster,
        telegram_bot=telegram_bot,
    )

    # --- Wire device manager events -> admin broadcaster ---
    _wire_device_events(
        manager, broadcaster,
        session_factory=app.state.db_session_factory,
        telegram_bot=telegram_bot,
        scheduler=scheduler,
        app=app,
    )
    await scheduler.start()
    logger.info("Scheduler started (open-source edition)")
    app.state.scheduler = scheduler
    app.state.bridge = bridge

    # --- Startup notification ---
    if telegram_bot is not None:
        try:
            online_count = len(manager.get_online_device_ids())
            license_status = "active" if is_licensed(config) else "inactive"
            await telegram_bot.send_notification(
                "\U0001f7e2 <b>Reelsomet VPS started</b>\n"
                f"Devices online: {online_count}\n"
                f"License: {license_status}"
            )
        except Exception:
            pass  # Don't crash if TG fails

    yield

    # --- Shutdown ---
    await scheduler.stop()

    flush_task = getattr(app.state, "last_seen_flush_task", None)
    if flush_task is not None:
        flush_task.cancel()
        try:
            await flush_task
        except asyncio.CancelledError:
            pass

    manual_prep_task = getattr(app.state, "manual_prep_cleanup_task", None)
    if manual_prep_task is not None:
        manual_prep_task.cancel()
        try:
            await manual_prep_task
        except asyncio.CancelledError:
            pass

    if hasattr(app.state, "telegram_bot"):
        await app.state.telegram_bot.stop()

    logging.getLogger("server").removeHandler(log_handler)

    await engine.dispose()
    logger.info("Database engine disposed")


def _wire_device_events(
    manager: DeviceConnectionManager,
    broadcaster: AdminBroadcaster,
    session_factory: Any = None,
    telegram_bot: VPSTelegramBot | None = None,
    scheduler: FarmScheduler | None = None,
    app: FastAPI | None = None,
) -> None:
    """Hook device manager lifecycle events to push updates to admin browsers."""
    from server.ws.protocol import MessageType, WSMessage

    original_connect = manager.connect
    original_disconnect = manager.disconnect

    async def _get_device_snapshot(device_id: int) -> dict[str, Any] | None:
        """Return a minimal DB snapshot for the device when persistence is available."""
        if session_factory is None:
            return None
        try:
            async with session_factory() as session:
                from server.models import Device
                from sqlalchemy import select as sa_select
                dev = (await session.execute(
                    sa_select(Device).where(Device.id == device_id),
                )).scalar_one_or_none()
                if dev is None:
                    return {
                        "exists": False,
                        "is_active": False,
                        "name": None,
                    }
                return {
                    "exists": True,
                    "is_active": bool(dev.is_active),
                    "name": dev.name,
                }
        except Exception:
            return None

    async def _device_accepts_events(device_id: int) -> bool:
        """Treat unknown/new devices as acceptable, but reject known inactive ones."""
        snapshot = await _get_device_snapshot(device_id)
        return snapshot is None or not snapshot["exists"] or bool(snapshot["is_active"])

    # Per-device dedup window for the "Device online" Telegram alert.
    # A force-restart respawn (sticky foreground services) generates
    # multiple WebSocket reconnect events in quick succession; without
    # this guard the operator sees three identical "🟢 Device online"
    # messages within ~1 second. 5-min cooldown is generous enough to
    # cover any expected reconnect storm and short enough that a real
    # outage-then-recovery still pages.
    _last_online_alert_at: dict[int, float] = {}
    _online_alert_cooldown_s = 300.0

    async def on_connect(device_id: int, websocket: Any) -> None:
        snapshot = await _get_device_snapshot(device_id)
        if snapshot is not None and snapshot["exists"] and not snapshot["is_active"]:
            logger.info("Ignoring websocket connect for inactive device %d", device_id)
            try:
                await websocket.close()
            except Exception:
                pass
            return

        await original_connect(device_id, websocket)
        await broadcaster.broadcast("device:connected", {"device_id": device_id})

        # Notify Telegram that device came online — once per cooldown.
        if telegram_bot is not None:
            now_ts = time.time()
            last = _last_online_alert_at.get(device_id, 0.0)
            if (now_ts - last) >= _online_alert_cooldown_s:
                _last_online_alert_at[device_id] = now_ts
                # Resolve device name from DB (best-effort)
                device_name = (snapshot or {}).get("name") or f"Device {device_id}"
                try:
                    await telegram_bot.notify_device_online(device_name)
                except Exception as exc:
                    logger.warning("Telegram online notification failed: %s", exc)

    async def on_disconnect(device_id: int, websocket: Any | None = None) -> bool:
        disconnected = await original_disconnect(device_id, websocket)
        if not disconnected:
            return False
        if not await _device_accepts_events(device_id):
            return disconnected
        await broadcaster.broadcast("device:disconnected", {"device_id": device_id})
        return disconnected

    manager.connect = on_connect  # type: ignore[assignment]
    manager.disconnect = on_disconnect  # type: ignore[assignment]

    # Wrap existing on_event callback to also broadcast device events
    prev_on_event = manager.on_event

    async def _auto_register_device(device_id: int, info: dict[str, Any]) -> None:
        """Auto-create Device record in DB if it doesn't exist."""
        try:
            async with session_factory() as session:
                from server.models import Device
                from sqlalchemy import select
                existing = (await session.execute(
                    select(Device).where(Device.id == device_id)
                )).scalar_one_or_none()
                if existing is None:
                    model = info.get("model", f"Device-{device_id}")
                    device = Device(
                        id=device_id,
                        device_id=str(device_id),
                        name=model,
                        device_model=model,
                        android_version=info.get("androidVersion", ""),
                        app_version=info.get("appVersion", ""),
                        status="online",
                    )
                    session.add(device)
                    await session.commit()
                    logger.info("Auto-registered device %d (%s)", device_id, model)
                elif not existing.is_active:
                    logger.info("Ignoring hello from inactive device %d", device_id)
                else:
                    existing.device_model = info.get("model") or existing.device_model
                    existing.android_version = info.get("androidVersion") or existing.android_version
                    existing.app_version = info.get("appVersion") or existing.app_version
                    existing.status = "online"
                    await session.commit()
        except Exception as exc:
            logger.warning("Auto-register device %d failed: %s", device_id, exc)

    async def on_event(device_id: int, message: WSMessage) -> None:
        if prev_on_event is not None:
            await prev_on_event(device_id, message)

        if not await _device_accepts_events(device_id):
            return

        if message.type == MessageType.DEVICE_HELLO:
            await _auto_register_device(device_id, message.payload)
            await broadcaster.broadcast("device:status", {
                "device_id": device_id,
                "is_online": True,
                **message.payload,
            })
        elif message.type == MessageType.EVENT_DEVICE_STATUS:
            await broadcaster.broadcast("device:status", {
                "device_id": device_id,
                "is_online": True,
                **message.payload,
            })
        elif message.type == MessageType.EVENT_PINTEREST_FSM:
            payload = dict(message.payload)
            try:
                if session_factory is not None:
                    from server.pinterest.events import persist_pinterest_fsm_event
                    async with session_factory() as session:
                        await persist_pinterest_fsm_event(
                            session,
                            device_id=device_id,
                            payload=payload,
                        )
                        await session.commit()
            except Exception as exc:
                logger.error(
                    "Pinterest FSM event ingest error from device %d: %s",
                    device_id,
                    exc,
                )
            payload.setdefault("device_id", device_id)
            await broadcaster.broadcast("pinterest:fsm", payload)
        elif message.type == MessageType.EVENT_REDDIT_FSM:
            payload = dict(message.payload)
            try:
                if session_factory is not None:
                    from server.reddit.events import persist_reddit_fsm_event
                    async with session_factory() as session:
                        await persist_reddit_fsm_event(
                            session,
                            device_id=device_id,
                            payload=payload,
                        )
                        await session.commit()
            except Exception as exc:
                logger.error(
                    "Reddit FSM event ingest error from device %d: %s",
                    device_id,
                    exc,
                )
            payload.setdefault("device_id", device_id)
            await broadcaster.broadcast("reddit:fsm", payload)
        elif message.type in (MessageType.EVENT_POST_COMPLETE, MessageType.EVENT_POST_FAILED):
            await broadcaster.broadcast("post:result", {
                "device_id": device_id,
                "success": message.type == MessageType.EVENT_POST_COMPLETE,
                **message.payload,
            })
        elif message.type == MessageType.EVENT_PROFILE_STATS:
            # Update account followers/following/posts from phone scrape
            try:
                async with session_factory() as session:
                    from server.models import Account
                    from sqlalchemy import select as sa_select
                    username = message.payload.get("username", "")
                    if username:
                        acct = (await session.execute(
                            sa_select(Account).where(Account.username == username)
                        )).scalar_one_or_none()
                        if acct is not None:
                            f = message.payload.get("followers", -1)
                            fw = message.payload.get("following", -1)
                            p = message.payload.get("posts", -1)
                            stats_payload: dict[str, Any] = {"username": username}
                            old_posts_count = int(acct.posts_count or 0)
                            if f >= 0:
                                acct.followers = f
                                stats_payload["followers"] = f
                            if fw >= 0:
                                acct.following = fw
                                stats_payload["following"] = fw
                            if p >= 0:
                                acct.posts_count = p
                                stats_payload["posts_count"] = p
                                if (
                                    scheduler is not None
                                    and p > old_posts_count
                                ):
                                    await scheduler.reconcile_profile_post_count(
                                        session,
                                        account=acct,
                                        device_id=device_id,
                                        old_posts_count=old_posts_count,
                                        new_posts_count=p,
                                    )
                            await session.commit()
                            if len(stats_payload) > 1:
                                await broadcaster.broadcast("account:update", stats_payload)
                            logger.info("Profile stats @%s: followers=%s following=%s posts=%s", username, f, fw, p)
            except Exception as exc:
                logger.warning("Profile stats update failed: %s", exc)

        elif message.type == MessageType.EVENT_LOGIN_STATUS:
            # T5: Instagram login FSM progress (pending / 2fa_required /
            # success / failed) broadcast from LoginStateMachine on the
            # phone. The AccountsPage Login tab subscribes to
            # `event.login_status` and updates its UI in real time —
            # without this rebroadcast the WS subscription is a no-op
            # and the UI falls back to polling. See
            # docs/T5-backend-followup.md.
            payload = dict(message.payload)
            payload.setdefault("device_id", device_id)
            await broadcaster.broadcast("event.login_status", payload)

        elif message.type in (MessageType.VIDEO_DOWNLOAD_COMPLETE, MessageType.VIDEO_DOWNLOAD_PROGRESS):
            is_complete = (
                message.type == MessageType.VIDEO_DOWNLOAD_COMPLETE
                or message.payload.get("percent") == 100
            )
            if is_complete and scheduler is not None:
                payload = message.payload.copy()
                payload.setdefault("success", True)
                try:
                    await scheduler.handle_download_complete(device_id, payload)
                except Exception as exc:
                    logger.error(
                        "handle_download_complete error for device %d: %s",
                        device_id, exc,
                    )

        elif message.type == MessageType.MANUAL_PREP_COMPLETE:
            # Operator-driven manual prep flow (Movies/Reelsomet push).
            # Rebroadcast to admin clients so the modal that issued the
            # request can flip its UI from "Sending..." to "Sent". The
            # phone reports back with {requestId, success, mediaStoreUri,
            # error}.
            payload = dict(message.payload)
            payload.setdefault("device_id", device_id)
            await broadcaster.broadcast("event.manual_prep_complete", payload)

            # Telegram nudge: most operators won't have the admin tab
            # open while waiting for a manual prep, so push a TG
            # message with the caption ready to copy.
            tg_bot = getattr(app.state, "telegram_bot", None)
            if tg_bot is not None:
                try:
                    import html as _html
                    success = bool(payload.get("success", False))
                    caption = str(payload.get("caption") or "")
                    filename = str(payload.get("filename") or "")
                    dev_name = str(
                        payload.get("device_name")
                        or payload.get("deviceName")
                        or f"device #{device_id}"
                    )
                    if success:
                        body = (
                            "\U0001f4f2 <b>Manual prep ready</b>\n"
                            f"Device: <code>{_html.escape(dev_name)}</code>\n"
                            f"File: <code>{_html.escape(filename)}</code>\n\n"
                            f"<b>Caption:</b>\n<pre>{_html.escape(caption) if caption else '(empty)'}</pre>"
                        )
                    else:
                        err = str(payload.get("error") or "unknown error")
                        body = (
                            "⚠️ <b>Manual prep failed</b>\n"
                            f"Device: <code>{_html.escape(dev_name)}</code>\n"
                            f"File: <code>{_html.escape(filename)}</code>\n"
                            f"Error: {_html.escape(err)}"
                        )
                    await tg_bot.send_notification(body)
                except Exception:
                    logger.exception("manual_prep TG notification failed")

        elif message.type == MessageType.CAROUSEL_READY:
            if scheduler is not None:
                payload = message.payload.copy()
                try:
                    await scheduler.handle_carousel_ready(device_id, payload)
                except Exception as exc:
                    logger.error(
                        "handle_carousel_ready error for device %d: %s",
                        device_id, exc,
                    )

        elif message.type == MessageType.EVENT_LOG:
            # Advanced logging step 4 (Codex): ingest batched log events from
            # the phone's FarmLog forwarder. Payload shape:
            #   {"entries": [{eventId, ts, level, source, activity, ...}, ...]}
            # Dedup happens at the SQL UNIQUE(device_id, event_id) constraint
            # so resubmits during WS reconnect are silently dropped.
            entries = message.payload.get("entries") or []
            if entries:
                try:
                    from server.log_store import ingest_log_batch
                    from server.fsm_state import (
                        ACTIVITY_SNAPSHOT,
                        ACTIVITY_TAP,
                        snapshot_broadcast_payload,
                        tap_broadcast_payload,
                        update_state_from_entries,
                    )
                    async with session_factory() as session:
                        # ingest_log_batch returns the set of event_ids
                        # that were actually inserted (vs INSERT OR IGNORE
                        # silently dedup'd) — Codex review 2026-05-06.
                        # Use it to suppress broadcasts of reconnect
                        # resubmissions.
                        new_ids = await ingest_log_batch(
                            session, entries, device_id=device_id,
                        )
                        # Phones page (2026-05-06): mirror POSTING_STATE
                        # entries into device_fsm_state so the live-state
                        # pane has a hot-path read. Pass only newly-
                        # inserted entries so a reconnect can never
                        # roll the live state backward.
                        new_entries = [
                            e for e in entries
                            if (e.get("eventId") or e.get("event_id")) in new_ids
                        ]
                        upserted_states = await update_state_from_entries(
                            session, device_id, new_entries,
                        )
                        await session.commit()
                    # Skip the broadcast entirely when the whole batch
                    # was deduplicated by the UNIQUE(device_id, event_id)
                    # constraint. This is the common case on WS
                    # reconnect where the phone re-sends already-ingested
                    # entries, and rebroadcasting them would surface
                    # duplicate log lines in the admin UI (Codex iter 13
                    # bug hunt 2026-04-14).
                    if new_entries:
                        # Re-broadcast each NEW entry to admin browsers so
                        # the LogsPage updates live without polling.
                        for entry in new_entries:
                            try:
                                await broadcaster.broadcast("log:entry", {
                                    "ts": entry.get("ts"),
                                    "level": entry.get("level"),
                                    "source": entry.get("source"),
                                    "activity": entry.get("activity"),
                                    "device_id": device_id,
                                    "account": entry.get("account"),
                                    "trace_id": entry.get("traceId") or entry.get("trace_id"),
                                    "message": entry.get("message"),
                                    "fields": entry.get("fields") or {},
                                })
                            except Exception:
                                pass
                            # Phones page mirror: tap + snapshot meta
                            # broadcasts. State broadcast happens below
                            # from upserted_states.
                            try:
                                activity = entry.get("activity")
                                if activity == ACTIVITY_TAP:
                                    await broadcaster.broadcast(
                                        "phone:tap",
                                        tap_broadcast_payload(entry, device_id),
                                    )
                                elif activity == ACTIVITY_SNAPSHOT:
                                    await broadcaster.broadcast(
                                        "phone:snapshot",
                                        snapshot_broadcast_payload(entry, device_id),
                                    )
                            except Exception:
                                pass
                    # Broadcast state upserts (only fresh ones — see
                    # filter above). Empty list → no-op.
                    for row in upserted_states:
                        try:
                            await broadcaster.broadcast("phone:state", row)
                        except Exception:
                            pass
                except Exception as exc:
                    logger.error(
                        "event.log ingest error from device %d: %s",
                        device_id, exc,
                    )

    manager.on_event = on_event

    # Wrap handle_message to catch heartbeats (which don't trigger on_event)
    # and update device.last_seen_at on every inbound message.
    original_handle = manager.handle_message

    async def _touch_last_seen(device_id: int) -> None:
        """Queue a last_seen_at touch for the next batched heartbeat flush.

        Coalescing touches avoids one SQLite write transaction per
        heartbeat while preserving the silent-timeout semantics.
        """
        try:
            if app is not None:
                async with app.state.last_seen_lock:
                    app.state.last_seen_buffer[device_id] = farm_time.utcnow_naive()
                return

            if session_factory is None:
                return

            from server.models import Device

            async with session_factory.begin() as session:
                await session.execute(
                    update(Device)
                    .where(Device.id == device_id)
                    .values(last_seen_at=farm_time.utcnow_naive())
                    .execution_options(synchronize_session=False)
                )
        except Exception as exc:
            # Swallow to avoid tearing the WS socket down on a transient DB
            # blip, but log at warning so silent touch failures are visible
            # if the flap recurs.
            logger.warning("touch last_seen_at failed for device %d: %s", device_id, exc)

    async def handle_message_with_broadcast(device_id: int, message: WSMessage) -> None:
        await original_handle(device_id, message)
        # Phase 4 (2026-04-24): touch last_seen_at ONLY on first-class
        # liveness signals — application heartbeats, ping/pong, and
        # healthcheck replies. Touching on every message hides phones
        # whose reader thread is alive enough to autopong but whose
        # app side is wedged: the OkHttp PONG arrives, we touch
        # last_seen_at, the watchdog never marks the phone offline,
        # and the queue silently piles up. The active healthcheck
        # probe (probe_silent_devices) is the only way to detect a
        # wedged-app phone, and that probe relies on last_seen_at
        # being honest.
        is_liveness = (
            message.type in (
                MessageType.EVENT_HEARTBEAT,
                MessageType.DEVICE_HEARTBEAT,
                MessageType.PONG,
            )
            or (
                message.type == MessageType.RESP_OK
                and isinstance(message.payload, dict)
                and "echoToken" in message.payload
            )
        )
        if is_liveness:
            await _touch_last_seen(device_id)
        if not await _device_accepts_events(device_id):
            return
        if message.type in (MessageType.EVENT_HEARTBEAT, MessageType.DEVICE_HEARTBEAT):
            await broadcaster.broadcast("device:status", {
                "device_id": device_id,
                "is_online": True,
                **message.payload,
            })

    manager.handle_message = handle_message_with_broadcast  # type: ignore[assignment]


def create_app(config: VPSConfig | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Parameters
    ----------
    config : VPSConfig, optional
        Server configuration.  If *None*, a default ``VPSConfig()`` is used.
    """
    if config is None:
        config = VPSConfig()

    # Enable /api/docs only when REELSOMET_DEBUG=1 is set
    debug_mode = os.environ.get("REELSOMET_DEBUG", "0") == "1"

    app = FastAPI(
        title="Reelsomet VPS",
        version="0.1.0",
        docs_url="/api/docs" if debug_mode else None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    # Store config, ws_manager, and admin_broadcaster on app.state for
    # dependency injection.  Created here (not in lifespan) because they have
    # no async setup and must be available before lifespan runs (e.g. during tests).
    app.state.config = config
    app.state.ws_manager = DeviceConnectionManager()
    app.state.admin_broadcaster = AdminBroadcaster()
    screen_relay = ScreenRelay(app.state.ws_manager)
    screen_relay.attach_to_app(app)

    # --- Upload size limit ---
    app.add_middleware(_LimitUploadSizeMiddleware)

    # --- CORS ---
    allowed_origins: list[str] = []
    if config.domain:
        allowed_origins.append(f"https://{config.domain}")
        allowed_origins.append(f"http://{config.domain}")
    # In debug mode, also allow localhost for development
    if debug_mode:
        allowed_origins.append("http://localhost:5173")
        allowed_origins.append("http://localhost:3000")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- WebSocket routes (no prefix) ---
    app.include_router(ws_router)
    app.include_router(admin_ws_router)
    app.include_router(screen_ws_router)

    # --- REST API routers ---
    from server.api.auth_router import router as auth_router
    from server.api.control_auth import router as control_auth_router
    from server.api.dashboard import router as dashboard_router
    from server.api.devices import router as devices_router
    from server.api.accounts import router as accounts_router
    from server.api.queue import router as queue_router
    from server.api.insights import router as insights_router
    from server.api.engagement import router as engagement_router
    from server.api.engagement_llm import router as engagement_llm_router
    from server.api.monitor import router as monitor_router
    from server.api.logs import router as logs_router
    from server.api.settings_api import router as settings_router
    from server.api.setup import router as setup_router
    from server.api.license import router as license_router
    from server.api.system import router as system_router
    from server.api.video_download import router as video_download_router
    from server.api.scenarios import router as scenarios_router
    from server.api.tracks import router as tracks_router
    from server.api.models_api import router as models_router
    from server.api.ig_login import router as ig_login_router
    from server.api.photo_sets import router as photo_sets_router
    from server.api.screen_alert import router as screen_alert_router
    from server.api.story_assets import router as story_assets_router
    from server.api.caption_seeds import router as caption_seeds_router
    from server.api.proxy import router as proxy_router
    from server.api.manual_prep import router as manual_prep_router
    from server.api.manual_photo_push import router as manual_photo_push_router
    from server.api.phones import router as phones_router
    from server.api.pinterest import router as pinterest_router
    from server.api.reddit import router as reddit_router

    app.include_router(auth_router)
    app.include_router(control_auth_router)
    app.include_router(license_router)
    app.include_router(setup_router)
    app.include_router(dashboard_router)
    app.include_router(devices_router)
    app.include_router(accounts_router)
    app.include_router(queue_router)
    app.include_router(insights_router)
    app.include_router(engagement_router)
    app.include_router(engagement_llm_router)
    app.include_router(monitor_router)
    app.include_router(logs_router)
    app.include_router(settings_router)
    app.include_router(system_router)
    app.include_router(video_download_router)
    app.include_router(scenarios_router)
    app.include_router(tracks_router)
    app.include_router(models_router)
    app.include_router(ig_login_router)
    app.include_router(proxy_router)
    app.include_router(photo_sets_router)
    app.include_router(screen_alert_router)
    app.include_router(story_assets_router)
    app.include_router(caption_seeds_router)
    app.include_router(manual_prep_router)
    app.include_router(manual_photo_push_router)
    app.include_router(phones_router)
    app.include_router(pinterest_router)
    app.include_router(reddit_router)

    # --- Health endpoint (no auth required) ---
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # --- APK download (no auth required — customer installs on phone) ---
    from fastapi import HTTPException

    @app.get("/api/apk/download")
    async def download_apk() -> FileResponse:
        """Serve the Android APK for phone installation."""
        apk_path = Path(__file__).resolve().parent.parent / "apk" / "reelsomet.apk"
        if not apk_path.exists():
            raise HTTPException(404, "APK not found")
        return FileResponse(
            str(apk_path),
            filename="reelsomet.apk",
            media_type="application/vnd.android.package-archive",
        )

    # --- Static files ---
    # Static files are served by Caddy (or nginx) in production.
    # For development, mount /assets for Vite build output.
    static_dir = Path(config.static_dir)
    if static_dir.is_dir():
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="static_assets")
        logger.info("Serving /assets from %s", static_dir)
    else:
        logger.info("No static directory at %s", static_dir)

    # Browser routes share the built Vue entry point. Missing API routes must
    # remain JSON 404s, never successful responses containing the SPA HTML.
    if (static_dir / "index.html").is_file():
        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str) -> FileResponse:
            if path == "api" or path.startswith("api/") or path == "ws" or path.startswith("ws/"):
                raise HTTPException(404, "Not found")
            return FileResponse(static_dir / "index.html")

    return app
