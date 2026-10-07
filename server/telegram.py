"""Telegram bot for VPS admin notifications and control.

Runs inside the FastAPI event loop using python-telegram-bot 22.x (async).
All handlers are admin-only: ``update.effective_user.id`` must be in
``config.telegram_admin_chat_ids``.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from server.config import VPSConfig
from server.models import Account, Device, PostLog, Video
from server.updater import apply_update, check_for_updates, format_uptime, get_system_info
from server.ws.manager import DeviceConnectionManager

logger = logging.getLogger(__name__)


def _is_admin(user_id: int, config: VPSConfig) -> bool:
    return user_id in config.telegram_admin_chat_ids


def _timeago(dt: datetime | None) -> str:
    """Human-readable time-ago string."""
    if dt is None:
        return "never"
    now = datetime.now(timezone.utc)
    # Handle naive datetimes by assuming UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


class VPSTelegramBot:
    """Admin Telegram bot that runs inside the FastAPI event loop."""

    def __init__(
        self,
        config: VPSConfig,
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: DeviceConnectionManager,
    ) -> None:
        self.config = config
        self.session_factory = session_factory
        self.ws_manager = ws_manager
        self._app: Application | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build and start the bot (non-blocking, runs in background)."""
        token = self.config.telegram_bot_token
        if not token:
            logger.warning("Telegram bot token not configured, skipping bot start")
            return

        builder = Application.builder().token(token)
        self._app = builder.build()

        # Register handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("devices", self._cmd_devices))
        self._app.add_handler(CommandHandler("accounts", self._cmd_accounts))
        self._app.add_handler(CommandHandler("queue", self._cmd_queue))
        self._app.add_handler(CommandHandler("logs", self._cmd_logs))
        self._app.add_handler(CommandHandler("settings", self._cmd_settings))
        self._app.add_handler(CommandHandler("update", self._cmd_update))
        self._app.add_handler(CommandHandler("version", self._cmd_version))
        self._app.add_handler(CallbackQueryHandler(self._on_callback))

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started")

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        if self._app is None:
            return
        try:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            if self._app.running:
                await self._app.stop()
            await self._app.shutdown()
        except Exception as exc:
            logger.error("Error stopping Telegram bot: %s", exc)
        finally:
            self._app = None
            logger.info("Telegram bot stopped")

    # ------------------------------------------------------------------
    # Outgoing notifications
    # ------------------------------------------------------------------

    async def send_notification(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a message to all admin chat_ids."""
        if self._app is None or self._app.bot is None:
            return
        for chat_id in self.config.telegram_admin_chat_ids:
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=parse_mode,
                )
            except Exception as exc:
                logger.error("Failed to send notification to %d: %s", chat_id, exc)

    async def notify_post_success(
        self, account: str, video: str, duration_ms: int,
    ) -> None:
        duration_s = duration_ms / 1000.0
        text = (
            f"\u2705 <b>Post success</b>\n"
            f"Account: <code>{html.escape(account)}</code>\n"
            f"Video: <code>{html.escape(video)}</code>\n"
            f"Duration: {duration_s:.1f}s"
        )
        await self.send_notification(text)

    async def notify_post_failure(
        self, account: str, video: str, error: str,
    ) -> None:
        text = (
            f"\u274c <b>Post failed</b>\n"
            f"Account: <code>{html.escape(account)}</code>\n"
            f"Video: <code>{html.escape(video)}</code>\n"
            f"Error: {html.escape(error)}"
        )
        await self.send_notification(text)

    async def notify_post_action_blocked(
        self, account: str, error: str,
    ) -> None:
        """Operator alert — Instagram returned action_blocked / challenge.

        Auto-quarantine is disabled (operator request 2026-05-02). This
        is a notification only; the account stays active. Scheduler
        applies a 1h per-account cooldown so a buffered-replay or a
        flapping FSM cannot spam this alert.
        """
        text = (
            f"\U0001f6a8 <b>ACTION BLOCKED</b>\n"
            f"Account: <code>{html.escape(account)}</code>\n"
            f"Error: {html.escape(error)}\n"
            f"Account is still active — review IG manually and pause if needed."
        )
        await self.send_notification(text)

    async def notify_device_offline(self, device_name: str) -> None:
        text = f"\U0001f534 <b>Device offline</b>: {html.escape(device_name)}"
        await self.send_notification(text)

    async def notify_device_online(self, device_name: str) -> None:
        text = f"\U0001f7e2 <b>Device online</b>: {html.escape(device_name)}"
        await self.send_notification(text)

    # ------------------------------------------------------------------
    # Admin guard
    # ------------------------------------------------------------------

    async def _guard(self, update: Update) -> bool:
        """Return True if the user is authorized. Reply 'Unauthorized' otherwise."""
        user = update.effective_user
        if user is None or not _is_admin(user.id, self.config):
            target = update.message or (update.callback_query and update.callback_query.message)
            if target is not None:
                try:
                    await target.reply_text("Unauthorized")
                except Exception:
                    pass
            return False
        return True

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _cmd_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return
        text = (
            "\U0001f916 <b>Reelsomet VPS Bot</b>\n\n"
            "Available commands:\n"
            "/status \u2014 Dashboard summary\n"
            "/devices \u2014 Device list\n"
            "/accounts \u2014 Account list\n"
            "/queue \u2014 Video queue\n"
            "/logs \u2014 Recent activity\n"
            "/settings \u2014 Config summary\n"
            "/update \u2014 Check & apply updates\n"
            "/version \u2014 System info"
        )
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        async with self.session_factory() as session:
            total_devices = (await session.execute(
                select(func.count(Device.id)),
            )).scalar_one()
            online_ids = self.ws_manager.get_online_device_ids()
            online_count = len(online_ids)

            active_accounts = (await session.execute(
                select(func.count(Account.id)).where(Account.is_active.is_(True)),
            )).scalar_one()

            pending_count = (await session.execute(
                select(func.count(Video.id)).where(Video.status == "pending"),
            )).scalar_one()
            scheduled_count = (await session.execute(
                select(func.count(Video.id)).where(Video.status == "scheduled"),
            )).scalar_one()

            # Farm-local "today" boundary (Codex iter 4 bug hunt
            # 2026-04-14). UTC midnight was the wrong anchor for
            # operators running the VPS in UTC+N.
            from server import farm_time as _farm_time
            today_start, _today_end = _farm_time.local_day_bounds_utc(
                _farm_time.utcnow_naive(), self.config.farm_timezone,
            )
            posted_today = (await session.execute(
                select(func.count(Video.id)).where(
                    Video.status == "posted",
                    Video.posted_at >= today_start,
                ),
            )).scalar_one()
            # Use `updated_at` (status transition time) to match
            # dashboard_stats — `created_at` is when the Video row
            # was inserted which can be days before the failure,
            # producing phantom "failed today" counts (Codex iter 4
            # Q5 2026-04-14).
            failed_today = (await session.execute(
                select(func.count(Video.id)).where(
                    Video.status == "failed",
                    Video.updated_at >= today_start,
                ),
            )).scalar_one()

            engagement_running = (await session.execute(
                select(func.count(Account.id)).where(
                    Account.engagement_enabled.is_(True),
                ),
            )).scalar_one()

        eng_status = "running" if engagement_running > 0 else "disabled"
        text = (
            "\U0001f4ca <b>Reelsomet VPS Status</b>\n"
            "\u2501" * 20 + "\n"
            f"\U0001f4f1 Devices: {online_count} online / {total_devices} total\n"
            f"\U0001f464 Accounts: {active_accounts} active\n"
            f"\U0001f4f9 Queue: {pending_count} pending, {scheduled_count} scheduled\n"
            f"\u2705 Posted today: {posted_today}\n"
            f"\u274c Failed today: {failed_today}\n"
            f"\U0001f916 Engagement: {eng_status}"
        )
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_devices(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        async with self.session_factory() as session:
            result = await session.execute(select(Device).order_by(Device.id))
            devices = list(result.scalars().all())

        if not devices:
            await update.message.reply_text("No devices registered.")  # type: ignore[union-attr]
            return

        online_ids = self.ws_manager.get_online_device_ids()
        lines = ["\U0001f4f1 <b>Devices</b>", "\u2501" * 16]
        buttons: list[list[InlineKeyboardButton]] = []

        for i, dev in enumerate(devices, 1):
            is_online = dev.id in online_ids
            dot = "\U0001f7e2" if is_online else "\U0001f534"
            status_text = "Online" if is_online else "Offline"
            last_seen = _timeago(dev.last_seen_at)
            model = html.escape(dev.device_model or "unknown")
            name = html.escape(dev.name or f"Device {dev.id}")
            lines.append(
                f"\n{i}. {name} ({model})\n"
                f"   {dot} {status_text} | Last: {last_seen}"
            )
            buttons.append([
                InlineKeyboardButton("Ping", callback_data=f"dev:ping:{dev.id}"),
                InlineKeyboardButton("Details", callback_data=f"dev:detail:{dev.id}"),
            ])

        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore[union-attr]

    async def _cmd_accounts(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        async with self.session_factory() as session:
            result = await session.execute(select(Account).order_by(Account.id))
            accounts = list(result.scalars().all())

        if not accounts:
            await update.message.reply_text("No accounts registered.")  # type: ignore[union-attr]
            return

        lines = ["\U0001f464 <b>Accounts</b>", "\u2501" * 16]
        buttons: list[list[InlineKeyboardButton]] = []

        for i, acc in enumerate(accounts, 1):
            if acc.is_paused:
                status_icon = "\u23f8"
                status_text = "Paused"
            elif acc.is_blocked:
                status_icon = "\U0001f6ab"
                status_text = "Blocked"
            elif acc.is_active:
                status_icon = "\u2705"
                status_text = "Active"
            else:
                status_icon = "\u26aa"
                status_text = "Inactive"

            name = html.escape(acc.username)
            model_part = f" | Model: {html.escape(acc.recreator_model)}" if acc.recreator_model else ""
            lines.append(
                f"\n{i}. {name}\n"
                f"   {status_icon} {status_text} | Posted: {acc.total_posted}{model_part}"
            )

            toggle_label = "Resume" if acc.is_paused else "Pause"
            toggle_action = "acc:pause:" + acc.username
            buttons.append([
                InlineKeyboardButton(toggle_label, callback_data=toggle_action),
                InlineKeyboardButton("Details", callback_data=f"acc:detail:{acc.username}"),
            ])

        text = "\n".join(lines)
        keyboard = InlineKeyboardMarkup(buttons)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)  # type: ignore[union-attr]

    async def _cmd_queue(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        async with self.session_factory() as session:
            pending = (await session.execute(
                select(func.count(Video.id)).where(Video.status == "pending"),
            )).scalar_one()
            scheduled = (await session.execute(
                select(func.count(Video.id)).where(Video.status == "scheduled"),
            )).scalar_one()
            failed = (await session.execute(
                select(func.count(Video.id)).where(Video.status == "failed"),
            )).scalar_one()

            # Farm-local "today" boundary (Codex iter 4 bug hunt
            # 2026-04-14). UTC midnight was the wrong anchor for
            # operators running the VPS in UTC+N.
            from server import farm_time as _farm_time
            today_start, _today_end = _farm_time.local_day_bounds_utc(
                _farm_time.utcnow_naive(), self.config.farm_timezone,
            )
            posted_today = (await session.execute(
                select(func.count(Video.id)).where(
                    Video.status == "posted",
                    Video.posted_at >= today_start,
                ),
            )).scalar_one()

            # Recent videos (last 5)
            recent_result = await session.execute(
                select(Video)
                .where(Video.status.in_(["pending", "scheduled", "posted", "failed"]))
                .order_by(Video.created_at.desc())
                .limit(5),
            )
            recent_videos = list(recent_result.scalars().all())

        lines = [
            "\U0001f4f9 <b>Video Queue</b>",
            "\u2501" * 16,
            f"\U0001f4e6 Pending: {pending}",
            f"\U0001f4e4 Scheduled: {scheduled}",
            f"\U0001f4ee Posted today: {posted_today}",
            f"\u274c Failed: {failed}",
        ]

        if recent_videos:
            lines.append("\nRecent:")
            for vid in recent_videos:
                acct = html.escape(vid.account_username)
                fname = html.escape(vid.filename)
                if vid.status == "posted":
                    icon = "\u2705 Posted"
                elif vid.status == "failed":
                    icon = "\u274c Failed"
                elif vid.status == "scheduled" and vid.scheduled_time:
                    time_str = vid.scheduled_time.strftime("%H:%M")
                    icon = f"\u23f0 {time_str}"
                else:
                    icon = "\U0001f4e6 Pending"
                lines.append(f"\u2022 {acct} \u2014 {fname} \u2014 {icon}")

        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        async with self.session_factory() as session:
            result = await session.execute(
                select(PostLog).order_by(PostLog.timestamp.desc()).limit(10),
            )
            logs = list(result.scalars().all())

        if not logs:
            await update.message.reply_text("No recent activity.")  # type: ignore[union-attr]
            return

        lines = ["\U0001f4cb <b>Recent Activity</b>", "\u2501" * 16]
        for log in logs:
            ts = log.timestamp.strftime("%H:%M") if log.timestamp else "??:??"
            acct = html.escape(log.account_username)
            if log.result == "success":
                dur = f" ({log.duration_ms / 1000:.0f}s)" if log.duration_ms else ""
                lines.append(f"{ts} \u2705 {acct} posted{dur}")
            else:
                err = html.escape(log.error_message or "unknown error")
                lines.append(f"{ts} \u274c {acct} failed: {err}")

        text = "\n".join(lines)
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    async def _cmd_settings(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        cfg = self.config
        domain = html.escape(cfg.domain or f"{cfg.host}:{cfg.port}")

        async with self.session_factory() as session:
            insights_on = (await session.execute(
                select(func.count(Account.id)).where(Account.insights_enabled.is_(True)),
            )).scalar_one()
            engagement_on = (await session.execute(
                select(func.count(Account.id)).where(Account.engagement_enabled.is_(True)),
            )).scalar_one()

        insights_status = f"{insights_on} accounts" if insights_on else "disabled"
        engagement_status = f"{engagement_on} accounts" if engagement_on else "disabled"

        text = (
            "\u2699\ufe0f <b>Settings</b>\n"
            "\u2501" * 16 + "\n"
            f"Domain: {domain}\n"
            f"LLM model: {html.escape(cfg.llm_model or 'not set')}\n"
            f"Insights: {insights_status}\n"
            f"Engagement: {engagement_status}"
        )
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Update & version commands
    # ------------------------------------------------------------------

    async def _cmd_update(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        msg = await update.message.reply_text(  # type: ignore[union-attr]
            "\U0001f50d Checking for updates..."
        )

        result = await check_for_updates()

        if not result["available"]:
            await msg.edit_text("\u2705 Already up to date")
            return

        commits = result["commits"]
        summary = html.escape(result["summary"])
        text = (
            "\U0001f4e6 <b>Update Available</b>\n"
            "\u2501" * 20 + "\n"
            f"{commits} new commit{'s' if commits != 1 else ''}:\n"
            f"<pre>{summary}</pre>\n"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("\u2705 Apply Update", callback_data="update:apply"),
                InlineKeyboardButton("\u274c Cancel", callback_data="update:cancel"),
            ],
        ])
        await msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _cmd_version(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if not await self._guard(update):
            return

        info = get_system_info()

        # Gather extra info: device count, license status
        online_count = len(self.ws_manager.get_online_device_ids())
        async with self.session_factory() as session:
            total_devices = (await session.execute(
                select(func.count(Device.id)),
            )).scalar_one()

        text = (
            "\u2139\ufe0f <b>System Info</b>\n"
            "\u2501" * 20 + "\n"
            f"Version: {html.escape(info['version'])}\n"
            f"Uptime: {html.escape(info['uptime_human'])}\n"
            f"Python: {html.escape(info['python_version'])}\n"
            f"OS: {html.escape(info['platform'])}\n"
            f"Devices: {online_count} online / {total_devices} total"
        )
        await update.message.reply_text(text, parse_mode="HTML")  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Callback query handler (inline buttons)
    # ------------------------------------------------------------------

    async def _on_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if query is None:
            return
        await query.answer()

        user = update.effective_user
        if user is None or not _is_admin(user.id, self.config):
            await query.edit_message_text("Unauthorized")
            return

        data = query.data or ""
        parts = data.split(":")
        if len(parts) < 2:
            await query.edit_message_text("Invalid callback data.")
            return

        category = parts[0]
        action = parts[1]
        identifier = ":".join(parts[2:]) if len(parts) > 2 else ""

        if category == "update":
            await self._handle_update_callback(query, action)
        elif len(parts) < 3:
            await query.edit_message_text("Invalid callback data.")
            return
        elif category == "dev":
            await self._handle_device_callback(query, action, identifier)
        elif category == "acc":
            await self._handle_account_callback(query, action, identifier)
        else:
            await query.edit_message_text("Unknown action.")

    async def _handle_device_callback(
        self, query: Any, action: str, device_id_str: str,
    ) -> None:
        try:
            device_id = int(device_id_str)
        except ValueError:
            await query.edit_message_text("Invalid device ID.")
            return

        if action == "ping":
            if not self.ws_manager.is_online(device_id):
                await query.edit_message_text(
                    f"\U0001f534 Device {device_id} is offline \u2014 cannot ping.",
                )
                return
            try:
                ok = await self.ws_manager.ping_device(device_id, timeout=5.0)
                if ok:
                    await query.edit_message_text(
                        f"\U0001f7e2 Device {device_id}: pong received",
                    )
                else:
                    await query.edit_message_text(
                        f"\U0001f534 Device {device_id}: ping timeout",
                    )
            except Exception as exc:
                await query.edit_message_text(
                    f"\u26a0\ufe0f Ping error: {html.escape(str(exc))}",
                )

        elif action == "detail":
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Device).where(Device.id == device_id),
                )
                dev = result.scalar_one_or_none()

            if dev is None:
                await query.edit_message_text("Device not found.")
                return

            is_online = self.ws_manager.is_online(dev.id)
            dot = "\U0001f7e2" if is_online else "\U0001f534"
            text = (
                f"\U0001f4f1 <b>{html.escape(dev.name or 'Device')}</b>\n\n"
                f"ID: {dev.id}\n"
                f"Serial: <code>{html.escape(dev.device_id)}</code>\n"
                f"Model: {html.escape(dev.device_model or 'unknown')}\n"
                f"Android: {html.escape(dev.android_version or 'unknown')}\n"
                f"IP: {html.escape(dev.ip_address)}:{dev.port}\n"
                f"Status: {dot} {'Online' if is_online else 'Offline'}\n"
                f"Last seen: {_timeago(dev.last_seen_at)}\n"
                f"App version: {html.escape(dev.app_version or 'unknown')}"
            )
            await query.edit_message_text(text, parse_mode="HTML")
        else:
            await query.edit_message_text("Unknown device action.")

    async def _handle_account_callback(
        self, query: Any, action: str, username: str,
    ) -> None:
        if action == "pause":
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Account).where(Account.username == username),
                )
                acc = result.scalar_one_or_none()
                if acc is None:
                    await query.edit_message_text("Account not found.")
                    return

                acc.is_paused = not acc.is_paused
                new_state = "paused" if acc.is_paused else "resumed"
                await session.commit()

            await query.edit_message_text(
                f"\u2705 <b>{html.escape(username)}</b> {new_state}",
                parse_mode="HTML",
            )

        elif action == "detail":
            async with self.session_factory() as session:
                result = await session.execute(
                    select(Account).where(Account.username == username),
                )
                acc = result.scalar_one_or_none()

            if acc is None:
                await query.edit_message_text("Account not found.")
                return

            if acc.is_paused:
                status = "\u23f8 Paused"
            elif acc.is_blocked:
                status = "\U0001f6ab Blocked"
            elif acc.is_active:
                status = "\u2705 Active"
            else:
                status = "\u26aa Inactive"

            last_posted = _timeago(acc.last_posted_at)
            text = (
                f"\U0001f464 <b>{html.escape(acc.username)}</b>\n\n"
                f"Status: {status}\n"
                f"Posted: {acc.total_posted} | Failed: {acc.total_failed}\n"
                f"Last posted: {last_posted}\n"
                f"Model: {html.escape(acc.recreator_model or 'none')}\n"
                f"Insights: {'enabled' if acc.insights_enabled else 'disabled'}\n"
                f"Engagement: {'enabled' if acc.engagement_enabled else 'disabled'}"
            )
            await query.edit_message_text(text, parse_mode="HTML")
        else:
            await query.edit_message_text("Unknown account action.")

    async def _handle_update_callback(self, query: Any, action: str) -> None:
        if action == "cancel":
            await query.edit_message_text("\u274c Update cancelled.")
            return

        if action == "apply":
            await query.edit_message_text(
                "\u23f3 Applying update... Server will restart in a few seconds.",
            )
            result = await apply_update()
            if result["success"]:
                text = (
                    "\u2705 <b>Update applied</b>\n\n"
                    f"{html.escape(result['message'])}\n\n"
                )
                if result.get("restart_scheduled"):
                    text += "\U0001f504 Server restarting now..."
                await query.edit_message_text(text, parse_mode="HTML")
            else:
                text = (
                    "\u274c <b>Update failed</b>\n\n"
                    f"{html.escape(result['message'])}"
                )
                await query.edit_message_text(text, parse_mode="HTML")
            return

        await query.edit_message_text("Unknown update action.")
