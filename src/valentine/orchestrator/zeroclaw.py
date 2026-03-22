# src/valentine/orchestrator/zeroclaw.py
from __future__ import annotations

import json
import logging
import re
from typing import List

from valentine.agents.base import BaseAgent
from valentine.identity import internal_identity_block
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, RoutingDecision, IncomingMessage, ContentType, Priority
from valentine.utils import safe_parse_json

logger = logging.getLogger(__name__)

# Keyword patterns that ALWAYS route to CodeSmith — bypasses the LLM entirely.
# This prevents Oracle from giving tutorials for tasks CodeSmith should handle.
_CODESMITH_PATTERNS = re.compile(
    r"\b("
    r"write\s+(a\s+|some\s+|the\s+)?code"
    r"|build\s+(me\s+)?(a\s+|the\s+)?(\w+\s+)?(web\s*app|website|app|api|server|bot|tool|script|page|dashboard|project|site|program|game)"
    r"|create\s+(me\s+)?(a\s+|the\s+)?(\w+\s+)?(web\s*app|website|app|api|server|bot|tool|script|page|dashboard|project|site|program|game|python|node|flask|fastapi|django|react|environment|venv|virtualenv)"
    r"|make\s+(me\s+)?(a\s+|the\s+)?(\w+\s+)?(web\s*app|website|app|api|server|bot|tool|script|page|dashboard|project|site|program|game)"
    r"|deploy\b|dockerfile|docker\s*compose|nginx|systemd|cloudflare"
    r"|set\s*up\s+(a\s+|the\s+)?(\w+\s+)?(server|environment|project|venv|virtualenv|python|node|database|redis|docker)"
    r"|install\s+(a\s+)?(\w+\s+)?(package|module|library|dependency|pip|npm|node|python)"
    r"|run\s+(this|a|the)?\s*(command|script|code|shell|bash|python|pip|npm)"
    r"|git\s+(clone|push|pull|commit|init|status|log|branch|merge|checkout|stash|diff|remote|reset|rebase)"
    r"|github\b|gitlab\b"
    r"|debug\s+(this|my|the)"
    r"|fix\s+(this|my|the)\s*(\w+\s+)?(code|bug|error|issue|script)"
    r"|refactor\b|optimize\s+(this|my|the)\s*(\w+\s+)?(code|function|script)"
    r"|pip\s+install|npm\s+install|apt\s+install"
    r"|python3?\s+|node\s+|bash\s+|sh\s+"
    r"|curl\s+|wget\s+"
    r"|chmod\s+|chown\s+|mkdir\s+|ls\s+-"
    r"|server\s+status|cpu\s+usage|disk\s+space|memory\s+usage"
    r"|check\s+(the\s+)?(\w+\s+)?(server|system|cpu|memory|disk|ram|uptime|load)"
    r"|skill\s+(install|list|run)"
    r"|cron\s*job|systemctl|service\s+(start|stop|restart|status)"
    r")\b",
    re.IGNORECASE,
)


class ZeroClawRouter(BaseAgent):
    def __init__(self, llm, bus, tool_registry=None):
        super().__init__(
            name=AgentName.ZEROCLAW,
            llm=llm,
            bus=bus,
            consumer_group="zeroclaw_routers",
            consumer_name="zeroclaw_1",
        )
        self.task_stream = self.bus.ROUTER_STREAM
        self.tool_registry = tool_registry
        self._tool_summary = ""

        # Lightweight Mem0 reader for routing context (read-only)
        self._memory = None
        try:
            from mem0 import Memory
            mem0_config = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": settings.qdrant_host,
                        "port": settings.qdrant_port,
                        "collection_name": "valentine_memory",
                    },
                },
                "embedder": {
                    "provider": "huggingface",
                    "config": {"model": "all-MiniLM-L6-v2"},
                },
            }
            self._memory = Memory.from_config(mem0_config)
            logger.info("ZeroClaw memory reader initialized")
        except Exception as e:
            logger.warning(f"ZeroClaw memory reader unavailable (non-fatal): {e}")

    @property
    def system_prompt(self) -> str:
        tools_section = ""
        if self.tool_registry and self._tool_summary:
            tools_section = (
                "\n\nAVAILABLE TOOLS (can be used by agents):\n"
                + self._tool_summary
            )

        return (
            internal_identity_block()
            + """You are ZeroClaw, the Master Orchestrator.
Your ONLY job is to analyze each incoming user message and decide which sub-agent should handle it.

Available Agents:
1. "oracle" — ONLY for casual chat, opinions, general knowledge, web search, news, games, and conversation. Use oracle when the user is TALKING, not when they want something BUILT or DONE.
2. "codesmith" — Code, building, creating, deploying, DevOps, shell, scripts, GitHub, server management, skills, file operations. Route to codesmith when the user wants to:
   - BUILD, CREATE, MAKE, or SET UP anything technical (apps, websites, APIs, environments, projects, tools, bots, scripts, games, dashboards)
   - Write, debug, fix, refactor, or explain code
   - Run commands, scripts, or shell operations
   - Manage GitHub/git repos (clone, push, pull, PRs, issues, commits)
   - Check server status, CPU, memory, disk, uptime
   - Install packages, libraries, or dependencies (pip, npm, apt)
   - Install/list/run skills
   - Deploy, manage services, configure infrastructure
   - Create a Python/Node/virtual environment
   - Work with Docker, nginx, systemd, cron jobs
   IMPORTANT: If the user says "create", "build", "make", "set up", "deploy", or "install" followed by ANYTHING technical → route to "codesmith", NOT "oracle". Oracle gives tutorials. CodeSmith actually does the work.
3. "iris" — Vision and images ONLY. Use when:
   - The user sends a PHOTO/IMAGE (content_type is "photo")
   - The user asks to GENERATE/CREATE/MAKE an image
   - The user asks for OCR or screenshot analysis
4. "echo" — Voice/audio ONLY. Use when content_type is "voice".
5. "browser" — Web browsing, page scraping, website interaction. Use when:
   - The user wants to scrape/extract data from a website
   - The user wants to navigate a web page and interact with it
   - The user needs JavaScript-rendered content (not just raw HTML)
   - The user asks to take a screenshot of a webpage

RULES:
- If content_type is "photo" → ALWAYS route to "iris", regardless of text.
- If content_type is "voice" → ALWAYS route to "echo".
- If the user asks to generate/create/make an image → route to "iris".
- If the user says "create", "build", "make", "set up", "deploy", "install", "run", "write", "code", "debug", "fix" → route to "codesmith".
- GitHub, git, deploy, server, skills, shell, python, pip, npm, docker → route to "codesmith".
- Website scraping, page interaction, JS-rendered content → route to "browser".
- Oracle is ONLY for conversation, opinions, search, and general knowledge. NEVER route a "build/create/make" request to oracle.
- If unsure between oracle and codesmith → route to "codesmith". It can always respond conversationally if the task is just a question.

Output ONLY valid JSON:
{"intent": "short description", "agent": "oracle|codesmith|iris|echo|browser", "priority": "normal", "chain": [], "params": {"tool": "tool_name"}}

No markdown. No explanation. JSON only."""
            + tools_section
        )

    async def publish_result(self, result: TaskResult):
        # ZeroClaw results are internal — don't broadcast to the bot
        await self.bus.add_task(self.result_stream, result.to_dict())

    async def _fetch_context(self, message: IncomingMessage) -> List[str]:
        """Fetch memory context from Qdrant to enrich routing decisions."""
        if not self._memory or not message.text:
            return []
        try:
            results = self._memory.search(message.text, user_id=message.user_id, limit=3)
            context = []
            for r in results:
                mem_type = r.get("metadata", {}).get("type", "fact")
                text = r.get("text", r.get("memory", ""))
                if not text:
                    continue
                if mem_type == "procedure":
                    context.append(f"[HOW-TO] {text}")
                elif mem_type == "capability":
                    context.append(f"[INSTALLED] {text}")
                elif mem_type == "constraint":
                    context.append(f"[LIMITATION] {text}")
                else:
                    context.append(text)
            return context
        except Exception as e:
            logger.warning(f"ZeroClaw memory lookup failed (non-fatal): {e}")
            return []

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        context_items = await self._fetch_context(msg)

        # Build a rich prompt that includes content type and media info
        prompt_parts = []
        if msg.user_name:
            prompt_parts.append(f"User name: {msg.user_name}")
        if msg.text:
            prompt_parts.append(f"User message: {msg.text}")
        else:
            prompt_parts.append("User message: (no text)")

        if msg.reply_to_text:
            prompt_parts.append(f"Replying to message: {msg.reply_to_text[:200]}")

        prompt_parts.append(f"Content type: {msg.content_type.value if isinstance(msg.content_type, ContentType) else msg.content_type}")

        if msg.media_path:
            prompt_parts.append(f"Has media attachment: yes ({msg.media_path})")

        if context_items:
            prompt_parts.append(f"Memory context: {context_items}")

        prompt = "\n".join(prompt_parts)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt},
        ]

        try:
            kwargs = {}
            if self.llm.provider_name in ("groq", "cerebras"):
                kwargs["response_format"] = {"type": "json_object"}

            response_text = await self.llm.chat_completion(
                messages, temperature=0.0, max_tokens=200, **kwargs,
            )

            data = safe_parse_json(response_text)
            if data is None:
                raise ValueError(f"LLM returned unparseable response: {response_text[:200]}")

            agent_str = data.get("agent", "oracle").lower()
            try:
                target_agent = AgentName(agent_str)
            except ValueError:
                target_agent = AgentName.ORACLE

            # Hard overrides based on content type (don't trust LLM for these)
            content_type = msg.content_type
            if isinstance(content_type, str):
                content_type = ContentType(content_type)

            if content_type == ContentType.PHOTO:
                target_agent = AgentName.IRIS
            elif content_type == ContentType.VOICE:
                target_agent = AgentName.ECHO
            elif target_agent == AgentName.ORACLE and msg.text and _CODESMITH_PATTERNS.search(msg.text):
                # Keyword override: LLM said Oracle but the message clearly needs CodeSmith
                logger.info(f"ZeroClaw keyword override: Oracle → CodeSmith for: {msg.text[:80]}")
                target_agent = AgentName.CODESMITH

            # Convert LLM output strings to proper enums
            try:
                priority = Priority(data.get("priority", "normal"))
            except ValueError:
                priority = Priority.NORMAL

            routing = RoutingDecision(
                intent=data.get("intent", "chat"),
                agent=target_agent,
                priority=priority,
                chain=[AgentName(a) for a in data.get("chain", [])] if data.get("chain") else None,
                memory_context=context_items,
            )

            delegated_task = AgentTask(
                task_id=task.task_id,
                agent=target_agent,
                routing=routing,
                message=msg,
                previous_results=task.previous_results,
            )

            target_stream = self.bus.stream_name(target_agent.value, "task")
            await self.bus.add_task(target_stream, delegated_task.to_dict())

            logger.info(f"ZeroClaw routed {task.task_id} → {target_agent.value} (intent: {routing.intent})")

            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                text=f"Routed to {target_agent.value}",
            )

        except Exception as e:
            logger.error(f"ZeroClaw routing failed, falling back to Oracle: {e}")
            fallback_routing = RoutingDecision(intent="fallback", agent=AgentName.ORACLE)
            fallback_task = AgentTask(
                task_id=task.task_id,
                agent=AgentName.ORACLE,
                routing=fallback_routing,
                message=msg,
            )
            await self.bus.add_task(
                self.bus.stream_name("oracle", "task"), fallback_task.to_dict(),
            )
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                text="Fallback to Oracle",
            )
