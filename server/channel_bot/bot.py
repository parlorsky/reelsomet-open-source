"""Telegram Channel Bot — admin commands, scheduling, auto-posting (multi-channel)."""

import asyncio
import html as _html
import logging
import random
import time
from pathlib import Path

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputPaidMediaPhoto
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from .config import ChannelBotConfig, ChannelConfig, save_shared_key, save_channel_key
from .database import (
    add_example,
    clear_examples,
    get_all_posts,
    get_example_count,
    get_examples,
    get_last_post_type,
    load_pending_posts,
    get_post_count,
    mark_posted,
    mark_failed,
    save_pending_payload,
    save_post,
)
from .generator import generate_post, generate_paid_caption, generate_poll, analyze_style

logger = logging.getLogger("channel_bot.bot")

# Conversation states
SET_LEGEND = 0
ADD_EXAMPLES = 1
SELECT_CHANNEL_LEGEND = 2
SELECT_CHANNEL_EXAMPLES = 3


APPROVAL_TIMEOUT = 600  # 10 minutes


class ChannelBot:
    def __init__(self, config: ChannelBotConfig):
        self.config = config
        self.app: Application = None
        self._scheduler = None
        self._legend_buffer: dict = {}  # chat_id -> list of text chunks
        self._legend_channel_idx: dict = {}  # chat_id -> channel index
        self._examples_count: dict = {}  # chat_id -> count added in session
        self._examples_channel_idx: dict = {}  # chat_id -> channel index
        self._pending: dict = {}  # post_id -> {text, channel_id, channel_idx, is_paid, photo, star_count, caption, timer}
        self._background_tasks: set = set()  # prevent GC of fire-and-forget tasks

    def _is_admin(self, update: Update) -> bool:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        return chat_id in self.config.admin_chat_ids

    def _channel(self, idx: int) -> ChannelConfig | None:
        """Get channel by index, or None."""
        if 0 <= idx < len(self.config.channels):
            return self.config.channels[idx]
        return None

    def _serialize_pending_info(self, info: dict) -> dict:
        photo = info.get("photo")
        return {
            "text": info.get("text", ""),
            "caption": info.get("caption", ""),
            "channel_id": info.get("channel_id", ""),
            "channel_idx": info.get("channel_idx", -1),
            "is_paid": bool(info.get("is_paid")),
            "is_poll": bool(info.get("is_poll")),
            "photo": str(photo) if photo else "",
            "star_count": int(info.get("star_count", 0) or 0),
            "admin_msg_ids": {
                str(chat_id): int(msg_id)
                for chat_id, msg_id in (info.get("admin_msg_ids") or {}).items()
            },
            "approval_deadline": float(info.get("approval_deadline") or 0.0),
        }

    def _persist_pending_state(self, post_id: int) -> None:
        info = self._pending.get(post_id)
        if not info:
            return
        save_pending_payload(
            self.config.db_path,
            post_id,
            self._serialize_pending_info(info),
        )

    def _schedule_auto_approve(self, post_id: int, deadline_ts: float) -> None:
        info = self._pending.get(post_id)
        if not info:
            return
        delay = max(0.0, deadline_ts - time.time())
        loop = asyncio.get_running_loop()
        def _create_approve_task(pid=post_id):
            task = loop.create_task(self._safe_auto_approve(pid))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        info["timer"] = loop.call_later(delay, _create_approve_task)

    async def restore_pending_posts(self) -> tuple[int, int]:
        """Restore draft approvals after restart and reschedule auto-publish."""
        restored = 0
        failed = 0

        for row in load_pending_posts(self.config.db_path):
            post_id = int(row["id"])
            payload = row.get("pending_payload") or {}
            if not payload:
                mark_failed(self.config.db_path, post_id, "pending approval state missing after restart")
                failed += 1
                continue

            channel_id = payload.get("channel_id") or row.get("channel_id") or ""
            channel_idx = self._find_channel_idx(channel_id)
            if channel_idx < 0:
                try:
                    channel_idx = int(payload.get("channel_idx", -1))
                except Exception:
                    channel_idx = -1
            if not self._channel(channel_idx):
                mark_failed(
                    self.config.db_path,
                    post_id,
                    f"channel not found for pending post: {channel_id or channel_idx}",
                )
                failed += 1
                continue

            photo_raw = payload.get("photo") or ""
            photo = Path(photo_raw) if photo_raw else None
            if photo is not None and not photo.exists():
                mark_failed(self.config.db_path, post_id, f"pending photo missing: {photo}")
                failed += 1
                continue

            admin_msg_ids = {}
            for chat_id, msg_id in (payload.get("admin_msg_ids") or {}).items():
                try:
                    admin_msg_ids[int(chat_id)] = int(msg_id)
                except Exception:
                    continue

            approval_deadline = float(payload.get("approval_deadline") or 0.0)
            if approval_deadline <= 0:
                approval_deadline = time.time() + APPROVAL_TIMEOUT

            self._pending[post_id] = {
                "text": payload.get("text", row.get("text", "")),
                "caption": payload.get("caption", ""),
                "channel_id": channel_id,
                "channel_idx": channel_idx,
                "is_paid": bool(payload.get("is_paid")),
                "is_poll": bool(payload.get("is_poll")),
                "photo": photo,
                "star_count": int(payload.get("star_count", 0) or 0),
                "admin_msg_ids": admin_msg_ids,
                "approval_deadline": approval_deadline,
            }
            self._persist_pending_state(post_id)
            self._schedule_auto_approve(post_id, approval_deadline)
            restored += 1

        if restored or failed:
            logger.info("Pending approvals restored: %d restored, %d failed", restored, failed)
        return restored, failed

    def attach_scheduler(self, scheduler) -> None:
        """Attach APScheduler instance and build current hourly jobs."""
        self._scheduler = scheduler
        self.refresh_schedule_jobs()

    def refresh_schedule_jobs(self) -> None:
        """Rebuild hourly auto-post cron jobs from current config."""
        if not self._scheduler:
            return

        # Remove previously configured channel-posting jobs.
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("channel_post_"):
                self._scheduler.remove_job(job.id)

        # Recreate jobs with fresh random minute offsets.
        for hour in sorted(set(self.config.schedule_hours)):
            minute = random.randint(1, 55)
            self._scheduler.add_job(
                self.auto_post_all,
                "cron",
                hour=hour,
                minute=minute,
                timezone=self.config.timezone,
                id=f"channel_post_{hour}",
            )
            logger.info("  Scheduled %02d:%02d", hour, minute)

    def build(self) -> Application:
        """Build the telegram Application with all handlers."""
        from telegram.request import HTTPXRequest

        request = HTTPXRequest(
            read_timeout=120,
            write_timeout=120,
            connect_timeout=60,
            pool_timeout=60,
        )
        self.app = (
            Application.builder()
            .token(self.config.bot_token)
            .request(request)
            .build()
        )

        # ConversationHandlers
        legend_conv = ConversationHandler(
            entry_points=[CommandHandler("setlegend", self._cmd_setlegend_start)],
            states={
                SELECT_CHANNEL_LEGEND: [
                    CallbackQueryHandler(self._setlegend_select_channel, pattern=r"^chbot:leg_ch:\d+$"),
                ],
                SET_LEGEND: [
                    CommandHandler("done", self._cmd_setlegend_done),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._setlegend_text),
                ],
            },
            fallbacks=[CommandHandler("cancel", self._conv_cancel)],
            conversation_timeout=600,
        )

        examples_conv = ConversationHandler(
            entry_points=[CommandHandler("addexamples", self._cmd_addexamples_start)],
            states={
                SELECT_CHANNEL_EXAMPLES: [
                    CallbackQueryHandler(self._addexamples_select_channel, pattern=r"^chbot:ex_ch:\d+$"),
                ],
                ADD_EXAMPLES: [
                    CommandHandler("done", self._cmd_addexamples_done),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._addexamples_text),
                ],
            },
            fallbacks=[CommandHandler("cancel", self._conv_cancel)],
            conversation_timeout=600,
        )

        # Register handlers (order matters)
        self.app.add_handler(legend_conv)
        self.app.add_handler(examples_conv)
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("addchannel", self._cmd_addchannel))
        self.app.add_handler(CommandHandler("generate", self._cmd_generate))
        self.app.add_handler(CommandHandler("preview", self._cmd_preview))
        self.app.add_handler(CommandHandler("history", self._cmd_history))
        self.app.add_handler(CommandHandler("schedule", self._cmd_schedule))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("clearexamples", self._cmd_clearexamples))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Auto-collect channel posts (match any configured channel)
        self.app.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POST, self._on_channel_post),
            group=1,
        )

        return self.app

    # ── Helpers ────────────────────────────────────────

    def _channel_label(self, idx: int) -> str:
        """Short label for a channel: index + channel_id."""
        ch = self._channel(idx)
        if not ch:
            return f"#{idx}"
        return ch.channel_id or f"#{idx} (не задан)"

    def _channel_selector_kb(self, prefix: str) -> InlineKeyboardMarkup:
        """Build inline keyboard for channel selection."""
        rows = []
        for i, ch in enumerate(self.config.channels):
            label = ch.channel_id or f"#{i} (не задан)"
            desc = ch.channel_description[:30] if ch.channel_description else ""
            btn_text = f"{label}"
            if desc:
                btn_text += f" — {desc}"
            rows.append([InlineKeyboardButton(btn_text, callback_data=f"{prefix}{i}")])
        return InlineKeyboardMarkup(rows)

    def _find_channel_idx(self, channel_id_or_username: str) -> int:
        """Find channel index by channel_id or @username. Returns -1 if not found."""
        for i, ch in enumerate(self.config.channels):
            if ch.channel_id == channel_id_or_username:
                return i
        return -1

    # ── Commands ──────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await self._send_status(update.message)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await self._send_status(update.message)

    async def _send_status(self, message):
        """Build and send status panel with all channels."""
        c = self.config
        status = "на паузе" if c.is_paused else "активен"
        hours = ", ".join(str(h) for h in sorted(c.schedule_hours)) if c.schedule_hours else "—"

        lines = [
            "<b>Channel Bot</b>",
            f"Статус: {status}",
            f"Расписание: {hours} ({c.timezone})",
            f"Каналов: {len(c.channels)}",
            "",
        ]

        for i, ch in enumerate(c.channels):
            channel_name = ch.channel_id or "<i>не задан</i>"
            example_count = get_example_count(c.db_path, channel_id=ch.channel_id)
            posted_count = get_post_count(c.db_path, channel_id=ch.channel_id, status="posted")
            legend_preview = ch.legend[:60] + "..." if len(ch.legend) > 60 else ch.legend
            legend_preview = _html.escape(legend_preview) if legend_preview else "<i>нет</i>"

            paid_photos = self._count_paid_photos(ch)
            paid_info = (
                f"{paid_photos} фото, {int(ch.paid_probability * 100)}%, "
                f"{ch.paid_star_count_min}-{ch.paid_star_count_max}"
                if paid_photos > 0
                else "нет"
            )

            lines.append(f"<b>Канал #{i}: {channel_name}</b>")
            lines.append(f"  Легенда: {legend_preview}")
            lines.append(f"  Примеров: {example_count} | Постов: {posted_count}")
            lines.append(f"  Paid: {paid_info}")
            lines.append("")

        text = "\n".join(lines)

        rows = []
        # Per-channel generate/preview buttons
        for i, ch in enumerate(c.channels):
            label = ch.channel_id or f"#{i}"
            rows.append([
                InlineKeyboardButton(f"Генерировать {label}", callback_data=f"chbot:gen:{i}"),
                InlineKeyboardButton(f"Превью {label}", callback_data=f"chbot:preview:{i}"),
            ])

        rows.append([
            InlineKeyboardButton("Расписание", callback_data="chbot:schedule"),
            InlineKeyboardButton("История", callback_data="chbot:history"),
        ])

        # Paid buttons per channel
        for i, ch in enumerate(c.channels):
            if self._count_paid_photos(ch) > 0:
                label = ch.channel_id or f"#{i}"
                rows.append([
                    InlineKeyboardButton(
                        f"Paid {label} ({ch.paid_star_count_min}-{ch.paid_star_count_max})",
                        callback_data=f"chbot:paid:{i}",
                    ),
                ])

        rows.append([
            InlineKeyboardButton(
                "Возобновить" if c.is_paused else "Пауза",
                callback_data="chbot:resume" if c.is_paused else "chbot:pause",
            ),
        ])
        kb = InlineKeyboardMarkup(rows)
        await message.reply_html(text, reply_markup=kb)

    async def _cmd_addchannel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        args = context.args
        if not args:
            await update.message.reply_html(
                "Укажите ID канала:\n<code>/addchannel @channelname</code>\n"
                "или <code>/addchannel -100123456789</code>\n\n"
                "Бот должен быть администратором канала."
            )
            return

        channel_id = args[0]

        # Verify bot has access
        try:
            chat = await context.bot.get_chat(channel_id)
        except Exception as e:
            await update.message.reply_html(
                f"Не удалось подключиться к каналу: {_html.escape(str(e))}\n\n"
                "Убедитесь, что бот добавлен как администратор канала."
            )
            return

        # Check if already exists
        existing_idx = self._find_channel_idx(channel_id)
        if existing_idx >= 0:
            await update.message.reply_html(
                f"Канал {_html.escape(chat.title or channel_id)} уже есть (#{existing_idx})"
            )
            return

        # Add new channel
        new_ch = ChannelConfig(channel_id=channel_id)
        self.config.channels.append(new_ch)
        idx = len(self.config.channels) - 1
        save_channel_key(self.config, idx, "channel_id", channel_id)

        await update.message.reply_html(
            f"Канал #{idx} добавлен: <b>{_html.escape(chat.title or channel_id)}</b>\n\n"
            f"Теперь задайте легенду: /setlegend\n"
            f"И добавьте примеры: /addexamples"
        )

    async def _cmd_generate(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        if not self.config.channels:
            await update.message.reply_text("Нет каналов. Добавьте: /addchannel @channel")
            return

        # If one channel, generate directly; if multiple, show selector
        if len(self.config.channels) == 1:
            await self._generate_for_channel(update, context, 0)
        else:
            await update.message.reply_html(
                "Выберите канал для генерации:",
                reply_markup=self._channel_selector_kb("chbot:gen:"),
            )

    async def _cmd_preview(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        if not self.config.channels:
            await update.message.reply_text("Нет каналов. Добавьте: /addchannel @channel")
            return

        if len(self.config.channels) == 1:
            await self._preview_for_channel(update, context, 0)
        else:
            await update.message.reply_html(
                "Выберите канал для превью:",
                reply_markup=self._channel_selector_kb("chbot:preview:"),
            )

    async def _generate_for_channel(self, update, context, idx: int, query=None):
        """Generate and post to a specific channel."""
        ch = self._channel(idx)
        if not ch or not ch.channel_id:
            text = "Канал не настроен."
            if query:
                await query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return

        status_text = f"Генерирую пост для {ch.channel_id}..."
        if query:
            await query.edit_message_text(status_text)
        else:
            await update.message.reply_text(status_text)

        try:
            text = await generate_post(self.config, ch, self.config.db_path)
            post_id = save_post(self.config.db_path, text, ch.channel_id)
            msg_id, is_poll = await self._send_post_or_poll(ch.channel_id, text)
            mark_posted(self.config.db_path, post_id, msg_id)
            post_type = "Опрос" if is_poll else "Пост"
            result = f"{post_type} опубликован в {ch.channel_id}!\n\n<i>{_html.escape(text[:300])}</i>"
            if query:
                await query.edit_message_text(result, parse_mode="HTML")
            else:
                await update.message.reply_html(result)
        except Exception as e:
            logger.error("Generate & post failed for %s: %s", ch.channel_id, e, exc_info=True)
            err_text = f"Ошибка: {_html.escape(str(e))}"
            if query:
                await query.edit_message_text(err_text, parse_mode="HTML")
            else:
                await update.message.reply_html(err_text)

    async def _preview_for_channel(self, update, context, idx: int, query=None):
        """Generate preview for a specific channel (no publish)."""
        ch = self._channel(idx)
        if not ch:
            return

        status_text = f"Генерирую превью для {ch.channel_id or f'#{idx}'}..."
        if query:
            await query.edit_message_text(status_text)
        else:
            await update.message.reply_text(status_text)

        try:
            text = await generate_post(self.config, ch, self.config.db_path)
            result = f"<b>Превью [{ch.channel_id}]</b> (не опубликован):\n\n{_html.escape(text)}"
            if query:
                await query.edit_message_text(result, parse_mode="HTML")
            else:
                await update.message.reply_html(result)
        except Exception as e:
            logger.error("Preview failed for %s: %s", ch.channel_id, e, exc_info=True)
            err_text = f"Ошибка: {_html.escape(str(e))}"
            if query:
                await query.edit_message_text(err_text, parse_mode="HTML")
            else:
                await update.message.reply_html(err_text)

    async def _cmd_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        posts = get_all_posts(self.config.db_path, limit=10)
        if not posts:
            await update.message.reply_text("Нет постов в истории.")
            return

        lines = ["<b>Последние посты:</b>\n"]
        for p in posts:
            status_icon = {"posted": "✅", "draft": "📝", "failed": "❌"}.get(
                p["status"], "❓"
            )
            ch_tag = p.get("channel_id", "")
            if ch_tag:
                ch_tag = f" [{ch_tag}]"
            preview = _html.escape(p["text"][:80]) + ("..." if len(p["text"]) > 80 else "")
            ts = p.get("posted_at") or p["created_at"]
            ts_short = ts[:16] if ts else "?"
            lines.append(f"{status_icon} <code>{ts_short}</code>{ch_tag} {preview}")

        await update.message.reply_html("\n".join(lines))

    async def _cmd_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        await update.message.reply_html(
            "<b>Расписание постинга</b>\n"
            "Выберите часы (зелёный = активен):",
            reply_markup=self._build_schedule_kb(),
        )

    def _build_schedule_kb(self) -> InlineKeyboardMarkup:
        """Build inline keyboard with hour toggles."""
        active = set(self.config.schedule_hours)
        rows = []
        for row_start in range(0, 24, 6):
            row = []
            for h in range(row_start, min(row_start + 6, 24)):
                label = f"{'🟢' if h in active else '⚪'} {h:02d}"
                row.append(InlineKeyboardButton(label, callback_data=f"chbot:hr:{h}"))
            rows.append(row)
        rows.append([InlineKeyboardButton("💾 Сохранить", callback_data="chbot:save_sched")])
        return InlineKeyboardMarkup(rows)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        self.config.is_paused = True
        save_shared_key(self.config, "is_paused", True)
        await update.message.reply_text("Автопилот приостановлен. /resume чтобы возобновить.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        self.config.is_paused = False
        save_shared_key(self.config, "is_paused", False)
        await update.message.reply_text("Автопилот возобновлён!")

    async def _cmd_clearexamples(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        if not self.config.channels:
            await update.message.reply_text("Нет каналов.")
            return

        if len(self.config.channels) == 1:
            ch = self.config.channels[0]
            count = clear_examples(self.config.db_path, channel_id=ch.channel_id)
            await update.message.reply_text(f"Удалено {count} примеров для {ch.channel_id}.")
        else:
            # Show channel selector
            rows = []
            for i, ch in enumerate(self.config.channels):
                label = ch.channel_id or f"#{i}"
                ex_count = get_example_count(self.config.db_path, channel_id=ch.channel_id)
                rows.append([InlineKeyboardButton(
                    f"{label} ({ex_count} примеров)",
                    callback_data=f"chbot:clearex:{i}",
                )])
            rows.append([InlineKeyboardButton("Все каналы", callback_data="chbot:clearex:all")])
            await update.message.reply_html(
                "Очистить примеры для какого канала?",
                reply_markup=InlineKeyboardMarkup(rows),
            )

    # ── Conversation: /setlegend ──────────────────────

    async def _cmd_setlegend_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return ConversationHandler.END
        if not self.config.channels:
            await update.message.reply_text("Сначала добавьте канал: /addchannel @channel")
            return ConversationHandler.END

        if len(self.config.channels) == 1:
            # Skip selection, go directly to text input
            chat_id = update.effective_chat.id
            self._legend_buffer[chat_id] = []
            self._legend_channel_idx[chat_id] = 0
            ch = self.config.channels[0]
            current = ch.legend
            text = (
                f"<b>Установка легенды для {ch.channel_id or '#0'}</b>\n\n"
                "Отправьте текст персоны/бэкстори (можно несколькими сообщениями).\n"
                "/done — сохранить, /cancel — отмена\n"
            )
            if current:
                text += f"\nТекущая легенда:\n<i>{_html.escape(current[:200])}</i>"
            await update.message.reply_html(text)
            return SET_LEGEND

        # Multiple channels — show selector
        await update.message.reply_html(
            "Выберите канал для установки легенды:",
            reply_markup=self._channel_selector_kb("chbot:leg_ch:"),
        )
        return SELECT_CHANNEL_LEGEND

    async def _setlegend_select_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split(":")[-1])
        chat_id = update.effective_chat.id
        self._legend_buffer[chat_id] = []
        self._legend_channel_idx[chat_id] = idx
        ch = self._channel(idx)
        current = ch.legend if ch else ""
        text = (
            f"<b>Установка легенды для {self._channel_label(idx)}</b>\n\n"
            "Отправьте текст персоны/бэкстори (можно несколькими сообщениями).\n"
            "/done — сохранить, /cancel — отмена\n"
        )
        if current:
            text += f"\nТекущая легенда:\n<i>{_html.escape(current[:200])}</i>"
        await query.edit_message_text(text, parse_mode="HTML")
        return SET_LEGEND

    async def _setlegend_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self._legend_buffer.setdefault(chat_id, []).append(update.message.text)
        count = len(self._legend_buffer[chat_id])
        await update.message.reply_text(f"Принято ({count} часть). Ещё текст или /done")
        return SET_LEGEND

    async def _cmd_setlegend_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        parts = self._legend_buffer.pop(chat_id, [])
        idx = self._legend_channel_idx.pop(chat_id, 0)
        if not parts:
            await update.message.reply_text("Нет текста. Отправьте текст или /cancel")
            return SET_LEGEND
        legend = "\n\n".join(parts)
        save_channel_key(self.config, idx, "legend", legend)
        await update.message.reply_html(
            f"Легенда для {self._channel_label(idx)} сохранена ({len(legend)} симв.):\n\n"
            f"<i>{_html.escape(legend[:300])}</i>"
        )
        return ConversationHandler.END

    # ── Conversation: /addexamples ────────────────────

    async def _cmd_addexamples_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return ConversationHandler.END
        if not self.config.channels:
            await update.message.reply_text("Сначала добавьте канал: /addchannel @channel")
            return ConversationHandler.END

        if len(self.config.channels) == 1:
            chat_id = update.effective_chat.id
            self._examples_count[chat_id] = 0
            self._examples_channel_idx[chat_id] = 0
            ch = self.config.channels[0]
            existing = get_example_count(self.config.db_path, channel_id=ch.channel_id)
            await update.message.reply_html(
                f"<b>Добавление примеров для {ch.channel_id or '#0'}</b>\n\n"
                f"Отправляйте посты по одному (каждое сообщение = 1 пост).\n"
                f"/done — завершить, /cancel — отмена\n\n"
                f"Сейчас в базе: {existing} примеров"
            )
            return ADD_EXAMPLES

        await update.message.reply_html(
            "Выберите канал для добавления примеров:",
            reply_markup=self._channel_selector_kb("chbot:ex_ch:"),
        )
        return SELECT_CHANNEL_EXAMPLES

    async def _addexamples_select_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split(":")[-1])
        chat_id = update.effective_chat.id
        self._examples_count[chat_id] = 0
        self._examples_channel_idx[chat_id] = idx
        ch = self._channel(idx)
        channel_id = ch.channel_id if ch else ""
        existing = get_example_count(self.config.db_path, channel_id=channel_id)
        await query.edit_message_text(
            f"<b>Добавление примеров для {self._channel_label(idx)}</b>\n\n"
            f"Отправляйте посты по одному (каждое сообщение = 1 пост).\n"
            f"/done — завершить, /cancel — отмена\n\n"
            f"Сейчас в базе: {existing} примеров",
            parse_mode="HTML",
        )
        return ADD_EXAMPLES

    async def _addexamples_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = update.message.text.strip()
        if not text:
            return ADD_EXAMPLES
        idx = self._examples_channel_idx.get(chat_id, 0)
        ch = self._channel(idx)
        channel_id = ch.channel_id if ch else ""
        add_example(self.config.db_path, text, channel_id=channel_id, source="admin")
        self._examples_count[chat_id] = self._examples_count.get(chat_id, 0) + 1
        count = self._examples_count[chat_id]
        await update.message.reply_text(f"Пример #{count} сохранён. Ещё или /done")
        return ADD_EXAMPLES

    async def _cmd_addexamples_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        added = self._examples_count.pop(chat_id, 0)
        idx = self._examples_channel_idx.pop(chat_id, 0)
        ch = self._channel(idx)
        channel_id = ch.channel_id if ch else ""
        total = get_example_count(self.config.db_path, channel_id=channel_id)

        # Quick style analysis
        examples = get_examples(self.config.db_path, channel_id=channel_id, limit=self.config.style_sample_count)
        style = analyze_style(examples)

        await update.message.reply_html(
            f"Добавлено: {added} примеров для {self._channel_label(idx)} (всего: {total})\n\n"
            f"<b>Анализ стиля:</b>\n"
            f"Средняя длина: ~{style['avg_length']} симв.\n"
            f"Эмодзи: {style['emoji_frequency']}\n"
            f"Параграфы: {style['paragraph_style']}\n"
            f"Хэштеги: {'да' if style['uses_hashtags'] else 'нет'}\n"
            f"Язык: {style['language']}"
        )
        return ConversationHandler.END

    # ── Conversation utils ────────────────────────────

    async def _conv_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Отменено.")
        return ConversationHandler.END

    # ── Callback handler ──────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update):
            return
        query = update.callback_query
        await query.answer()
        data = query.data

        try:
            if data.startswith("chbot:gen:"):
                idx = int(data.split(":")[2])
                await self._generate_for_channel(update, context, idx, query=query)

            elif data.startswith("chbot:preview:"):
                idx = int(data.split(":")[2])
                await self._preview_for_channel(update, context, idx, query=query)

            elif data == "chbot:history":
                posts = get_all_posts(self.config.db_path, limit=10)
                if not posts:
                    await query.edit_message_text("Нет постов в истории.")
                    return
                lines = ["<b>Последние посты:</b>\n"]
                for p in posts:
                    icon = {"posted": "✅", "draft": "📝", "failed": "❌"}.get(
                        p["status"], "❓"
                    )
                    ch_tag = p.get("channel_id", "")
                    if ch_tag:
                        ch_tag = f" [{ch_tag}]"
                    preview = _html.escape(p["text"][:80])
                    ts = (p.get("posted_at") or p["created_at"])[:16]
                    lines.append(f"{icon} <code>{ts}</code>{ch_tag} {preview}")
                await query.edit_message_text("\n".join(lines), parse_mode="HTML")

            elif data == "chbot:schedule":
                await query.edit_message_text(
                    "<b>Расписание постинга</b>\nВыберите часы:",
                    parse_mode="HTML",
                    reply_markup=self._build_schedule_kb(),
                )

            elif data.startswith("chbot:hr:"):
                hour = int(data.split(":")[2])
                hours = set(self.config.schedule_hours)
                if hour in hours:
                    hours.discard(hour)
                else:
                    hours.add(hour)
                self.config.schedule_hours = sorted(hours)
                await self._safe_edit(
                    query,
                    "<b>Расписание постинга</b>\nВыберите часы:",
                    reply_markup=self._build_schedule_kb(),
                )

            elif data == "chbot:save_sched":
                save_shared_key(self.config, "schedule_hours", self.config.schedule_hours)
                self.refresh_schedule_jobs()
                hours_str = ", ".join(
                    f"{h:02d}:00" for h in sorted(self.config.schedule_hours)
                )
                await query.edit_message_text(
                    f"Расписание сохранено:\n{hours_str}",
                    parse_mode="HTML",
                )

            elif data == "chbot:pause":
                self.config.is_paused = True
                save_shared_key(self.config, "is_paused", True)
                await query.edit_message_text("Автопилот приостановлен. /status")

            elif data == "chbot:resume":
                self.config.is_paused = False
                save_shared_key(self.config, "is_paused", False)
                await query.edit_message_text("Автопилот возобновлён! /status")

            elif data.startswith("chbot:paid:"):
                idx = int(data.split(":")[2])
                ch = self._channel(idx)
                if not ch or not ch.channel_id:
                    await query.edit_message_text("Канал не настроен.")
                    return
                photo = self._pick_random_photo(ch)
                if not photo:
                    await query.edit_message_text("Нет фото в paid_media_dir")
                    return
                await query.edit_message_text(f"Генерирую paid пост для {ch.channel_id}...")
                caption = await generate_paid_caption(self.config, ch, db_path=self.config.db_path)
                star_count = self._random_star_count(ch)
                post_id = save_post(
                    self.config.db_path,
                    f"[PAID {star_count}] {caption}",
                    ch.channel_id,
                )
                msg_id, stars = await self._send_paid_post(ch, photo, caption, star_count)
                mark_posted(self.config.db_path, post_id, msg_id)
                await query.edit_message_text(
                    f"Paid пост для {ch.channel_id} ({stars})!\n"
                    f"Фото: {photo.name}\n\n{_html.escape(caption[:300])}",
                    parse_mode="HTML",
                )

            elif data.startswith("chbot:ok:"):
                post_id = int(data.split(":")[2])
                if post_id in self._pending:
                    await query.edit_message_text("Публикую...")
                    await self._do_publish(post_id)
                else:
                    await query.edit_message_text("Пост уже обработан.")

            elif data.startswith("chbot:regen:"):
                post_id = int(data.split(":")[2])
                if post_id in self._pending:
                    await query.edit_message_text("Регенерирую...")
                    await self._do_regen(post_id)
                else:
                    await query.edit_message_text("Пост уже обработан.")

            elif data.startswith("chbot:drop:"):
                post_id = int(data.split(":")[2])
                if post_id in self._pending:
                    await self._do_drop(post_id)
                else:
                    await query.edit_message_text("Пост уже обработан.")

            elif data.startswith("chbot:clearex:"):
                target = data.split(":")[2]
                if target == "all":
                    count = clear_examples(self.config.db_path)
                    await query.edit_message_text(f"Удалено {count} примеров (все каналы).")
                else:
                    idx = int(target)
                    ch = self._channel(idx)
                    if ch:
                        count = clear_examples(self.config.db_path, channel_id=ch.channel_id)
                        await query.edit_message_text(
                            f"Удалено {count} примеров для {ch.channel_id}."
                        )

        except BadRequest as e:
            if "not modified" not in str(e).lower():
                await query.edit_message_text(f"Ошибка: {_html.escape(str(e))}", parse_mode="HTML")
        except Exception as e:
            logger.error("Callback error: %s", e, exc_info=True)
            try:
                await query.edit_message_text(f"Ошибка: {_html.escape(str(e))}", parse_mode="HTML")
            except Exception:
                pass

    @staticmethod
    async def _safe_edit(query, text: str, parse_mode: str = "HTML", reply_markup=None):
        try:
            await query.edit_message_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    # ── Channel post auto-collector ───────────────────

    async def _on_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Auto-collect posts from any monitored channel."""
        if not update.channel_post or not update.channel_post.text:
            return
        post_channel_id = str(update.channel_post.chat.id)
        post_username = update.channel_post.chat.username

        # Match against configured channels
        for ch in self.config.channels:
            if not ch.channel_id:
                continue
            if ch.channel_id == post_channel_id:
                add_example(self.config.db_path, update.channel_post.text,
                            channel_id=ch.channel_id, source="channel")
                return
            if post_username and f"@{post_username}" == ch.channel_id:
                add_example(self.config.db_path, update.channel_post.text,
                            channel_id=ch.channel_id, source="channel")
                return

    # ── Paid media helpers ────────────────────────────

    _PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

    def _get_paid_media_dir(self, channel: ChannelConfig) -> Path:
        """Resolve paid media directory path for a channel."""
        d = channel.paid_media_dir
        if not d:
            return Path()
        p = Path(d)
        if not p.is_absolute():
            # On VPS, resolve relative paths from data_dir
            p = Path(self.config.db_path).parent.parent / d
        return p

    def _count_paid_photos(self, channel: ChannelConfig) -> int:
        """Count available photos in paid_media_dir for a channel."""
        d = self._get_paid_media_dir(channel)
        if not d or not d.is_dir():
            return 0
        return sum(1 for f in d.iterdir() if f.suffix.lower() in self._PHOTO_EXTENSIONS)

    def _pick_random_photo(self, channel: ChannelConfig) -> Path | None:
        """Pick a random photo from paid_media_dir."""
        d = self._get_paid_media_dir(channel)
        if not d or not d.is_dir():
            return None
        photos = [f for f in d.iterdir() if f.suffix.lower() in self._PHOTO_EXTENSIONS]
        return random.choice(photos) if photos else None

    def _should_be_paid(self, channel: ChannelConfig) -> bool:
        """Decide if the next post should be paid content."""
        if not channel.paid_media_dir or channel.paid_probability <= 0:
            return False
        if self._count_paid_photos(channel) == 0:
            return False
        return random.random() < channel.paid_probability

    def _random_star_count(self, channel: ChannelConfig) -> int:
        """Random star count between min and max."""
        return random.randint(channel.paid_star_count_min, channel.paid_star_count_max)

    async def _send_paid_post(
        self, channel: ChannelConfig, photo_path: Path, caption: str, star_count: int
    ) -> tuple:
        """Send a paid media post to the channel.

        Returns (message_id, star_count) on success.
        """
        with open(photo_path, "rb") as f:
            msg = await self.app.bot.send_paid_media(
                chat_id=channel.channel_id,
                star_count=star_count,
                media=[InputPaidMediaPhoto(f)],
                caption=caption,
            )
        return msg.message_id, star_count

    # ── Poll helpers ────────────────────────────────────

    @staticmethod
    def _parse_poll(text: str) -> tuple:
        """Detect if generated text is a poll.

        Format: starts with /poll, first line = question, rest = options.
        Returns (question, [options]) or (None, None).
        """
        stripped = text.strip()
        if not stripped.lower().startswith("/poll"):
            return None, None
        lines = [line.strip() for line in stripped.split("\n") if line.strip()]
        # Remove the /poll prefix from first line
        first = lines[0]
        question = first[5:].strip().lstrip(":").strip() if len(first) > 5 else ""
        if not question and len(lines) > 1:
            question = lines[1]
            options = [line.lstrip("- ").lstrip("0123456789.)").strip() for line in lines[2:]]
        else:
            options = [line.lstrip("- ").lstrip("0123456789.)").strip() for line in lines[1:]]
        options = [o for o in options if o]
        if question and len(options) >= 2:
            return question, options[:10]  # Telegram max 10 options
        return None, None

    async def _send_post_or_poll(self, channel_id: str, text: str) -> tuple:
        """Send text as message or poll. Returns (message_id, is_poll)."""
        question, options = self._parse_poll(text)
        if question and options:
            msg = await self.app.bot.send_poll(
                chat_id=channel_id,
                question=question,
                options=options,
                is_anonymous=True,
            )
            return msg.message_id, True
        else:
            msg = await self.app.bot.send_message(chat_id=channel_id, text=text)
            return msg.message_id, False

    # ── Auto-posting (called by scheduler) ────────────

    # ── Approval flow ─────────────────────────────────

    async def _send_for_approval(self, idx: int, text: str, is_paid: bool,
                                  photo: Path | None = None, caption: str = "",
                                  star_count: int = 0):
        """Send generated post to admin for approval with auto-publish timer."""
        ch = self._channel(idx)
        if not ch:
            return

        if is_paid:
            post_id = save_post(
                self.config.db_path,
                f"[PAID {star_count}] {caption}",
                ch.channel_id,
            )
            preview = (
                f"<b>Paid пост для {ch.channel_id}</b> ({star_count} stars)\n"
                f"Фото: {photo.name if photo else '?'}\n\n"
                f"{_html.escape(caption)}"
            )
        else:
            post_id = save_post(self.config.db_path, text, ch.channel_id)
            question, options = self._parse_poll(text)
            if question:
                preview = (
                    f"<b>Опрос для {ch.channel_id}</b>\n\n"
                    f"<b>{_html.escape(question)}</b>\n"
                    + "\n".join(f"  - {_html.escape(o)}" for o in options)
                )
            else:
                preview = (
                    f"<b>Пост для {ch.channel_id}</b>\n\n"
                    f"{_html.escape(text)}"
                )

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Постить", callback_data=f"chbot:ok:{post_id}"),
                InlineKeyboardButton("Реген", callback_data=f"chbot:regen:{post_id}"),
                InlineKeyboardButton("Отмена", callback_data=f"chbot:drop:{post_id}"),
            ]
        ])

        # Detect if this is a poll
        is_poll = not is_paid and text.strip().lower().startswith("/poll")

        # Store pending data
        self._pending[post_id] = {
            "text": text,
            "caption": caption,
            "channel_id": ch.channel_id,
            "channel_idx": idx,
            "is_paid": is_paid,
            "is_poll": is_poll,
            "photo": photo,
            "star_count": star_count,
            "admin_msg_ids": {},  # chat_id -> message_id (for editing later)
            "approval_deadline": time.time() + APPROVAL_TIMEOUT,
        }
        self._persist_pending_state(post_id)

        # Send to all admins
        for chat_id in self.config.admin_chat_ids:
            try:
                msg = await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=preview + "\n\n<i>Авто-постинг через 10 мин</i>",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                self._pending[post_id]["admin_msg_ids"][chat_id] = msg.message_id
                self._persist_pending_state(post_id)
            except Exception as e:
                logger.warning("Failed to send approval to %s: %s", chat_id, e)

        # Schedule auto-publish after timeout
        self._schedule_auto_approve(post_id, self._pending[post_id]["approval_deadline"])

        logger.info("Sent post_id=%d for approval (channel=%s, paid=%s)",
                     post_id, ch.channel_id, is_paid)

    async def _safe_auto_approve(self, post_id: int):
        """Wrapper around _auto_approve that catches and logs exceptions."""
        try:
            await self._auto_approve(post_id)
        except Exception as e:
            logger.error("Auto-approve failed post_id=%d: %s", post_id, e, exc_info=True)

    async def _auto_approve(self, post_id: int):
        """Auto-publish after timeout if not yet handled."""
        if post_id not in self._pending:
            return
        logger.info("Auto-approving post_id=%d (timeout)", post_id)
        await self._do_publish(post_id, auto=True)

    async def _do_publish(self, post_id: int, auto: bool = False):
        """Actually publish a pending post."""
        info = self._pending.pop(post_id, None)
        if not info:
            return

        # Cancel timer if still pending
        timer = info.get("timer")
        if timer:
            timer.cancel()

        channel_id = info["channel_id"]
        tag = "авто" if auto else "одобрен"

        ch = self._channel(info["channel_idx"])
        if not ch:
            err = f"Channel #{info['channel_idx']} no longer exists"
            logger.error("Publish failed post_id=%d: %s", post_id, err)
            mark_failed(self.config.db_path, post_id, err)
            return

        try:
            if info["is_paid"]:
                msg_id, stars = await self._send_paid_post(
                    ch,
                    info["photo"],
                    info["caption"],
                    info["star_count"],
                )
                mark_posted(self.config.db_path, post_id, msg_id)
                result = f"Paid пост ({tag}) в {channel_id} ({stars} stars)"
                logger.info("Published PAID post_id=%d to %s (%s)", post_id, channel_id, tag)
            else:
                msg_id, is_poll = await self._send_post_or_poll(channel_id, info["text"])
                mark_posted(self.config.db_path, post_id, msg_id)
                post_type = "Опрос" if is_poll else "Пост"
                result = f"{post_type} ({tag}) в {channel_id}"
                logger.info("Published %s post_id=%d to %s (%s)",
                            post_type, post_id, channel_id, tag)

            # Update admin messages
            for chat_id, admin_msg_id in info.get("admin_msg_ids", {}).items():
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=admin_msg_id,
                        text=f"{result}",
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error("Publish failed post_id=%d: %s", post_id, e, exc_info=True)
            mark_failed(self.config.db_path, post_id, str(e))
            for chat_id, admin_msg_id in info.get("admin_msg_ids", {}).items():
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=admin_msg_id,
                        text=f"Ошибка публикации: {_html.escape(str(e))}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    async def _do_regen(self, post_id: int):
        """Regenerate text for a pending post and re-send for approval."""
        info = self._pending.pop(post_id, None)
        if not info:
            return

        timer = info.get("timer")
        if timer:
            timer.cancel()

        mark_failed(self.config.db_path, post_id, "regenerated")
        idx = info["channel_idx"]
        ch = self._channel(idx)
        if not ch:
            return

        # Update admin messages
        for chat_id, admin_msg_id in info.get("admin_msg_ids", {}).items():
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=admin_msg_id,
                    text="Регенерирую...",
                )
            except Exception:
                pass

        try:
            if info["is_paid"]:
                caption = await generate_paid_caption(self.config, ch, db_path=self.config.db_path)
                await self._send_for_approval(
                    idx, "", True, info["photo"], caption, info["star_count"],
                )
            elif info.get("is_poll"):
                text = await generate_poll(self.config, ch, self.config.db_path)
                await self._send_for_approval(idx, text, False)
            else:
                text = await generate_post(self.config, ch, self.config.db_path)
                await self._send_for_approval(idx, text, False)
        except Exception as e:
            logger.error("Regen failed: %s", e, exc_info=True)
            for chat_id in self.config.admin_chat_ids:
                try:
                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=f"Ошибка регенерации: {_html.escape(str(e))}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    async def _do_drop(self, post_id: int):
        """Cancel a pending post."""
        info = self._pending.pop(post_id, None)
        if not info:
            return

        timer = info.get("timer")
        if timer:
            timer.cancel()

        mark_failed(self.config.db_path, post_id, "rejected by admin")
        logger.info("Dropped post_id=%d", post_id)

        for chat_id, admin_msg_id in info.get("admin_msg_ids", {}).items():
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=admin_msg_id,
                    text="Пост отменён.",
                )
            except Exception:
                pass

    # ── Auto-posting (called by scheduler) ────────────

    # Rotation: regular -> paid -> poll -> regular -> ...
    _ROTATION = {"regular": "paid", "paid": "poll", "poll": "regular"}

    def _get_next_post_type(self, channel: ChannelConfig) -> str:
        """Determine next post type based on rotation pattern."""
        last = get_last_post_type(self.config.db_path, channel.channel_id)
        next_type = self._ROTATION.get(last, "regular")

        # If paid is next but no photos available, skip to poll
        if next_type == "paid":
            if not channel.paid_media_dir or self._count_paid_photos(channel) == 0:
                next_type = "poll"

        return next_type

    async def auto_post_all(self):
        """Generate posts for all channels and send for approval. Called by APScheduler."""
        if self.config.is_paused:
            logger.info("Auto-post skipped: paused")
            return
        if not self.config.channels:
            logger.warning("Auto-post skipped: no channels configured")
            return

        for idx, ch in enumerate(self.config.channels):
            if not ch.channel_id:
                logger.warning("Auto-post skipped channel #%d: no channel_id", idx)
                continue

            post_type = self._get_next_post_type(ch)
            logger.info("Generating for %s (type=%s)...", ch.channel_id, post_type)

            try:
                if post_type == "paid":
                    photo = self._pick_random_photo(ch)
                    if not photo:
                        # Fallback to poll if no photo found
                        post_type = "poll"

                if post_type == "paid":
                    caption = await generate_paid_caption(self.config, ch, db_path=self.config.db_path)
                    star_count = self._random_star_count(ch)
                    await self._send_for_approval(
                        idx, "", True, photo, caption, star_count,
                    )
                elif post_type == "poll":
                    try:
                        text = await generate_poll(self.config, ch, self.config.db_path)
                    except Exception as poll_err:
                        logger.warning("Poll generation failed, falling back to regular: %s", poll_err)
                        text = await generate_post(self.config, ch, self.config.db_path)
                    await self._send_for_approval(idx, text, False)
                else:
                    text = await generate_post(self.config, ch, self.config.db_path)
                    await self._send_for_approval(idx, text, False)

            except Exception as e:
                logger.error("Generate failed for %s: %s", ch.channel_id, e, exc_info=True)
                for chat_id in self.config.admin_chat_ids:
                    try:
                        await self.app.bot.send_message(
                            chat_id=chat_id,
                            text=f"Генерация не удалась для {ch.channel_id}: {e}",
                        )
                    except Exception:
                        pass
