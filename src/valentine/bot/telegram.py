# src/valentine/bot/telegram.py
import json
import logging
import asyncio
import uuid

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from valentine.models import (
    IncomingMessage, AgentTask, RoutingDecision, TaskResult,
    ContentType, AgentName, MessageSource,
)
from valentine.bus.redis_bus import RedisBus
from valentine.config import settings

logger = logging.getLogger(__name__)

class TelegramBot:
    def __init__(self, bus: RedisBus):
        self.bus = bus
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self._setup_handlers()

    def _setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Hey! I'm Valentine — your AI assistant. How can I help you today?")

    async def _route_message(self, update: Update, content_type: ContentType, text: str, media_path: str = None):
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
        logger.info(f"Routing message {msg.message_id} to zeroclaw.route")

        # Wrap in an AgentTask envelope so ZeroClaw can deserialise it
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent=AgentName.ZEROCLAW,
            routing=RoutingDecision(intent="incoming", agent=AgentName.ZEROCLAW),
            message=msg,
        )
        try:
            # Publish to the Redis Stream that ZeroClaw reads via xreadgroup
            await self.bus.add_task(self.bus.ROUTER_STREAM, task.to_dict())
        except Exception as e:
            logger.error(f"Failed to publish to zeroclaw.route: {e}")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action(action="typing")
        user_text = update.message.text
        if user_text:
            await self._route_message(update, ContentType.TEXT, user_text)

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action(action="typing")
        photo_file = await update.message.photo[-1].get_file()
        file_path = f"/tmp/{photo_file.file_id}.jpg"
        await photo_file.download_to_drive(custom_path=file_path)
        
        caption = update.message.caption or ""
        await self._route_message(update, ContentType.PHOTO, caption, media_path=file_path)

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.chat.send_action(action="record_voice")
        voice = update.message.voice or update.message.audio
        voice_file = await voice.get_file()
        file_path = f"/tmp/{voice_file.file_id}.ogg"
        await voice_file.download_to_drive(custom_path=file_path)
        
        await self._route_message(update, ContentType.VOICE, "", media_path=file_path)

    async def listen_for_responses(self):
        pubsub = self.bus.redis.pubsub()
        await pubsub.subscribe("agent.response")
        logger.info("Bot subscribed to agent.response")
        
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    result = TaskResult.from_dict(data)
                    
                    if not result.chat_id:
                        logger.warning(f"Result for task {result.task_id} missing chat_id, cannot send.")
                        continue
                        
                    if result.success:
                        if result.content_type == ContentType.TEXT:
                            await self.app.bot.send_message(chat_id=result.chat_id, text=result.text)
                        elif result.content_type == ContentType.PHOTO:
                            await self.app.bot.send_photo(chat_id=result.chat_id, photo=result.media_path, caption=result.text)
                        elif result.content_type == ContentType.VOICE:
                            await self.app.bot.send_voice(chat_id=result.chat_id, voice=result.media_path, caption=result.text)
                    else:
                        await self.app.bot.send_message(chat_id=result.chat_id, text=f"I'm sorry, an error occurred during processing: {result.error}")
                        
                except Exception as e:
                    logger.error(f"Error handling agent response in bot: {e}")

    async def start(self):
        logger.info("Starting Telegram Bot...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        
        # Start background listener for responses
        asyncio.create_task(self.listen_for_responses())

    async def stop(self):
        logger.info("Stopping Telegram Bot...")
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
