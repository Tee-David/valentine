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
from valentine.access import AccessControl
from valentine.security import (
    sanitise_input, detect_injection, validate_media_extension,
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
        # Memory & history
        self.app.add_handler(CommandHandler("memory", self._cmd_memory))
        self.app.add_handler(CommandHandler("forget", self._cmd_forget))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        # Voice
        self.app.add_handler(CommandHandler("tts", self._cmd_tts))
        # User management (admin only)
        self.app.add_handler(CommandHandler("users", self._cmd_users))
        self.app.add_handler(CommandHandler("allow", self._cmd_allow))
        self.app.add_handler(CommandHandler("revoke", self._cmd_revoke))
        self.app.add_handler(CommandHandler("access", self._cmd_access))
        # Admin
        self.app.add_handler(CommandHandler("restart", self._cmd_restart))
        # Message handlers (must be last)
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

        # Clear any stale webhook/polling session to prevent
        # "terminated by other getUpdates request" conflicts
        try:
            await self.app.bot.delete_webhook(drop_pending_updates=True)
            logger.info("Cleared stale webhook/updates before starting polling.")
        except Exception as e:
            logger.warning(f"delete_webhook on startup failed (non-fatal): {e}")

        await self.app.start()

        # Register slash commands with Telegram so they appear in the "/" menu
        from telegram import BotCommand
        await self.app.bot.set_my_commands([
            BotCommand("start", "Start Valentine"),
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
            BotCommand("memory", "Search my memory"),
            BotCommand("forget", "Remove a memory"),
            BotCommand("clear", "Clear conversation history"),
            BotCommand("tts", "Get a voice reply"),
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
        await update.message.reply_text(
            f"Hey! I'm {PRODUCT_NAME} ({CODENAME}) — your multi-agent AI assistant, "
            f"built by {COMPANY_NAME} under the leadership of {CEO_NAME}.\n\n"
            "Send me anything — text, photos, voice, documents.\n"
            "Type /help to see all available commands."
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
            "  /jobs — List scheduled jobs\n\n"
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

    async def _cmd_forget(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Delete a memory."""
        query = update.message.text.replace("/forget", "").strip()
        if not query:
            await update.message.reply_text("Usage: /forget <what to forget>")
            return
        await self._route(update, ContentType.TEXT, f"forget this memory: {query}")

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
                ["sudo", "chown", "-R", "ubuntu:ubuntu", "/opt/valentine/.git"],
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
        # --- Dedup: prevent processing the same Telegram update twice (e.g. after restart) ---
        update_id = str(update.update_id)
        dedup_key = f"tg:dedup:{update_id}"
        already_seen = await self.bus.redis.set(dedup_key, "1", nx=True, ex=self._DEDUP_TTL)
        if not already_seen:
            # The key already existed — we've processed this update before
            logger.info(f"Dedup: skipping already-processed update {update_id}")
            return

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
        caption = update.message.caption or ""
        await self._route(update, ContentType.VOICE, caption, media_path=path)

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
