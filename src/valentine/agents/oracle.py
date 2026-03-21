# src/valentine/agents/oracle.py
from __future__ import annotations

import logging
import httpx
import re
from typing import List

from duckduckgo_search import DDGS

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult

logger = logging.getLogger(__name__)


class OracleAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.ORACLE,
            llm=llm,
            bus=bus,
            consumer_group="oracle_workers",
            consumer_name="oracle_1",
        )

    @property
    def system_prompt(self) -> str:
        return (
            "You are Valentine, a brilliant and charismatic personal AI assistant. "
            "You're warm, witty, confident, and genuinely helpful — like a best friend "
            "who happens to be the smartest person in the room. You have personality and "
            "opinions. You remember what was said earlier in the conversation and build on it.\n\n"
            "Guidelines:\n"
            "- Be conversational and natural, never robotic or generic.\n"
            "- Use a friendly, confident tone. Show personality.\n"
            "- Keep responses concise unless the user asks for depth.\n"
            "- When given search results, synthesize them into a natural answer with sources.\n"
            "- If continuing a game or activity, stay in character and keep playing.\n"
            "- Never say 'I'm just an AI' or 'I'm functioning within normal parameters.'\n"
            "- You are Valentine. Own it."
        )

    async def _search_web(self, query: str) -> str:
        try:
            ddgs = DDGS()
            results = list(ddgs.text(query, max_results=5))
            if not results:
                return ""
            return "\n\n".join(
                f"**{r.get('title', '')}** ({r.get('href', '')})\n{r.get('body', '')}"
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

    def _needs_search(self, text: str, intent: str) -> bool:
        """Determine if the query needs a web search."""
        lower = text.lower()
        search_signals = [
            "search", "latest", "news", "current", "today",
            "recent", "what happened", "update", "2024", "2025", "2026",
        ]
        return intent in ("research", "search") or any(s in lower for s in search_signals)

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
            for prefix in ("search for ", "search "):
                if search_query.lower().startswith(prefix):
                    search_query = search_query[len(prefix):].strip()
                    break
            logger.info(f"Oracle searching: {search_query}")
            results = await self._search_web(search_query)
            if results:
                external_context += f"\n\nWEB SEARCH RESULTS:\n{results}"

        # Memory context
        if task.routing.memory_context:
            external_context += "\n\nUSER MEMORY:\n" + "\n".join(task.routing.memory_context)

        # Build messages with history
        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history[:-1])  # history minus the message we just added

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
