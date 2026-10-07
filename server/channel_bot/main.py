"""Channel Bot VPS entry point — standalone process with Telegram bot + APScheduler."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import load_config
from .database import _ensure_db
from .bot import ChannelBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("channel_bot")


def main(config_path: str | None = None):
    config = load_config(config_path)

    if not config.bot_token or config.bot_token in ("", "placeholder"):
        logger.error("Set channel_bot.bot_token in config.yaml")
        sys.exit(1)

    # Ensure DB directory & tables
    Path(config.db_path).parent.mkdir(parents=True, exist_ok=True)
    _ensure_db(config.db_path)

    bot = ChannelBot(config)
    app = bot.build()

    async def post_init(application):
        scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 600})
        bot.attach_scheduler(scheduler)
        scheduler.start()
        restored_pending, failed_pending = await bot.restore_pending_posts()

        hours_str = ", ".join(f"{h:02d}:00" for h in sorted(config.schedule_hours))
        channels_str = ", ".join(
            ch.channel_id or f"#{i}" for i, ch in enumerate(config.channels)
        ) or "(no channels)"

        logger.info("Channel Bot started")
        logger.info("  Channels: %s", channels_str)
        logger.info("  Schedule: %s (%s)", hours_str, config.timezone)
        logger.info("  LLM: %s / %s", config.llm_provider or "not set", config.llm_model or "not set")
        logger.info("  DB: %s", config.db_path)
        if restored_pending or failed_pending:
            logger.info("  Pending: restored=%d failed=%d", restored_pending, failed_pending)

        for chat_id in config.admin_chat_ids:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"Channel Bot started.\nChannels: {channels_str}\nSchedule: {hours_str}\n/status",
                )
            except Exception:
                pass

    app.post_init = post_init
    logger.info("Starting Channel Bot...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    config_path = None
    if len(sys.argv) > 1 and sys.argv[1] == "--config":
        config_path = sys.argv[2]
    main(config_path)
