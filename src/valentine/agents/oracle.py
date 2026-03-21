# src/valentine/agents/oracle.py
import logging
import httpx
import re
from typing import List

from duckduckgo_search import AsyncDDGS

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
            consumer_name="oracle_1"
        )
    
    @property
    def system_prompt(self) -> str:
        return """You are Oracle, the primary research and reasoning agent of Valentine v2.
You serve as a world-class research analyst. You provide detailed, highly accurate, and deeply reasoned answers.
You gracefully handle casual chatting, complex analytical queries, and summations.
You rely strictly on the external information injected into your prompt when answering factual questions about recent topics."""

    async def _search_web(self, query: str) -> str:
        try:
            results = await AsyncDDGS().text(query, max_results=4)
            return "\n\n".join([f"Source: {r['title']} ({r['href']})\nSnippet: {r['body']}" for r in results])
        except Exception as e:
            logger.error(f"DDGS web search failed: {e}")
            return f"[Search failed: {e}]"

    async def _fetch_url(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
                # Extremely raw reading for now: strip basic HTML tags and collapse whitespace
                content = re.sub(r'<[^>]+>', ' ', response.text)
                content = re.sub(r'\s+', ' ', content).strip()
                return content[:6000] # truncate to avoid context limit
        except Exception as e:
            logger.error(f"URL fetch failed: {e}")
            return f"[URL fetch failed: {e}]"

    async def process_task(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        msg = task.message
        
        context_str = ""
        if task.routing.memory_context:
            context_str = "USER MEMORY/CONTEXT:\n" + "\n".join(task.routing.memory_context) + "\n\n"
            
        target_prompt = msg.text or ""
        external_context = ""
        
        # Simple heuristic injection
        if "http" in target_prompt:
            urls = [w for w in target_prompt.split() if w.startswith("http")]
            for url in urls[:2]: # At most 2 urls
                logger.info(f"Oracle fetching URL: {url}")
                fetched = await self._fetch_url(url)
                external_context += f"CONTENTS OF {url}:\n{fetched}\n\n"
                
        elif "search" in target_prompt.lower() or intent in ["research", "search"]:
            search_query = target_prompt
            if target_prompt.lower().startswith("search for"):
                search_query = target_prompt[10:].strip()
            elif target_prompt.lower().startswith("search"):
                search_query = target_prompt[6:].strip()
                
            logger.info(f"Oracle searching web for: {search_query}")
            search_results = await self._search_web(search_query)
            external_context += f"WEB SEARCH RESULTS FOR '{search_query}':\n{search_results}\n\n"

        prompt = f"USER INPUT:\n{target_prompt}\n\n"
        if context_str:
            prompt += context_str
        if external_context:
            prompt += external_context
            
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        try:
            response_text = await self.llm.chat_completion(messages)
            return TaskResult(
                task_id=task.task_id, 
                agent=self.name, 
                success=True, 
                text=response_text
            )
        except Exception as e:
            logger.exception("Oracle generation failed")
            return TaskResult(
                task_id=task.task_id, 
                agent=self.name, 
                success=False, 
                error=str(e)
            )
