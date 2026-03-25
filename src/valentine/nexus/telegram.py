# src/valentine/nexus/telegram.py
"""Full-featured Telegram platform adapter implementing PlatformAdapter ABC."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from typing import Any

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
)

from valentine.bus.redis_bus import RedisBus
from valentine.config import settings
from valentine.models import (
    AgentName, AgentTask, ContentType, IncomingMessage,
    MessageSource, RoutingDecision, TaskResult,
)
from valentine.nexus.adapter import PlatformAdapter
from valentine.access import AccessControl
from valentine.core.scheduler import Scheduler, parse_duration
from valentine.security import (
    sanitise_input, sanitise_output, detect_injection, detect_secrets,
    is_self_awareness_query, validate_media_extension,
    MAX_MEDIA_SIZE_MB
)

logger = logging.getLogger(__name__)

# Telegram rate-limit: ~30 messages/sec globally, 1 msg/sec per chat
_CHAT_MIN_INTERVAL = 1.0  # seconds between messages to the same chat


class TelegramAdapter(PlatformAdapter):
    """Production Telegram adapter with media, rate-limiting, and error handling."""

    # Suppress duplicate errors to the same chat within this window (seconds)
    _ERROR_DEDUP_WINDOW = 30.0

    # Dedup window: ignore Telegram updates we've already processed (survives restarts via Redis)
    _DEDUP_TTL = 300  # 5 minutes — updates older than this won't be re-delivered by Telegram anyway

    def __init__(self, bus: RedisBus):
        self.bus = bus
        self.app = Application.builder().token(settings.telegram_bot_token).build()
        self._last_send: dict[str, float] = defaultdict(float)
        self._last_error: dict[str, tuple[str, float]] = {}  # chat_id → (error_key, timestamp)
        self._response_task: asyncio.Task | None = None
        self._access: AccessControl | None = None  # initialized on start()
        self._typing_tasks: dict[str, asyncio.Task] = {}  # chat_id -> typing loop task
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
        # Core commands
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("tour", self._cmd_tour))
        self.app.add_handler(CallbackQueryHandler(self._handle_tour_callback, pattern='^tour_'))
        # Self-awareness
        self.app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        self.app.add_handler(CommandHandler("capabilities", self._cmd_capabilities))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        # Agent management
        self.app.add_handler(CommandHandler("agents", self._cmd_agents))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode))
        self.app.add_handler(CommandHandler("skills", self._cmd_skills))
        self.app.add_handler(CommandHandler("tools", self._cmd_tools))
        # Scheduling
        self.app.add_handler(CommandHandler("schedule", self._cmd_schedule))
        self.app.add_handler(CommandHandler("jobs", self._cmd_jobs))
        self.app.add_handler(CommandHandler("new", self._cmd_new))
        self.app.add_handler(CommandHandler("conversations", self._cmd_conversations))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        # Memory & history
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("forget", self._cmd_forget))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        # Voice
        self.app.add_handler(CommandHandler("tts", self._cmd_tts))
        # Mini App
        self.app.add_handler(CommandHandler("workbench", self._cmd_workbench))
        # Morning reports
        self.app.add_handler(CommandHandler("morning", self._cmd_morning))
        self.app.add_handler(CallbackQueryHandler(self._handle_morning_callback, pattern='^mr_'))
        # User management (admin only)
        self.app.add_handler(CommandHandler("users", self._cmd_users))
        self.app.add_handler(CommandHandler("allow", self._cmd_allow))
        self.app.add_handler(CommandHandler("revoke", self._cmd_revoke))
        self.app.add_handler(CommandHandler("access", self._cmd_access))
        # Admin
        self.app.add_handler(CommandHandler("restart", self._cmd_restart))
        # Message handlers (must be last)
        self.app.add_handler(
            MessageHandler(filters.StatusUpdate.WEB_APP_DATA, self._on_web_app_data)
        )
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
        self._access = AccessControl(self.bus.redis)
        await self.app.initialize()

        # Acquire exclusive polling lock — only one bot instance can poll at a time.
        # This prevents 409 Conflict from zombie processes that survive restarts.
        lock_key = "valentine:telegram:polling_lock"
        lock_value = str(uuid.uuid4())
        # Force-set the lock (overrides any stale lock from a dead process)
        await self.bus.redis.set(lock_key, lock_value, ex=300)  # 5 min TTL
        self._polling_lock_key = lock_key
        self._polling_lock_value = lock_value
        logger.info(f"Acquired exclusive polling lock: {lock_value[:8]}...")

        # Clear any stale webhook/polling session to prevent
        # "terminated by other getUpdates request" conflicts
        try:
            await self.app.bot.delete_webhook(drop_pending_updates=True)
            # Wait a moment for Telegram to release the old polling session
            import asyncio as _asyncio
            await _asyncio.sleep(1)
            logger.info("Cleared stale webhook/updates before starting polling.")
        except Exception as e:
            logger.warning(f"delete_webhook on startup failed (non-fatal): {e}")

        await self.app.start()

        # Register slash commands with Telegram so they appear in the "/" menu
        from telegram import BotCommand
        await self.app.bot.set_my_commands([
            BotCommand("start", "Start Valentine"),
            BotCommand("tour", "Interactive capabilities tour"),
            BotCommand("help", "List all commands"),
            BotCommand("whoami", "Valentine's identity and origins"),
            BotCommand("capabilities", "Everything I can do"),
            BotCommand("status", "System health and agent status"),
            BotCommand("agents", "List all active agents"),
            BotCommand("mode", "Show or change autonomy mode"),
            BotCommand("skills", "List installed skills"),
            BotCommand("tools", "List available MCP tools"),
            BotCommand("schedule", "Create a recurring task"),
            BotCommand("jobs", "List scheduled jobs"),
            BotCommand("new", "Start a new conversation session"),
            BotCommand("conversations", "List active sessions"),
            BotCommand("resume", "Switch to an older session"),
            BotCommand("memory", "Search my memory"),
            BotCommand("forget", "Remove a memory"),
            BotCommand("clear", "Clear conversation history"),
            BotCommand("tts", "Get a voice reply"),
            BotCommand("workbench", "Open Project Workbench Mini App"),
            BotCommand("morning", "Configure daily morning report"),
            BotCommand("users", "List allowed users (admin)"),
            BotCommand("allow", "Grant user access (admin)"),
            BotCommand("revoke", "Revoke user access (admin)"),
            BotCommand("access", "Set access mode (admin)"),
            BotCommand("restart", "Restart Valentine (admin only)"),
        ])
        logger.info("Registered Telegram bot commands.")

        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        self._response_task = asyncio.create_task(self._listen_for_responses())
        logger.info("TelegramAdapter running.")

        # Notify admins that Valentine is back online
        await self._notify_admins_startup()

    async def _notify_admins_startup(self):
        """Send a message to all admin users that Valentine is back online."""
        admin_ids = set(settings.admin_user_ids)
        if settings.admin_user_id:
            admin_ids.add(settings.admin_user_id)
        if not admin_ids:
            return
        for admin_id in admin_ids:
            try:
                await self.app.bot.send_message(
                    chat_id=admin_id,
                    text="I'm back online and ready to go! ✅",
                )
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin_id}: {e}")

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
        from valentine.identity import PRODUCT_NAME, CODENAME, COMPANY_NAME, CEO_NAME
        from telegram import ReplyKeyboardMarkup
        keyboard = [
            ["/tour 🚀", "/help ❓"],
            ["/status 📊", "/conversations 💬"],
            ["/skills 🛠️", "/tools 🧰"],
            ["Clear Memory 🧹"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"Hey! I'm {PRODUCT_NAME} ({CODENAME}) — your multi-agent AI assistant, "
            f"built by {COMPANY_NAME} under the leadership of {CEO_NAME}.\n\n"
            "Send me anything — text, photos, voice, documents.\n"
            "Use the buttons below to explore, or type /help for a full list of commands.",
            reply_markup=reply_markup
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Available commands:\n\n"
            "Identity & Info:\n"
            "  /whoami — Who am I? My identity and origins\n"
            "  /capabilities — Everything I can do\n"
            "  /status — System health and uptime\n\n"
            "Agent Management:\n"
            "  /agents — List all active agents\n"
            "  /mode — Show current autonomy mode\n"
            "  /mode <supervised|full|readonly> — Change mode\n"
            "  /skills — List installed skills\n"
            "  /tools — List available MCP tools\n\n"
            "Scheduling:\n"
            "  /schedule <interval> <task> — Create a recurring task\n"
            "  /jobs — List scheduled jobs\n"
            "  /morning — Configure daily morning report\n\n"
            "Memory:\n"
            "  /memory <query> — Search my memory\n"
            "  /forget <query> — Remove a memory\n"
            "  /clear — Clear conversation history\n\n"
            "Voice:\n"
            "  /tts <message> — Get a voice reply\n\n"
            "User Management (admin):\n"
            "  /users — List allowed users\n"
            "  /allow <id> [name] — Grant access (or reply to a message)\n"
            "  /revoke <id> — Revoke access (or reply to a message)\n"
            "  /access <open|restricted> — Set access mode\n\n"
            "Admin:\n"
            "  /restart — Pull latest code and restart (admin only)\n\n"
            "Or just send me a message — I'll figure out the rest."
        )

    async def _cmd_whoami(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from valentine.identity import (
            PRODUCT_NAME, VERSION, COMPANY_NAME,
            CEO_NAME, CEO_ROLE, PERSONALITY_TAGLINE,
        )
        await update.message.reply_text(
            f"I'm {PRODUCT_NAME} v{VERSION}\n\n"
            f"Built by {COMPANY_NAME}, led by {CEO_NAME} ({CEO_ROLE}).\n\n"
            f"{PERSONALITY_TAGLINE}"
        )

    async def _cmd_capabilities(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from valentine.identity import CAPABILITIES
        lines = [f"  {name}: {desc}" for name, desc in CAPABILITIES.items()]
        await update.message.reply_text(
            "Here's everything I can do:\n\n" + "\n".join(lines)
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show system health by querying the health endpoint."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://127.0.0.1:8080/health")
                data = resp.json()
                status = data.get("status", "unknown")
                agents = data.get("agents", {})
                agent_lines = [
                    f"  {name}: {'up' if s == 'up' else 'DOWN'}"
                    for name, s in sorted(agents.items())
                ]
                status_text = (
                    f"System: {status.upper()}\n\n"
                    "Processes:\n" + "\n".join(agent_lines)
                )
        except Exception as e:
            status_text = f"Could not fetch status: {e}"

        try:
            from valentine.core.senses import EnvironmentScanner
            scanner = EnvironmentScanner()
            env_summary = await scanner.quick_scan()
            status_text += f"\n\n{env_summary}"
        except Exception as e:
            logger.warning(f"Environment scan failed (non-fatal): {e}")

        await update.message.reply_text(status_text)

    async def _cmd_agents(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        from valentine.models import AgentName
        agent_descriptions = {
            "zeroclaw": "Router — analyses messages and routes to the right agent",
            "oracle": "Chat — general conversation, Q&A, web search",
            "codesmith": "Engineer — code, shell commands, DevOps, skills",
            "iris": "Vision — image analysis and generation",
            "echo": "Voice — transcription and text-to-speech",
            "cortex": "Memory — persistent knowledge storage",
            "nexus": "Tools — external API integrations",
            "browser": "Browser — headless web browsing and scraping",
        }
        lines = [
            f"  {name}: {agent_descriptions.get(name, '')}"
            for name in [a.value for a in AgentName]
        ]
        await update.message.reply_text(
            "Active agents:\n\n" + "\n".join(lines)
        )

    async def _cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Show or change autonomy mode."""
        from valentine.config import settings
        text = update.message.text.replace("/mode", "").strip().lower()
        if text in ("supervised", "full", "readonly"):
            settings.autonomy_mode = text
            await update.message.reply_text(f"Autonomy mode changed to: {text}")
        else:
            await update.message.reply_text(
                f"Current mode: {settings.autonomy_mode}\n\n"
                "Modes:\n"
                "  supervised — I ask before dangerous actions\n"
                "  full — I execute everything without asking\n"
                "  readonly — I can only read, not write or execute\n\n"
                "Change: /mode <supervised|full|readonly>"
            )

    async def _cmd_skills(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List installed skills."""
        from valentine.skills.manager import SkillsManager
        manager = SkillsManager(settings.skills_dir, settings.skills_builtin_dir)
        manifests = manager.discover_all()
        if not manifests:
            await update.message.reply_text("No skills installed.")
            return
        lines = [
            f"  {m.name}: {m.description or '(no description)'}"
            for m in manifests
        ]
        await update.message.reply_text(
            f"Installed skills ({len(manifests)}):\n\n" + "\n".join(lines)
        )

    async def _cmd_tools(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List available MCP tools from the shared registry."""
        from valentine.tools.registry import ToolRegistry
        registry = ToolRegistry()
        try:
            tools = await registry.list_tools()
            if not tools:
                await update.message.reply_text(
                    "No MCP tools registered. Configure MCP servers in .env."
                )
                return
            lines = [f"  {t.name}: {t.description}" for t in tools]
            await update.message.reply_text(
                f"Available tools ({len(tools)}):\n\n" + "\n".join(lines)
            )
        finally:
            await registry.close()

    async def _cmd_memory(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Search Valentine's memory."""
        query = update.message.text.replace("/memory", "").strip()
        if not query:
            await update.message.reply_text("Usage: /memory <search query>")
            return
        # Route to Cortex for memory search
        await self._route(update, ContentType.TEXT, f"search my memory for: {query}")

    async def _cmd_tour(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Start the interactive capability tour."""
        keyboard = [
            [InlineKeyboardButton("🧠 Core Brain & Memory", callback_data="tour_memory")],
            [InlineKeyboardButton("🛠 Workspace & Projects", callback_data="tour_workspace")],
            [InlineKeyboardButton("⏰ Scheduling & Automation", callback_data="tour_schedule")],
            [InlineKeyboardButton("🌐 Web & Integrations", callback_data="tour_integrations")],
            [InlineKeyboardButton("❌ Close", callback_data="tour_close")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "👋 Welcome to the *Valentine Assistant Tour*!\n\n"
            "I am an OpenClaw-grade AI. I don't just chat—I build, test, remember, and execute.\n\n"
            "Select a category below to explore my capabilities:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    async def _handle_tour_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard presses for the tour."""
        query = update.callback_query
        await query.answer()

        data = query.data
        if data == "tour_close":
            await query.edit_message_text("Tour closed! Use `/tour` to open it again at any time.", parse_mode="Markdown")
            return

        tour_content = {
            "tour_memory": (
                "🧠 *Core Brain & Memory*\n\n"
                "• *Infinite Context:* I remember past projects and facts across our chats.\n"
                "• *Persistent Sessions:* Use `/new [Project]` to start a clean conversational workspace.\n"
                "• *Context Switching:* Use `/conversations` and `/resume <id>` to seamlessly jump between older threads.\n"
                "• *Forgetting:* Use `/forget <topic>` or `/clear` to erase data."
            ),
            "tour_workspace": (
                "🛠 *Workspace & Projects (CodeSmith)*\n\n"
                "• *System Execution:* I can read/write files and execute bash commands on your host VM.\n"
                "• *Docker Isolation:* Need to test risky code? I will spin up an isolated Docker Sandbox (`run_sandbox`) to execute it defensively.\n"
                "• *Web App Previews:* Ask me to 'build a React app and preview it'. I will deploy a Cloudflare Tunnel instantly giving you a live HTTPS link."
            ),
            "tour_schedule": (
                "⏰ *Scheduling & Automation*\n\n"
                "• *Cron Jobs:* Use `/schedule` to set recurring tasks.\n"
                "• *Autonomous Execution:* I can wake up, read the news, summarize it, and push it to your phone daily.\n"
                "• *Task Management:* Use `/jobs` to review running schedulers.\n\n"
                "_Example: 'Valentine, check if my server is alive every 30 minutes.'_"
            ),
            "tour_integrations": (
                "🌐 *Web & Connective Integrations (OpenClaw MCP)*\n\n"
                "• *Brave Search:* I can scrape the internet in real-time to augment my knowledge.\n"
                "• *GitHub:* Connected via MCP. I can manage PRs, repos, and issues natively.\n"
                "• *Google Drive/Gmail:* Connected via MCP for workspace awareness.\n"
                "• *Skills:* Use `/skills` to see everything I can do. I can autonomously research and write new markdown skills manually."
            )
        }

        # Render content and re-attach back button
        keyboard = [[InlineKeyboardButton("🔙 Back to Categories", callback_data="tour_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if data == "tour_back":
            # Re-render main menu
            main_keyboard = [
                [InlineKeyboardButton("🧠 Core Brain & Memory", callback_data="tour_memory")],
                [InlineKeyboardButton("🛠 Workspace & Projects", callback_data="tour_workspace")],
                [InlineKeyboardButton("⏰ Scheduling & Automation", callback_data="tour_schedule")],
                [InlineKeyboardButton("🌐 Web & Integrations", callback_data="tour_integrations")],
                [InlineKeyboardButton("❌ Close", callback_data="tour_close")]
            ]
            await query.edit_message_text(
                "Select a category below to explore my capabilities:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(main_keyboard)
            )
        elif data in tour_content:
            await query.edit_message_text(
                tour_content[data],
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

    async def _cmd_workbench(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Open the Project Workbench Telegram Mini App."""
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
        import os
        
        # The MiniApp must be served over HTTPS. We check an environment variable first.
        workbench_url = os.environ.get("VALENTINE_WORKBENCH_URL", "https://valentine-workbench.vercel.app")
        
        keyboard = [
            [InlineKeyboardButton(
                text="🚀 Open Workbench", 
                web_app=WebAppInfo(url=workbench_url)
            )]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "Launch the *Project Workbench* below to view live previews of your current apps:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

    async def _cmd_morning(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Configure or view the daily morning report."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Morning reports are an admin-only feature.")
            return

        from valentine.core.scheduler import Scheduler
        scheduler = Scheduler()
        try:
            chat_id = str(update.effective_chat.id)
            existing = await scheduler.get_morning_report(chat_id)

            if existing:
                topics = ", ".join(existing.topics) if existing.topics else "None set"
                sources = ", ".join(existing.sources) if existing.sources else "Auto"
                status = "✅ Enabled" if existing.enabled else "⏸ Paused"
                keyboard = [
                    [InlineKeyboardButton("🔄 Reconfigure", callback_data="mr_reconfig")],
                    [InlineKeyboardButton(
                        "⏸ Pause" if existing.enabled else "▶️ Resume",
                        callback_data="mr_toggle"
                    )],
                    [InlineKeyboardButton("🗑 Remove", callback_data="mr_delete")],
                    [InlineKeyboardButton("📰 Send Now", callback_data="mr_now")],
                ]
                await update.message.reply_text(
                    f"*Your Morning Report*\n\n"
                    f"Status: {status}\n"
                    f"⏰ Delivery: {existing.delivery_time} UTC\n"
                    f"📋 Topics: {topics}\n"
                    f"📡 Sources: {sources}\n\n"
                    f"What would you like to do?",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                # First-time setup wizard
                keyboard = [
                    [InlineKeyboardButton("🤖 AI / Machine Learning", callback_data="mr_topic_AI/ML"),
                     InlineKeyboardButton("💰 Crypto / Web3", callback_data="mr_topic_Crypto")],
                    [InlineKeyboardButton("🚀 Tech Startups", callback_data="mr_topic_Startups"),
                     InlineKeyboardButton("💻 Programming", callback_data="mr_topic_Programming")],
                    [InlineKeyboardButton("🌍 World News", callback_data="mr_topic_World News"),
                     InlineKeyboardButton("📈 Finance", callback_data="mr_topic_Finance")],
                    [InlineKeyboardButton("🔬 Science", callback_data="mr_topic_Science"),
                     InlineKeyboardButton("🎮 Gaming", callback_data="mr_topic_Gaming")],
                    [InlineKeyboardButton("✅ Done Selecting", callback_data="mr_topics_done")],
                ]
                ctx.user_data['mr_topics'] = []
                await update.message.reply_text(
                    "📰 *Morning Report Setup*\n\n"
                    "I'll send you a personalized briefing every morning! "
                    "First, select the topics you care about (tap multiple):\n\n"
                    "_💡 Suggestion: Start with 2-3 topics. I'll also proactively include "
                    "breaking news that might affect you even if it's outside your selected topics._",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        finally:
            await scheduler.close()

    async def _handle_morning_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle morning report setup wizard callbacks."""
        query = update.callback_query
        await query.answer()
        data = query.data
        chat_id = str(update.effective_chat.id)
        user = update.effective_user

        if data.startswith("mr_topic_"):
            topic = data.replace("mr_topic_", "")
            selected = ctx.user_data.get('mr_topics', [])
            if topic in selected:
                selected.remove(topic)
            else:
                selected.append(topic)
            ctx.user_data['mr_topics'] = selected

            # Re-render the keyboard with selection markers
            all_topics = [
                ("🤖 AI / Machine Learning", "AI/ML"), ("💰 Crypto / Web3", "Crypto"),
                ("🚀 Tech Startups", "Startups"), ("💻 Programming", "Programming"),
                ("🌍 World News", "World News"), ("📈 Finance", "Finance"),
                ("🔬 Science", "Science"), ("🎮 Gaming", "Gaming"),
            ]
            buttons = []
            row = []
            for label, key in all_topics:
                marker = "✓ " if key in selected else ""
                row.append(InlineKeyboardButton(f"{marker}{label}", callback_data=f"mr_topic_{key}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([InlineKeyboardButton("✅ Done Selecting", callback_data="mr_topics_done")])

            selected_str = ", ".join(selected) if selected else "None yet"
            await query.edit_message_text(
                f"📰 *Morning Report Setup*\n\n"
                f"Selected: *{selected_str}*\n\n"
                f"Tap topics to toggle them, then hit Done.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        elif data == "mr_topics_done":
            selected = ctx.user_data.get('mr_topics', [])
            if not selected:
                await query.answer("Please select at least one topic!", show_alert=True)
                return

            from valentine.core.scheduler import Scheduler, MorningReport
            import uuid as _uuid
            scheduler = Scheduler()
            try:
                report = MorningReport(
                    report_id=str(_uuid.uuid4())[:8],
                    chat_id=chat_id,
                    user_id=str(user.id),
                    user_name=user.first_name or user.username or "",
                    topics=selected,
                    sources=[],  # Auto-discover
                    delivery_time="07:00",  # Default 7 AM UTC
                )
                await scheduler.save_morning_report(report)

                topics_str = ", ".join(selected)
                await query.edit_message_text(
                    f"✅ *Morning Report Configured!*\n\n"
                    f"📋 Topics: {topics_str}\n"
                    f"⏰ Delivery: 07:00 UTC daily\n"
                    f"📡 Sources: Auto (I'll search the best sources)\n\n"
                    f"_To change the delivery time, just tell me: "
                    f"\"change my morning report to 8 AM\"_\n\n"
                    f"_💡 Pro tip: You can also tell me specific sources like "
                    f"\"add TechCrunch and Hacker News to my morning report\"_",
                    parse_mode="Markdown"
                )
            finally:
                await scheduler.close()

        elif data == "mr_toggle":
            from valentine.core.scheduler import Scheduler
            scheduler = Scheduler()
            try:
                report = await scheduler.get_morning_report(chat_id)
                if report:
                    report.enabled = not report.enabled
                    await scheduler.save_morning_report(report)
                    status = "resumed ▶️" if report.enabled else "paused ⏸"
                    await query.edit_message_text(f"Morning report {status}.")
            finally:
                await scheduler.close()

        elif data == "mr_delete":
            from valentine.core.scheduler import Scheduler
            scheduler = Scheduler()
            try:
                await scheduler.delete_morning_report(chat_id)
                await query.edit_message_text("🗑 Morning report removed. Use /morning to set up a new one.")
            finally:
                await scheduler.close()

        elif data == "mr_now":
            from valentine.core.scheduler import Scheduler
            scheduler = Scheduler()
            try:
                report = await scheduler.get_morning_report(chat_id)
                if report:
                    await scheduler._deliver_morning_report(report)
                    await query.edit_message_text("📰 Morning report is being generated! You'll receive it shortly.")
                else:
                    await query.edit_message_text("No morning report configured. Use /morning to set one up.")
            finally:
                await scheduler.close()

        elif data == "mr_reconfig":
            ctx.user_data['mr_topics'] = []
            keyboard = [
                [InlineKeyboardButton("🤖 AI / Machine Learning", callback_data="mr_topic_AI/ML"),
                 InlineKeyboardButton("💰 Crypto / Web3", callback_data="mr_topic_Crypto")],
                [InlineKeyboardButton("🚀 Tech Startups", callback_data="mr_topic_Startups"),
                 InlineKeyboardButton("💻 Programming", callback_data="mr_topic_Programming")],
                [InlineKeyboardButton("🌍 World News", callback_data="mr_topic_World News"),
                 InlineKeyboardButton("📈 Finance", callback_data="mr_topic_Finance")],
                [InlineKeyboardButton("🔬 Science", callback_data="mr_topic_Science"),
                 InlineKeyboardButton("🎮 Gaming", callback_data="mr_topic_Gaming")],
                [InlineKeyboardButton("✅ Done Selecting", callback_data="mr_topics_done")],
            ]
            await query.edit_message_text(
                "📰 *Reconfigure Morning Report*\n\n"
                "Select your new topics:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    async def _cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Delete a memory."""
        query = update.message.text.replace("/forget", "").strip()
        if not query:
            await update.message.reply_text("Usage: /forget <what to forget>")
            return
        await self._route(update, ContentType.TEXT, f"forget this memory: {query}")

    async def _cmd_new(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Create a new conversation session."""
        chat_id = str(update.effective_chat.id)
        name = update.message.text.replace("/new", "").strip() or "New Project"
        session_id = await self.bus.create_session(chat_id, name)
        await update.message.reply_text(f"Started new session: {name} (ID: {session_id})\nHistory context initialized. What are we building today?")

    async def _cmd_conversations(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List active sessions."""
        from datetime import datetime
        chat_id = str(update.effective_chat.id)
        active = await self.bus.get_active_session(chat_id)
        sessions = await self.bus.list_sessions(chat_id)
        if not sessions:
            await update.message.reply_text("No sessions found. Use /new <name> to create one.")
            return

        lines = ["*Your Sessions:*"]
        for s in sessions:
            marker = "🟢" if s['id'] == active else "⚪"
            dt = datetime.fromtimestamp(s['created']).strftime('%b %d %H:%M')
            lines.append(f"{marker} `{s['id']}` - {s['name']} _({dt})_")
        lines.append("\nUse `/resume <id>` to switch contexts.")
        await update.message.reply_markdown("\n".join(lines))

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Switch to a specific session."""
        chat_id = str(update.effective_chat.id)
        session_id = update.message.text.replace("/resume", "").strip()
        if not session_id:
            await update.message.reply_text("Usage: `/resume <id>`", parse_mode="Markdown")
            return

        success = await self.bus.switch_session(chat_id, session_id)
        if success:
            await update.message.reply_text(f"Switched context to session `{session_id}`.\nValentine now remembers the history for this thread.", parse_mode="Markdown")
        else:
            await update.message.reply_text("Session ID not found.", parse_mode="Markdown")

    async def _cmd_clear(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Clear conversation history for this chat."""
        chat_id = str(update.effective_chat.id)
        try:
            await self.bus.clear_history(chat_id)
            await update.message.reply_text(
                "Conversation history cleared. I still remember things "
                "from long-term memory — use /forget to remove those."
            )
        except Exception as e:
            await update.message.reply_text(f"Failed to clear history: {e}")

    def _is_admin(self, user_id: int | str) -> bool:
        """Check if a user is an admin. If no admins configured, allow all."""
        admin_ids = set(settings.admin_user_ids)
        if settings.admin_user_id:
            admin_ids.add(settings.admin_user_id)
        if not admin_ids:
            return True  # no restriction if not configured
        return str(user_id) in admin_ids

    async def _cmd_restart(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Restart Valentine (admin only). Pulls latest code, then restarts the service."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Sorry, only admins can restart Valentine.")
            return

        import subprocess

        await update.message.reply_text("Pulling latest code and restarting... 🔄")

        # Fix git directory permissions if needed, then pull
        try:
            # Ensure the ubuntu user owns the repo (fixes read-only .git errors)
            subprocess.run(
                ["sudo", "chown", "-R", "ubuntu:ubuntu", "/opt/valentine"],
                capture_output=True, timeout=10,
            )
            pull_result = subprocess.run(
                ["git", "-C", "/opt/valentine", "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30,
            )
            if pull_result.returncode == 0:
                pull_msg = pull_result.stdout.strip() or "Already up to date."
            else:
                pull_msg = f"Git pull failed: {pull_result.stderr.strip()}"
            await update.message.reply_text(f"Git: {pull_msg}")
        except Exception as e:
            await update.message.reply_text(f"Git pull error: {e}")

        # Reinstall package to pick up code changes
        try:
            subprocess.run(
                ["/opt/valentine/venv/bin/pip", "install", "-e", "/opt/valentine"],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            pass  # non-fatal, restart anyway

        await update.message.reply_text("Restarting service... I'll be back shortly! ✨")
        await asyncio.sleep(1)

        # Restart via systemd — this kills our own process, systemd respawns everything
        try:
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "valentine"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            # Fallback: kill our own process tree so systemd restart-on-failure kicks in
            import os, signal
            os.kill(1, signal.SIGTERM)  # last resort

    # ------------------------------------------------------------------
    # User management commands (admin only)
    # ------------------------------------------------------------------

    async def _cmd_users(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """List allowed users."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can manage users.")
            return
        if not self._access:
            await update.message.reply_text("Access control not initialized.")
            return

        mode = await self._access.get_mode()
        users = await self._access.list_users()

        if mode == "open":
            header = "Access mode: OPEN (anyone can use the bot)\n"
        else:
            header = "Access mode: RESTRICTED (allowlist only)\n"

        if not users:
            await update.message.reply_text(
                header + "\nNo users in allowlist. Use /allow <user_id> to add."
            )
            return

        lines = [f"  {u['name']} (ID: {u['user_id']})" for u in users]
        await update.message.reply_text(
            header + f"\nAllowed users ({len(users)}):\n" + "\n".join(lines)
        )

    async def _cmd_allow(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Grant a user access. Usage: /allow <user_id> [name]
        Or reply to a user's message with /allow to grant them access."""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can manage users.")
            return
        if not self._access:
            await update.message.reply_text("Access control not initialized.")
            return

        args = update.message.text.replace("/allow", "").strip()

        # If replying to someone's message, use their ID
        if update.message.reply_to_message and not args:
            target = update.message.reply_to_message.from_user
            user_id = str(target.id)
            user_name = target.first_name or target.username or "Unknown"
        elif args:
            parts = args.split(maxsplit=1)
            user_id = parts[0]
            user_name = parts[1] if len(parts) > 1 else "Unknown"
        else:
            await update.message.reply_text(
                "Usage: /allow <user_id> [name]\n"
                "Or reply to a user's message with /allow"
            )
            return

        added = await self._access.allow_user(user_id, user_name)
        if added:
            await update.message.reply_text(f"Granted access to {user_name} (ID: {user_id})")
        else:
            await update.message.reply_text(f"{user_name} (ID: {user_id}) already has access.")

    async def _cmd_revoke(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Revoke a user's access. Usage: /revoke <user_id>"""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can manage users.")
            return
        if not self._access:
            await update.message.reply_text("Access control not initialized.")
            return

        args = update.message.text.replace("/revoke", "").strip()

        if update.message.reply_to_message and not args:
            target = update.message.reply_to_message.from_user
            user_id = str(target.id)
        elif args:
            user_id = args.split()[0]
        else:
            await update.message.reply_text(
                "Usage: /revoke <user_id>\n"
                "Or reply to a user's message with /revoke"
            )
            return

        removed = await self._access.revoke_user(user_id)
        if removed:
            await update.message.reply_text(f"Revoked access for user {user_id}.")
        else:
            await update.message.reply_text(f"User {user_id} wasn't in the allowlist.")

    async def _cmd_access(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Set access mode. Usage: /access <open|restricted>"""
        if not self._is_admin(update.effective_user.id):
            await update.message.reply_text("Only admins can manage access.")
            return
        if not self._access:
            await update.message.reply_text("Access control not initialized.")
            return

        mode = update.message.text.replace("/access", "").strip().lower()
        if mode in ("open", "restricted"):
            await self._access.set_mode(mode)
            if mode == "open":
                await update.message.reply_text(
                    "Access mode: OPEN — anyone can use the bot."
                )
            else:
                await update.message.reply_text(
                    "Access mode: RESTRICTED — only allowed users can use the bot.\n"
                    "Use /allow <user_id> to grant access."
                )
        else:
            current = await self._access.get_mode()
            await update.message.reply_text(
                f"Current access mode: {current.upper()}\n\n"
                "Usage: /access <open|restricted>\n"
                "  open — anyone can use the bot\n"
                "  restricted — only allowed users (use /allow to add)"
            )

    async def _cmd_tts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Send a text message as a voice reply. Usage: /tts <message>"""
        text = update.message.text.replace("/tts", "").strip()
        if not text:
            await update.message.reply_text(
                "Usage: /tts <message>\nI'll reply with a voice message!"
            )
            return

        # Route directly to Echo (skip ZeroClaw) — Echo's TTS path handles text-only
        user = update.effective_user
        user_name = user.first_name or user.username or None
        msg = IncomingMessage(
            message_id=str(update.message.message_id),
            user_id=str(user.id),
            chat_id=str(update.effective_chat.id),
            platform=MessageSource.TELEGRAM,
            content_type=ContentType.TEXT,
            text=text,
            user_name=user_name,
            timestamp=update.message.date,
        )
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent=AgentName.ECHO,
            routing=RoutingDecision(intent="tts", agent=AgentName.ECHO),
            message=msg,
        )
        await self.bus.add_task(
            self.bus.stream_name("echo", "task"), task.to_dict(),
        )

    async def _cmd_schedule(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /schedule command to create scheduled tasks."""
        text = update.message.text.replace("/schedule", "").strip()
        if not text:
            await update.message.reply_text(
                "Usage: /schedule <interval> <task>\n"
                "Examples:\n"
                "  /schedule every 1h check server health\n"
                "  /schedule daily summarize AI news\n"
                "  /schedule every 10m monitor website status"
            )
            return
        # Route through ZeroClaw with schedule intent
        await self._route(update, ContentType.TEXT, f"/schedule {text}")

    async def _cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle /jobs command to list scheduled jobs."""
        from valentine.core.scheduler import Scheduler
        scheduler = Scheduler()
        try:
            jobs_text = await scheduler.format_jobs_list(
                str(update.effective_chat.id)
            )
            await update.message.reply_text(jobs_text)
        finally:
            await scheduler.close()

    async def _route(
        self,
        update: Update,
        content_type: ContentType,
        text: str,
        media_path: str | None = None,
    ):
        # --- Stale message guard: skip messages older than 30s ---
        if update.message and update.message.date:
            import datetime
            msg_age = (datetime.datetime.now(datetime.timezone.utc) - update.message.date).total_seconds()
            if msg_age > 30:
                logger.info(f"Skipping stale message (age={msg_age:.0f}s): {update.update_id}")
                return

        # --- Dedup: prevent processing the same Telegram update twice (e.g. after restart) ---
        update_id = str(update.update_id)
        dedup_key = f"tg:dedup:{update_id}"
        already_seen = await self.bus.redis.set(dedup_key, "1", nx=True, ex=self._DEDUP_TTL)
        if not already_seen:
            # The key already existed — we've processed this update before
            logger.info(f"Dedup: skipping already-processed update {update_id}")
            return

        # --- Start typing loop AFTER dedup+stale checks pass ---
        self.start_typing_loop(str(update.effective_chat.id))

        # --- Access control gate ---
        user_id = str(update.effective_user.id)
        if self._access:
            is_admin = self._is_admin(update.effective_user.id)
            if not await self._access.is_allowed(user_id, is_admin=is_admin):
                user_name = update.effective_user.first_name or "there"
                await update.message.reply_text(
                    f"Hey {user_name}! I'm Valentine, a multi-agent AI assistant "
                    f"built by WDC Solutions.\n\n"
                    f"You don't have access yet — ask the admin to grant you "
                    f"access. Your user ID is {user_id}."
                )
                return

        # --- Input sanitisation ---
        text = sanitise_input(text) if text else text

        # Log (but don't block) injection attempts — the agent prompts are
        # hardened to resist them, so we let them through with a flag.
        injection_flagged = False
        if text and detect_injection(text):
            logger.warning(
                "Prompt injection attempt from user %s in chat %s",
                update.effective_user.id,
                update.effective_chat.id,
            )
            injection_flagged = True

        # Capture the user's display name for personalized responses
        user = update.effective_user
        user_name = user.first_name or user.username or None

        # Capture reply context — if the user is replying to a message,
        # include the original text so the agent understands the thread.
        reply_to_text = None
        if update.message.reply_to_message:
            reply_msg = update.message.reply_to_message
            reply_to_text = reply_msg.text or reply_msg.caption or None

        msg = IncomingMessage(
            message_id=str(update.message.message_id),
            user_id=str(user.id),
            chat_id=str(update.effective_chat.id),
            platform=MessageSource.TELEGRAM,
            content_type=content_type,
            text=text,
            media_path=media_path,
            user_name=user_name,
            reply_to_text=reply_to_text,
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

    # Reminder patterns:
    #   "remind me in 5m to buy milk"
    #   "remind me to buy milk in 5 minutes"
    _REMINDER_RE_A = re.compile(
        r"remind\s+me\s+in\s+(\d+\s*(?:s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?))\s+(?:to\s+)?(.+)",
        re.IGNORECASE,
    )
    _REMINDER_RE_B = re.compile(
        r"remind\s+me\s+(?:to\s+)?(.+?)\s+in\s+(\d+\s*(?:s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?))\s*$",
        re.IGNORECASE,
    )

    async def _try_create_reminder(self, update: Update, text: str) -> bool:
        """Check if the message is a reminder request. Returns True if handled."""
        # Try pattern A: "remind me in 5m to buy milk"
        match = self._REMINDER_RE_A.search(text)
        if match:
            duration_str = match.group(1).strip()
            reminder_msg = match.group(2).strip()
        else:
            # Try pattern B: "remind me to buy milk in 5 minutes"
            match = self._REMINDER_RE_B.search(text)
            if match:
                reminder_msg = match.group(1).strip()
                duration_str = match.group(2).strip()
            else:
                return False  # Not a reminder — let the LLM handle it

        delay = parse_duration(duration_str)
        if not delay:
            return False

        scheduler = Scheduler()
        try:
            user = update.effective_user
            user_name = user.first_name or user.username or ""
            await scheduler.create_reminder(
                chat_id=str(update.effective_chat.id),
                user_id=str(user.id),
                user_name=user_name,
                message=reminder_msg,
                delay_seconds=delay,
            )
            # Format friendly time
            if delay < 60:
                time_str = f"{delay} seconds"
            elif delay < 3600:
                time_str = f"{delay // 60} minute{'s' if delay >= 120 else ''}"
            else:
                time_str = f"{delay // 3600} hour{'s' if delay >= 7200 else ''}"
            await update.message.reply_text(
                f"Got it! I'll remind you in {time_str}: {reminder_msg}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to create reminder: {e}")
            return False
        finally:
            await scheduler.close()

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if not text:
            return
        # Check for reminder requests first (handled directly, no LLM needed)
        if await self._try_create_reminder(update, text):
            return
        await self._route(update, ContentType.TEXT, text)

    async def _on_photo(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        path = await self.download_media(update.message.photo[-1])
        caption = update.message.caption or ""
        await self._route(update, ContentType.PHOTO, caption, media_path=path)

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        voice = update.message.voice or update.message.audio
        path = await self.download_media(voice)
        caption = update.message.caption or ""
        await self._route(update, ContentType.VOICE, caption, media_path=path)

    async def _on_document(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        doc = update.message.document
        path = await self.download_media(doc)
        caption = update.message.caption or ""
        await self._route(update, ContentType.DOCUMENT, caption, media_path=path)

    async def _on_video(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        video = update.message.video
        path = await self.download_media(video)
        caption = update.message.caption or ""
        await self._route(update, ContentType.VIDEO, caption, media_path=path)

    async def _on_web_app_data(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Handle data sent back from the Telegram Mini App via sendData().

        The Mini App sends a JSON string containing an action and optional payload.
        We parse it and route it through ZeroClaw as a text message so agents
        get full context from the Mini App interaction.
        """
        data_str = update.effective_message.web_app_data.data
        logger.info(f"Received web_app_data from Mini App: {data_str[:200]}")

        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            payload = {"raw": data_str}

        # Construct a human-readable text for ZeroClaw routing
        action = payload.get("action", "unknown")
        detail = payload.get("detail", payload.get("text", json.dumps(payload)))
        text = f"[MiniApp action={action}] {detail}"

        await self._route(update, ContentType.TEXT, text)

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

        # Stop typing loop once we have a result for this chat
        self.stop_typing_loop(result.chat_id)

        await self._rate_limit(result.chat_id)

        try:
            if not result.success:
                # Show user-friendly error — log the raw one for debugging
                error_msg = result.error or "Unknown error"
                logger.error(f"Task {result.task_id} failed: {error_msg}")

                # Always replace internal/technical errors with friendly message
                _technical_patterns = (
                    "http", "Traceback", "Client error", "Server error",
                    "429", "500", "502", "503", "504", "redacted",
                    "Too Many Requests", "rate limit", "timed out",
                    "Connection", "ECONNREFUSED", "Internal error",
                )
                if any(p.lower() in error_msg.lower() for p in _technical_patterns):
                    user_error = (
                        "Oops, I ran into a temporary issue. "
                        "Try again in a moment! 🔄"
                    )
                else:
                    user_error = error_msg

                # Deduplicate: don't spam the same error to the same chat
                now = time.monotonic()
                error_key = user_error[:50]  # group similar errors
                last = self._last_error.get(result.chat_id)
                if last and last[0] == error_key and (now - last[1]) < self._ERROR_DEDUP_WINDOW:
                    logger.info(f"Suppressing duplicate error for chat {result.chat_id}")
                    return
                self._last_error[result.chat_id] = (error_key, now)

                await self._send_with_retry(
                    self.app.bot.send_message,
                    chat_id=result.chat_id,
                    text=user_error,
                )
                return

            if result.content_type == ContentType.PHOTO and result.media_path:
                # Handle both local file paths and URLs
                photo = result.media_path
                if os.path.isfile(result.media_path):
                    photo = open(result.media_path, "rb")
                await self._send_with_retry(
                    self.app.bot.send_photo,
                    chat_id=result.chat_id,
                    photo=photo,
                    caption=(result.text or "")[:1024],
                )
            elif result.content_type == ContentType.VOICE and result.media_path:
                await self._send_with_retry(
                    self.app.bot.send_voice,
                    chat_id=result.chat_id,
                    voice=open(result.media_path, "rb"),
                    caption=(result.text or "")[:1024],
                )
            elif result.content_type == ContentType.DOCUMENT and result.media_path:
                # Use file_name from result if available for better UX
                filename = result.file_name or os.path.basename(result.media_path)
                await self._send_with_retry(
                    self.app.bot.send_document,
                    chat_id=result.chat_id,
                    document=open(result.media_path, "rb"),
                    filename=filename,
                    caption=(result.text or "")[:1024],
                )
            else:
                # TEXT or fallback — skip truly empty responses (e.g. Echo re-route)
                text = (result.text or "").strip()
                if not text:
                    return

                # Build optional Mini App inline button if agent attached one
                reply_markup = self._build_miniapp_markup(result.miniapp)

                # Telegram has a 4096-char limit per message
                chunks = list(_chunk_text(text, 4096))
                for i, chunk in enumerate(chunks):
                    # Attach the miniapp button only to the last chunk
                    kwargs: dict = {"chat_id": result.chat_id, "text": chunk}
                    if reply_markup and i == len(chunks) - 1:
                        kwargs["reply_markup"] = reply_markup
                    await self._send_with_retry(
                        self.app.bot.send_message, **kwargs
                    )
        except Exception as e:
            logger.error(f"Failed to send result {result.task_id} to Telegram: {e}")

    def _build_miniapp_markup(self, miniapp: dict | None):
        """Build an InlineKeyboardMarkup with a WebAppInfo button if miniapp data is present.

        Expected miniapp dict format:
            {"route": "/dashboard", "label": "Open Dashboard"}
        Returns None if miniapp is None or invalid.
        """
        if not miniapp or not isinstance(miniapp, dict):
            return None
        route = miniapp.get("route", "/")
        label = miniapp.get("label", "🚀 Open App")
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
            import os
            base_url = os.environ.get(
                "VALENTINE_WORKBENCH_URL", "https://valentine-workbench.vercel.app"
            )
            # Append route to base URL (strip trailing slash from base, leading from route)
            url = base_url.rstrip("/") + "/" + route.lstrip("/")
            return InlineKeyboardMarkup([
                [InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))]
            ])
        except Exception as e:
            logger.warning(f"Failed to build miniapp markup: {e}")
            return None

    # ------------------------------------------------------------------
    # Media download
    # ------------------------------------------------------------------

    async def download_media(self, file_ref: Any) -> str:
        tg_file = await file_ref.get_file()
        ext = _guess_extension(tg_file.file_path or "")
        # Validate file extension
        if ext and not validate_media_extension(f"file{ext}"):
            logger.warning("Rejected media with disallowed extension: %s", ext)
            raise ValueError(f"File type '{ext}' is not supported.")
        # Use workspace /tmp dir (system /tmp may be read-only on some VMs)
        media_dir = os.path.join(settings.workspace_dir, ".media")
        os.makedirs(media_dir, exist_ok=True)
        local_path = os.path.join(media_dir, f"{tg_file.file_id}{ext}")
        await tg_file.download_to_drive(custom_path=local_path)
        logger.info(f"Downloaded media → {local_path}")
        return local_path

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _typing_loop(self, chat_id: str) -> None:
        """Continuously refresh the typing indicator every 4 seconds (max 5 minutes)."""
        import time
        timeout_at = time.time() + 300
        try:
            while time.time() < timeout_at:
                try:
                    await self.app.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass  # best-effort
                await asyncio.sleep(4.0)
        except asyncio.CancelledError:
            pass  # Task cancelled when result arrives

    def start_typing_loop(self, chat_id: str) -> None:
        """Start a background task to keep the typing indicator active."""
        self.stop_typing_loop(chat_id)
        self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def stop_typing_loop(self, chat_id: str) -> None:
        """Cancel the typing loop for a chat."""
        task = self._typing_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

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
