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
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("tour", self.tour_command))
        self.app.add_handler(CommandHandler("workbench", self.workbench_command))
        self.app.add_handler(MessageHandler(filters.TEXT, self.handle_text))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self.handle_voice))

    async def _set_grouped_commands(self):
        # Grouping commands to top 10 as requested by user
        commands = [
            ("start", "Boot up Valentine"),
            ("help", "See the full command list & guide"),
            ("tour", "Interactive walkthrough of capabilities"),
            ("workbench", "Open the web GUI for files & sessions"),
            ("new", "Start a new conversation thread"),
            ("conversations", "List all active threads"),
            ("resume", "Switch to a specific thread"),
            ("skills", "List installed agent skills"),
            ("status", "Check agent memory and system health"),
            ("tts", "Speak text (e.g. /tts Hello world)")
        ]
        from telegram import BotCommand
        await self.app.bot.set_my_commands([BotCommand(k, v) for k, v in commands])

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from telegram import ReplyKeyboardMarkup
        keyboard = [
            ["/tour 🚀", "/workbench 💻"],
            ["/skills 🛠️", "/help 📖"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Hey! I'm Valentine — your AI agent and personal assistant. How can I help you today?",
            reply_markup=reply_markup
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "🤖 **Valentine Command Reference**\n\n"
            "**Core**\n"
            "• `/tour` - Interactive feature showcase\n"
            "• `/workbench` - Open the desktop GUI view\n\n"
            "**Sessions & Memory**\n"
            "• `/new [name]` - Start a fresh project context\n"
            "• `/conversations` - List all your threads\n"
            "• `/resume [id]` - Jump into an old thread\n\n"
            "**Tools & Agents**\n"
            "• `/skills` - See what Valentine can do (Social Media, Coding, etc)\n"
            "• `/status` - View running agents & CPU\n\n"
            "**Voice**\n"
            "• `/tts [text]` - Valentine speaks the text back (or just send a Voice Note!)\n\n"
            "💡 *Tip: Just talk naturally. I know when to search, write code, or create images automatically.*"
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def tour_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        tour_text = (
            "🚀 **Welcome to the Valentine Tour!**\n\n"
            "I'm a multi-agent AI system. I don't just chat—I execute.\n\n"
            "1️⃣ **Social Media Manager**: I can design ad campaigns, write viral threads, and handle crisis PR.\n"
            "2️⃣ **Principal Engineer**: I can write code, run shell commands, deploy to Vercel/Render, and manage GitHub.\n"
            "3️⃣ **Researcher**: I can crawl the web and synthesize deep data.\n"
            "4️⃣ **Memory**: I remember everything we discuss across sessions.\n\n"
            "Try sending me a voice note, asking me to generate an image, or telling me to scrape a website!"
        )
        keyboard = [[InlineKeyboardButton("Open Workbench 💻", web_app={"url": "https://valentine-app-demo.vercel.app/"})]] # Placeholder URL since actual Workbench URL needs to be tunnelled or configured
        await update.message.reply_text(tour_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    async def workbench_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        # Providing a WebApp button. If the user doesn't have a public HTTPS URL for the workbench yet, this will just show the button.
        keyboard = [[InlineKeyboardButton("Launch Workbench 💻", web_app={"url": "https://p-f646b9ec.trycloudflare.com/"})]] # Since the app runs locally normally, they'd use a tunnel. We will use a generic placeholder or real URL if known. Let's use loopback or local IP if applicable, or tell them to use Cloudflare. We'll use a placeholder for now, but tell them in text.
        text = "Click below to launch the Valentine Workbench Mini-App. Ensure your local tunnel is running."
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

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
        await self._set_grouped_commands()
        await self.app.updater.start_polling()
        
        # Start background listener for responses
        asyncio.create_task(self.listen_for_responses())

    async def stop(self):
        logger.info("Stopping Telegram Bot...")
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
