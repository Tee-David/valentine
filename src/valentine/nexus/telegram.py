# src/valentine/nexus/telegram.py
"""Full-featured Telegram platform adapter implementing PlatformAdapter ABC."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from typing import Any

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
)

from valentine.bus.redis_bus import RedisBus
from valentine.config import settings
from valentine.models import (
    AgentName, AgentTask, ContentType, IncomingMessage,
    MessageSource, RoutingDecision, TaskResult,
)
from valentine.nexus.adapter import PlatformAdapter

logger = logging.getLogger(__name__)

# Telegram rate-limit: ~30 messages/sec globally, 1 msg/sec per chat
_CHAT_MIN_INTERVAL = 1.0  # seconds between messages to the same chat


class TelegramAdapter(PlatformAdapter):
    """Production Telegram adapter with media, rate-limiting, and error handling."""

    def __init__(self, bus: RedisBus):
        self.bus = bus
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self._last_send: dict[str, float] = defaultdict(float)
        self._response_task: asyncio.Task | None = None
        self._setup_handlers()

    # ------------------------------------------------------------------
    # PlatformAdapter properties
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "telegram"

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
        )
        self.app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))
        self.app.add_handler(
            MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice)
        )
        self.app.add_handler(
            MessageHandler(filters.Document.ALL, self._on_document)
        )
        self.app.add_handler(MessageHandler(filters.VIDEO, self._on_video))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.info("TelegramAdapter starting…")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        self._response_task = asyncio.create_task(self._listen_for_responses())
        logger.info("TelegramAdapter running.")

    async def stop(self) -> None:
        logger.info("TelegramAdapter stopping…")
        if self._response_task:
            self._response_task.cancel()
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("TelegramAdapter stopped.")

    # ------------------------------------------------------------------
    # Inbound: Telegram → Redis
    # ------------------------------------------------------------------

    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Hello! I'm Valentine v2 — your multi-agent AI assistant. Send me anything."
        )

    async def _route(
        self,
        update: Update,
        content_type: ContentType,
        text: str,
        media_path: str | None = None,
    ):
        msg = IncomingMessage(
            message_id=str(update.message.message_id),
            user_id=str(update.effective_user.id),
            chat_id=str(update.effective_chat.id),
            platform=MessageSource.TELEGRAM,
            content_type=content_type,
            text=text,
            media_path=media_path,
            timestamp=update.message.date,
        )
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=msg,
        )
        try:
            await self.bus.add_task(self.bus.ROUTER_STREAM, task.to_dict())
        except Exception as e:
            logger.error(f"Failed to route message {msg.message_id}: {e}")

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self.send_typing(str(update.effective_chat.id))
        text = update.message.text
        if text:
            await self._route(update, ContentType.TEXT, text)

    async def _on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self.send_typing(str(update.effective_chat.id))
        path = await self.download_media(update.message.photo[-1])
        caption = update.message.caption or ""
        await self._route(update, ContentType.PHOTO, caption, media_path=path)

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self.send_typing(str(update.effective_chat.id))
        voice = update.message.voice or update.message.audio
        path = await self.download_media(voice)
        await self._route(update, ContentType.VOICE, "", media_path=path)

    async def _on_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self.send_typing(str(update.effective_chat.id))
        doc = update.message.document
        path = await self.download_media(doc)
        caption = update.message.caption or ""
        await self._route(update, ContentType.DOCUMENT, caption, media_path=path)

    async def _on_video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await self.send_typing(str(update.effective_chat.id))
        video = update.message.video
        path = await self.download_media(video)
        caption = update.message.caption or ""
        await self._route(update, ContentType.VIDEO, caption, media_path=path)

    # ------------------------------------------------------------------
    # Outbound: Redis → Telegram
    # ------------------------------------------------------------------

    async def _listen_for_responses(self):
        """Subscribe to agent.response pub/sub and relay results to Telegram."""
        pubsub = self.bus.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe("agent.response")
        logger.info("TelegramAdapter subscribed to agent.response")

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    result = TaskResult.from_dict(data)
                    await self.send_result(result)
                except Exception as e:
                    logger.error(f"Error handling agent response: {e}")
        except asyncio.CancelledError:
            await pubsub.unsubscribe("agent.response")
            await pubsub.close()

    async def send_result(self, result: TaskResult) -> None:
        if not result.chat_id:
            logger.warning(f"Result {result.task_id} has no chat_id — dropping.")
            return

        await self._rate_limit(result.chat_id)

        try:
            if not result.success:
                await self._send_with_retry(
                    self.app.bot.send_message,
                    chat_id=result.chat_id,
                    text=f"Sorry, something went wrong: {result.error}",
                )
                return

            if result.content_type == ContentType.PHOTO and result.media_path:
                await self._send_with_retry(
                    self.app.bot.send_photo,
                    chat_id=result.chat_id,
                    photo=result.media_path,
                    caption=(result.text or "")[:1024],
                )
            elif result.content_type == ContentType.VOICE and result.media_path:
                await self._send_with_retry(
                    self.app.bot.send_voice,
                    chat_id=result.chat_id,
                    voice=open(result.media_path, "rb"),
                )
            elif result.content_type == ContentType.DOCUMENT and result.media_path:
                await self._send_with_retry(
                    self.app.bot.send_document,
                    chat_id=result.chat_id,
                    document=open(result.media_path, "rb"),
                    caption=(result.text or "")[:1024],
                )
            else:
                # TEXT or fallback
                text = result.text or "(empty response)"
                # Telegram has a 4096-char limit per message
                for chunk in _chunk_text(text, 4096):
                    await self._send_with_retry(
                        self.app.bot.send_message,
                        chat_id=result.chat_id,
                        text=chunk,
                    )
        except Exception as e:
            logger.error(f"Failed to send result {result.task_id} to Telegram: {e}")

    # ------------------------------------------------------------------
    # Media download
    # ------------------------------------------------------------------

    async def download_media(self, file_ref: Any) -> str:
        tg_file = await file_ref.get_file()
        ext = _guess_extension(tg_file.file_path or "")
        local_path = f"/tmp/{tg_file.file_id}{ext}"
        await tg_file.download_to_drive(custom_path=local_path)
        logger.info(f"Downloaded media → {local_path}")
        return local_path

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def send_typing(self, chat_id: str) -> None:
        try:
            await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass  # best-effort

    # ------------------------------------------------------------------
    # Rate limiting & retry
    # ------------------------------------------------------------------

    async def _rate_limit(self, chat_id: str):
        """Enforce per-chat send interval to respect Telegram limits."""
        now = time.monotonic()
        elapsed = now - self._last_send[chat_id]
        if elapsed < _CHAT_MIN_INTERVAL:
            await asyncio.sleep(_CHAT_MIN_INTERVAL - elapsed)
        self._last_send[chat_id] = time.monotonic()

    @staticmethod
    async def _send_with_retry(send_fn, *, max_retries: int = 3, **kwargs):
        """Call a Telegram send method with retry on transient errors."""
        for attempt in range(max_retries):
            try:
                return await send_fn(**kwargs)
            except RetryAfter as e:
                logger.warning(f"Telegram rate-limited, retry after {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
            except (TimedOut, NetworkError) as e:
                wait = 2 ** attempt
                logger.warning(f"Telegram network error ({e}), retrying in {wait}s")
                await asyncio.sleep(wait)
        logger.error(f"Send failed after {max_retries} retries")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _guess_extension(file_path: str) -> str:
    """Extract file extension from a Telegram file path."""
    _, ext = os.path.splitext(file_path)
    return ext if ext else ".bin"


def _chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks of at most max_len characters."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
