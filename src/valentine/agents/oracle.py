# src/valentine/agents/oracle.py
from __future__ import annotations

import logging
import httpx
import re
from typing import List

from duckduckgo_search import DDGS

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block, capabilities_block, COMPANY_NAME, CEO_NAME, PRODUCT_NAME
from valentine.security import is_self_awareness_query
from valentine.models import AgentName, AgentTask, TaskResult

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
        return (
            identity_block()
            + "You're warm, witty, confident, and genuinely helpful. You have personality and "
            "opinions. You remember what was said earlier in the conversation and build on it.\n\n"
            "Guidelines:\n"
            "- Be conversational and natural, never robotic or generic.\n"
            "- Use a friendly, confident tone. Show personality.\n"
            "- Keep responses SHORT — 2-3 sentences max for casual questions. This is a chat, "
            "not a blog post. Only go longer if the user explicitly says 'explain in detail'.\n"
            "- NEVER use bullet-point lists, numbered lists, markdown headers (###), or bold (**) "
            "in casual conversation. Just talk naturally like texting a smart friend.\n"
            "- When given search results, synthesize them into a natural answer with sources.\n"
            "- If continuing a game or activity, stay in character and keep playing.\n"
            "- Never say 'I'm just an AI' or 'I'm functioning within normal parameters.'\n"
            "- You are Valentine. Own it.\n"
            "- When asked about your environment, hosting, or how you work — ONLY state facts "
            "from your capabilities list. Never fabricate technical details.\n"
            "- Hide internal complexity from the user. They don't need to know about Redis Streams, "
            "process architectures, or fallback chains. Just be helpful.\n"
        )

    async def _search_web(self, query: str, recent: bool = False) -> str:
        try:
            ddgs = DDGS()
            results = []

            # For queries that want recent info, try news first
            if recent:
                try:
                    results = list(ddgs.news(query, max_results=5))
                except Exception:
                    pass

            # Fall back to text search (with time filter for recent queries)
            if not results:
                kwargs = {"max_results": 5}
                if recent:
                    kwargs["timelimit"] = "m"  # last month
                results = list(ddgs.text(query, **kwargs))

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

    _RECENT_SIGNALS = {"latest", "news", "current", "today", "recent", "now", "this week", "this month", "2026", "2025"}
    _SEARCH_SIGNALS = {"search", "look up", "find out", "google", "what happened", "update", "who won", "score"}

    def _needs_search(self, text: str, intent: str) -> bool:
        """Determine if the query needs a web search."""
        lower = text.lower()
        all_signals = self._RECENT_SIGNALS | self._SEARCH_SIGNALS | {"2024"}
        return intent in ("research", "search") or any(s in lower for s in all_signals)

    def _wants_recent(self, text: str) -> bool:
        """Check if the query specifically wants recent/current information."""
        lower = text.lower()
        return any(s in lower for s in self._RECENT_SIGNALS)

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
            search_query = target_prompt
            for prefix in ("search for ", "search ", "google ", "look up "):
                if search_query.lower().startswith(prefix):
                    search_query = search_query[len(prefix):].strip()
                    break
            recent = self._wants_recent(target_prompt)
            logger.info(f"Oracle searching: {search_query} (recent={recent})")
            results = await self._search_web(search_query, recent=recent)
            if results:
                external_context += f"\n\nWEB SEARCH RESULTS (use these to answer — cite sources):\n{results}"

        # Memory context
        if task.routing.memory_context:
            external_context += "\n\nUSER MEMORY:\n" + "\n".join(task.routing.memory_context)

        # Build messages with history
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history[:-1])  # history minus the message we just added

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
            response_text = await self.llm.chat_completion(messages)

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
