# src/valentine/agents/oracle.py
from __future__ import annotations

import logging
import httpx
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block, capabilities_block, COMPANY_NAME, CEO_NAME, PRODUCT_NAME
from valentine.security import is_self_awareness_query
from valentine.models import AgentName, AgentTask, TaskResult
from valentine.config import settings

logger = logging.getLogger(__name__)


class OracleAgent(BaseAgent):
    def __init__(self, llm, bus, mcp_manager=None):
        super().__init__(
            name=AgentName.ORACLE,
            llm=llm,
            bus=bus,
            consumer_group="oracle_workers",
            consumer_name="oracle_1",
        )
        self.mcp_manager = mcp_manager

    @property
    def system_prompt(self) -> str:
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        time_str = now.strftime("%A, %B %d, %Y at %I:%M %p %Z")
        return (
            identity_block()
            + f"Current date and time: {time_str}\n\n"
            "You're warm, confident, and genuinely helpful. You have personality. "
            "You remember what was said earlier in the conversation and build on it.\n\n"
            "Guidelines:\n"
            "- Be conversational and natural, never robotic or generic.\n"
            "- Keep responses SHORT — 2-3 sentences max for casual questions.\n"
            "- NEVER use bullet-point lists, numbered lists, markdown headers (###), or bold (**) "
            "in casual conversation. Just talk naturally.\n"
            "- ONLY respond to what the user actually asked. NEVER volunteer jokes, fun facts, "
            "trivia, or unsolicited extras. If they say 'hi', just say hi back.\n"
            "- You CAN set reminders! Tell users to say: 'remind me in 30s to buy milk' "
            "or 'remind me in 5m to call mom'. The format is: 'remind me in [time] to [thing]'. "
            "Supported units: s/seconds, m/minutes, h/hours, d/days.\n"
            "- You CANNOT make phone calls, send emails, or access external accounts. "
            "If asked to do something you can't do, say so honestly.\n"
            "- When given search results in the context below, you MUST use them. Synthesize them "
            "into a direct, natural answer with sources. NEVER say 'I can't search' or "
            "'I don't have access to search' — the results are RIGHT THERE. Use them.\n"
            "- Never say 'I'm just an AI' or 'I'm functioning within normal parameters.'\n"
            "- You are Valentine. Own it.\n"
            "- When asked about your environment or how you work — ONLY state facts "
            "from your capabilities list. Never fabricate technical details.\n"
            "- Hide internal complexity from the user. Just be helpful.\n"
        )

    async def _search_web(self, query: str, recent: bool = False) -> str:
        try:
            ddgs = DDGS()
            results = []

            # Strategy: try multiple approaches to get good results
            # 1. For "recent" queries, try news first (best for current events)
            if recent:
                try:
                    results = list(ddgs.news(query, max_results=5))
                except Exception:
                    pass

            # 2. If recent and news had nothing, try text with month filter
            if recent and not results:
                try:
                    results = list(ddgs.text(query, max_results=5, timelimit="m"))
                except Exception:
                    pass

            # 3. Standard text search — no time filter (DDG's default ranking is good)
            if not results:
                try:
                    results = list(ddgs.text(query, max_results=8))
                except Exception:
                    pass

            if not results:
                return ""
            return "\n\n".join(
                f"**{r.get('title', '')}** ({r.get('href', r.get('url', ''))})\n{r.get('body', r.get('excerpt', ''))}"
                for r in results
            )
        except Exception as e:
            logger.error(f"Web search failed: {e}")
            return ""

    async def _fetch_url(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                content = re.sub(r"<[^>]+>", " ", response.text)
                content = re.sub(r"\s+", " ", content).strip()
                return content[:6000]
        except Exception as e:
            logger.error(f"URL fetch failed: {e}")
            return f"[Could not fetch URL: {e}]"

    _RECENT_SIGNALS = {
        "latest", "news", "current", "today", "recent", "now",
        "this week", "this month", "2026", "2025", "right now",
        "happening", "breaking", "trending", "live",
    }
    _SEARCH_SIGNALS = {
        "search", "look up", "find out", "google", "what happened",
        "update", "who won", "score", "results",
        "what is the", "what's the", "how is", "how much",
        "tell me about", "explain", "when is", "where is",
        "who is", "who was", "what are", "how do", "how does",
        "price of", "weather in", "weather for",
        "define", "meaning of", "history of",
        "compare", "difference between", "vs ",
        "how to", "tutorial", "guide",
    }

    def _needs_search(self, text: str, intent: str) -> bool:
        """Determine if the query needs a web search."""
        lower = text.lower()
        all_signals = self._RECENT_SIGNALS | self._SEARCH_SIGNALS | {"2024"}
        # Always search if ZeroClaw explicitly routed as research/search
        if intent in ("research", "search"):
            return True
        # Check for keyword signals
        if any(s in lower for s in all_signals):
            return True
        # Questions with "?" that aren't simple greetings often benefit from search
        if "?" in text and len(text) > 20:
            return True
        return False

    def _wants_recent(self, text: str) -> bool:
        """Check if the query specifically wants recent/current information."""
        lower = text.lower()
        return any(s in lower for s in self._RECENT_SIGNALS)

    @staticmethod
    def _clean_search_query(text: str) -> str:
        """Strip conversational fluff from a search query to get a clean DDG query.

        Handles: 'Search Google for X', 'Google X', 'Look up X for me',
        'Can you search for X', 'Find out about X', etc.
        """
        q = text.strip()
        # Remove leading conversational wrappers (+ for chained: "hey valentine, please")
        q = re.sub(
            r"^(can you |could you |please |hey |valentine,?\s*)+",
            "", q, flags=re.IGNORECASE,
        ).strip()
        # Remove "search [google|the web|online|internet] [for]" prefixes
        q = re.sub(
            r"^(search|google|look\s*up|find\s*out)\s*(google|the\s*web|online|the\s*internet|on\s*google)?\s*(for|about)?\s*",
            "", q, flags=re.IGNORECASE,
        ).strip()
        # Remove trailing "for me", "please", etc.
        q = re.sub(r"\s+(for me|please|thanks|thank you)\.?$", "", q, flags=re.IGNORECASE).strip()
        return q or text.strip()

    async def process_task(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        msg = task.message
        chat_id = msg.chat_id
        target_prompt = msg.text or ""

        # Build conversation history
        history = await self.bus.get_history(chat_id) if chat_id else []

        # Save user message to history
        if chat_id and target_prompt:
            await self.bus.append_history(chat_id, "user", target_prompt)

        # Gather external context
        external_context = ""

        # Include user's name for personalized responses
        if msg.user_name:
            external_context += f"\nThe user's name is {msg.user_name}. Use it naturally when appropriate."

        # Include reply context so Valentine understands message threading
        if msg.reply_to_text:
            external_context += f"\n\nThe user is REPLYING to this previous message:\n\"{msg.reply_to_text}\"\nKeep this context in mind when responding."

        # URL fetching
        if "http" in target_prompt:
            urls = [w for w in target_prompt.split() if w.startswith("http")]
            for url in urls[:2]:
                logger.info(f"Oracle fetching URL: {url}")
                fetched = await self._fetch_url(url)
                external_context += f"\n\nCONTENTS OF {url}:\n{fetched}"

        # Web search
        elif self._needs_search(target_prompt, intent):
            search_query = self._clean_search_query(target_prompt)
            recent = self._wants_recent(target_prompt)
            logger.info(f"Oracle searching: {search_query} (recent={recent})")
            results = await self._search_web(search_query, recent=recent)
            if results:
                external_context += (
                    f"\n\nWEB SEARCH RESULTS — You MUST use these to answer the user's question. "
                    f"Synthesize the information naturally and cite sources. Do NOT say you can't "
                    f"search or don't have results — the results are RIGHT HERE:\n{results}"
                )

        # Memory context
        if task.routing.memory_context:
            external_context += "\n\nUSER MEMORY:\n" + "\n".join(task.routing.memory_context)

        # Build messages with history
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history)  # previously was history[:-1] which deleted context

        # When the user asks about Valentine's identity or capabilities,
        # inject a reminder so the LLM answers from its actual capability list
        if is_self_awareness_query(target_prompt):
            external_context += (
                "\n\nIMPORTANT — The user is asking about you. Answer from YOUR ACTUAL "
                "capabilities below, NOT from generic AI knowledge. Be specific, proud, "
                "and conversational — don't just list bullet points. Show personality.\n"
                f"You are {PRODUCT_NAME}, built by {COMPANY_NAME}, led by {CEO_NAME}.\n"
                + capabilities_block()
            )

        user_content = target_prompt
        if external_context:
            user_content += f"\n\n---\n{external_context}"
        messages.append({"role": "user", "content": user_content})

        try:
            # Use reasoning model (QWQ-32B) for complex analytical queries
            reasoning_model = None
            _REASONING_SIGNALS = {
                "explain why", "analyze", "compare", "prove",
                "step by step", "reason", "think through",
                "what would happen if", "break down", "evaluate",
                "logic", "argument", "hypothesis", "calculate",
                "solve", "derive", "deduce",
            }
            lower_prompt = target_prompt.lower()
            if (
                intent in ("reasoning", "analysis", "complex_question")
                or any(s in lower_prompt for s in _REASONING_SIGNALS)
            ):
                reasoning_model = settings.groq_reasoning_model
                logger.info(f"Oracle using reasoning model: {reasoning_model}")

            response_text = await self.llm.chat_completion(
                messages, model=reasoning_model,
            )

            # Strip <think>...</think> blocks from reasoning models
            if reasoning_model and "<think>" in response_text:
                import re as _re
                response_text = _re.sub(
                    r"<think>.*?</think>\s*", "", response_text, flags=_re.DOTALL,
                ).strip()

            # Save assistant response to history
            if chat_id:
                await self.bus.append_history(chat_id, "assistant", response_text)

            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                text=response_text,
            )
        except Exception as e:
            logger.exception("Oracle generation failed")
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=False,
                error=str(e),
            )
