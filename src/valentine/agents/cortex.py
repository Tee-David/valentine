# src/valentine/agents/cortex.py
from __future__ import annotations

import logging
from typing import List

from mem0 import Memory
from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, IncomingMessage
from valentine.config import settings

logger = logging.getLogger(__name__)

class CortexAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.CORTEX,
            llm=llm,
            bus=bus,
            consumer_group="cortex_memory_workers",
            consumer_name="cortex_1"
        )
        
        # Configure mem0 to use local Qdrant + local embeddings
        mem0_config = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": settings.qdrant_host,
                    "port": settings.qdrant_port,
                    "collection_name": "valentine_memory"
                }
            },
            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": "all-MiniLM-L6-v2"
                }
            }
        }
        try:
            self.memory = Memory.from_config(mem0_config)
        except Exception as e:
            logger.warning(f"Failed to initialize Mem0. Memory features will be degraded: {e}")
            self.memory = None

    @property
    def system_prompt(self) -> str:
        return """You are Cortex, the memory and context agent for Valentine v2.
Your main duty is to extract details about the user (preferences, relationships, project names, technical architecture choices) and format them for long-term storage."""

    async def _extract_memories(self, msg: IncomingMessage):
        """Use LLM to explicitly extract facts from conversation"""
        prompt = f"Extract concise hard facts from this user message to remember long-term: {msg.text}"
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        extraction = await self.llm.chat_completion(messages, temperature=0.1)
        if extraction and len(extraction) > 5 and "nothing" not in extraction.lower():
            self.memory.add(extraction, user_id=msg.user_id, metadata={"source_msg": msg.message_id})

    async def process_task(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        msg = task.message
        
        if not self.memory:
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error="Memory layer uninitialized")

        try:
            if intent in ["store_memory", "chat"]:
                # If conversational, attempt to extract memory automatically
                await self._extract_memories(msg)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True, text="Memory extracted and stored.")
                
            elif intent == "search_memory":
                results = self.memory.search(msg.text, user_id=msg.user_id, limit=5)
                context = "\\n".join([r["text"] for r in results])
                return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=context)
                
            return TaskResult(task_id=task.task_id, agent=self.name, success=True, text="No memory action taken.")
            
        except Exception as e:
            logger.exception("Cortex memory operation failed")
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error=str(e))

    async def fetch_context_for_routing(self, message: IncomingMessage) -> List[str]:
        """A quick synchronous method ZeroClaw can call to inject state before routing"""
        if not self.memory or not message.text:
            return []
        try:
            results = self.memory.search(message.text, user_id=message.user_id, limit=3)
            return [r["text"] for r in results]
        except Exception as e:
            logger.error(f"Memory fast-search failed: {e}")
            return []
