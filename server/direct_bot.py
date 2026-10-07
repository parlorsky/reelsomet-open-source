"""
Direct Bot — Telegram Channel DM Auto-Responder.

Subscribes to Channel Direct Messages (Bot API 9.2+).
Bot must be added as channel admin with `can_manage_direct_messages`.
DMs arrive as updates in the linked supergroup (chat.is_direct_messages=True),
each subscriber gets a topic (message.direct_messages_topic.topic_id).

Usage:
    python -m pc.direct_bot
"""

import collections
import logging

import httpx
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes

from server.config import load_config
from server.llm_client import resolve_base_url, _call_openai_compat, _call_anthropic

logger = logging.getLogger("direct_bot")

DEFAULT_SYSTEM_PROMPT = """\
Ты — девушка которая ведёт канал. Общаешься с подписчиками в личных сообщениях.

Правила:
- Ты общаешься как живая девушка в мессенджере — легко, с интересом, флиртуя
- Проявляй искренний интерес к собеседнику, задавай вопросы ("а ты чем занимаешься?", "расскажи о себе")
- Если человек написал что-то непонятное или абракадабру — пошути ("это шифр? 😄", "переведи на русский 😂")
- Поддерживай разговор, НЕ заканчивай диалог, НЕ прощайся, НЕ отправляй делать дела
- Пиши коротко: 1-3 предложения
- Используй эмодзи умеренно (1-2 на сообщение)
- Если тема неудобная — плавно переведи на другую, но НЕ отталкивай
- Будь тёплой, игривой и открытой к общению
- Никогда не говори что ты бот или ИИ
- Отвечай по-русски, в разговорном стиле мессенджера\
"""


def _format_history(system_prompt: str, history: collections.deque) -> str:
    """Flatten system prompt + conversation history into a single prompt string."""
    parts = [system_prompt, "\n\nИстория переписки:"]
    for msg in history:
        role = msg["role"]
        prefix = "Собеседник" if role == "user" else "Ты"
        parts.append(f"{prefix}: {msg['content']}")
    parts.append("\nОтветь (только текст ответа, без префикса):")
    return "\n".join(parts)


class DirectBot:
    def __init__(self):
        config = load_config()
        raw_cfg = {}
        try:
            import yaml
            from pathlib import Path
            p = Path(config._config_path)
            if p.exists():
                raw_cfg = (yaml.safe_load(p.read_text()) or {}).get("direct_bot", {})
        except Exception:
            pass

        self.token = raw_cfg.get("telegram_bot_token", "")
        self.memory_size = raw_cfg.get("memory_size", 10)
        self.system_prompt = raw_cfg.get("system_prompt", "") or DEFAULT_SYSTEM_PROMPT

        # LLM — use the global VPS LLM config
        self.llm_provider = config.llm_provider
        self.llm_base_url = resolve_base_url(config.llm_provider, config.llm_base_url)
        self.llm_api_key = config.llm_api_key
        self.llm_model = config.llm_model
        self.llm_timeout = config.engagement_llm_timeout

        # Memory: (chat_id, topic_id) -> deque of {role, content}
        # OrderedDict for LRU eviction — max 1000 conversations
        self.conversations: collections.OrderedDict[tuple[int, int], collections.deque] = collections.OrderedDict()
        self._max_conversations = 1000
        self._bot_id: int | None = None
        # Track message IDs sent by the bot to filter out echo updates
        self._own_message_ids: collections.OrderedDict[int, None] = collections.OrderedDict()

    def build(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(MessageHandler(
            filters.DIRECT_MESSAGES & ~filters.COMMAND,
            self.handle_dm,
        ))
        app.post_init = self._post_init
        return app

    async def _post_init(self, application: Application) -> None:
        self._bot_id = application.bot.id
        logger.info("Bot initialized: @%s (id=%s)", application.bot.username, self._bot_id)

    async def _ask_llm(self, prompt: str) -> str | None:
        """Send prompt to LLM via multi-provider client.

        Returns ``None`` when no LLM provider is configured so callers
        (which already handle None / empty replies) can skip sending a
        reply instead of crashing. Transient provider errors still raise
        and are handled by the caller's existing try/except.
        """
        if not self.llm_provider:
            logger.debug("LLM provider unset; skipping reply")
            return None
        if self.llm_provider == "anthropic":
            return await _call_anthropic(
                self.llm_base_url, self.llm_api_key, self.llm_model,
                "", prompt, self.llm_timeout,
            )
        else:
            return await _call_openai_compat(
                self.llm_base_url, self.llm_api_key, self.llm_model,
                "", prompt, self.llm_timeout,
            )

    def _is_own_message(self, msg) -> bool:
        """Check if message was sent by this bot (multiple strategies)."""
        # Strategy 1: track sent message IDs
        if msg.message_id in self._own_message_ids:
            return True
        # Strategy 2: from_user matches bot ID
        if msg.from_user and msg.from_user.id == self._bot_id:
            return True
        return False

    def _remember_own_message(self, message_id: int) -> None:
        self._own_message_ids[message_id] = None
        self._own_message_ids.move_to_end(message_id)
        while len(self._own_message_ids) > 1000:
            self._own_message_ids.popitem(last=False)

    async def handle_dm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        if not msg:
            return

        # Ignore own messages (bot echo)
        if self._is_own_message(msg):
            logger.debug("Skipping own message %s", msg.message_id)
            return

        topic = msg.direct_messages_topic
        if not topic:
            return
        chat_id = msg.chat.id
        topic_id = topic.topic_id
        conv_key = (chat_id, topic_id)

        # Extract text from any message type
        user_text = msg.text or msg.caption or ""
        if not user_text:
            # Describe non-text content so LLM can react
            if msg.photo:
                user_text = "[отправил(а) фото]"
            elif msg.video:
                user_text = "[отправил(а) видео]"
            elif msg.sticker:
                emoji = msg.sticker.emoji or ""
                user_text = f"[отправил(а) стикер {emoji}]"
            elif msg.voice:
                user_text = "[отправил(а) голосовое сообщение]"
            elif msg.video_note:
                user_text = "[отправил(а) кружочек]"
            elif msg.document:
                user_text = "[отправил(а) файл]"
            elif msg.animation:
                user_text = "[отправил(а) гифку]"
            else:
                user_text = "[отправил(а) что-то]"

        user_name = msg.from_user.first_name if msg.from_user else "unknown"
        logger.info("[chat:%s topic:%s] %s: %s", chat_id, topic_id, user_name, user_text)

        # Typing indicator (not supported in channel DM chats, ignore errors)
        try:
            await context.bot.send_chat_action(msg.chat.id, "typing")
        except Exception:
            pass

        # Append user message to memory (LRU: move to end on access)
        if conv_key in self.conversations:
            self.conversations.move_to_end(conv_key)
        history = self.conversations.setdefault(
            conv_key, collections.deque(maxlen=self.memory_size * 2),
        )
        # Evict oldest conversations if over limit
        while len(self.conversations) > self._max_conversations:
            self.conversations.popitem(last=False)
        history.append({"role": "user", "content": user_text})

        # Build prompt from history and call LLM
        prompt = _format_history(self.system_prompt, history)

        try:
            reply_text = await self._ask_llm(prompt)
        except Exception:
            logger.exception("LLM request failed for chat %s topic %s", chat_id, topic_id)
            # Remove the user message we appended before the failed LLM call
            if history and history[-1].get("role") == "user":
                history.pop()
            return

        # _ask_llm returns None when no LLM provider is configured — silently
        # drop the turn so the bot keeps running in stub mode.
        if reply_text is None:
            if history and history[-1].get("role") == "user":
                history.pop()
            return

        # Clean up common LLM prefixes
        reply_text = reply_text.strip()
        for prefix in ("Ты:", "Даша:", "Бот:"):
            if reply_text.lower().startswith(prefix.lower()):
                reply_text = reply_text[len(prefix):].strip()

        logger.info("[chat:%s topic:%s] reply: %s", chat_id, topic_id, reply_text)

        # Save assistant reply to memory
        if not reply_text:
            logger.warning("Empty LLM reply for chat %s topic %s; skipping send", chat_id, topic_id)
            return
        history.append({"role": "assistant", "content": reply_text})

        # Send reply in the same DM topic
        sent = await context.bot.send_message(
            chat_id=msg.chat.id,
            text=reply_text,
            direct_messages_topic_id=topic_id,
        )
        # Track sent message ID to filter echo
        self._remember_own_message(sent.message_id)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    bot = DirectBot()
    if not bot.token:
        logger.error("Set direct_bot.telegram_bot_token in config.yaml")
        return
    app = bot.build()
    logger.info("Direct Bot starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
