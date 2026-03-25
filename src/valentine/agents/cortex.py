# src/valentine/agents/cortex.py
from __future__ import annotations

import logging
from typing import List

from valentine.agents.base import BaseAgent
from valentine.identity import internal_identity_block, COMPANY_NAME, CEO_NAME
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
            from mem0 import Memory  # lazy import
            self.memory = Memory.from_config(mem0_config)
        except Exception as e:
            logger.warning(f"Memory layer unavailable (non-fatal): {e}")
            self.memory = None

    @property
    def system_prompt(self) -> str:
        return internal_identity_block() + f"""You are Cortex, the memory and context agent for Valentine.
Built by {COMPANY_NAME}, led by {CEO_NAME}.

Your duties:
1. FACTUAL MEMORY: Extract facts about the user (preferences, names, projects, tech stack choices).
2. PROCEDURAL MEMORY: Extract HOW-TO knowledge from successful interactions:
   - Commands that worked (with their context)
   - Workflows that succeeded
   - Error resolutions (what failed and how it was fixed)
   - Environment-specific quirks (ports blocked, paths that work, ARM64 gotchas)
3. CAPABILITY MEMORY: Track what tools/skills/packages are installed and working.
4. CONSTRAINT MEMORY: Track limitations (rate limits hit, tools not available, ports blocked).

When extracting memories, categorize them:
- FACT: "User prefers React over Vue"
- PROCEDURE: "To generate a PDF report: pip install reportlab, then run the report-gen skill"
- CAPABILITY: "ffmpeg is installed at /usr/bin/ffmpeg, confirmed working"
- CONSTRAINT: "Groq API rate-limited at 30 req/min, switch to Cerebras when hitting 80%"
"""

    async def _extract_memories(self, msg: IncomingMessage, agent_response: str = ""):
        """Extract facts AND procedures from the conversation."""
        if not self.memory or not msg.text:
            return

        # Extract factual memories (existing behavior)
        fact_prompt = f"Extract concise hard facts about the user from this message: {msg.text}"
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": fact_prompt}
        ]
        extraction = await self.llm.chat_completion(messages, temperature=0.1)
        if extraction and len(extraction) > 5 and "nothing" not in extraction.lower():
            self.memory.add(
                extraction,
                user_id=msg.user_id,
                metadata={"type": "fact", "source_msg": msg.message_id}
            )

        # Extract cross-user procedural memories (Learning Layer)
        if agent_response:
            proc_prompt = (
                f"Extract actionable HOW-TO knowledge, successful workflows, error resolutions, "
                f"or environment constraints from this interaction.\n\n"
                f"User said: {msg.text}\n"
                f"Agent responded: {agent_response[:1000]}\n\n"
                f"CRITICAL PRIVACY RULES: You MUST generalize and redact the knowledge. "
                f"Remove ALL names, IDs, IP addresses, private file paths, passwords, and sensitive data. "
                f"Format: One clean, general procedure per line. If nothing procedural, respond with 'nothing'."
            )
            proc_messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": proc_prompt}
            ]
            proc_extraction = await self.llm.chat_completion(proc_messages, temperature=0.1)
            if proc_extraction and len(proc_extraction) > 5 and "nothing" not in proc_extraction.lower():
                self.memory.add(
                    proc_extraction,
                    user_id="global_system",  # Stored globally for cross-user learning
                    metadata={"type": "procedure", "source_msg": msg.message_id}
                )

    async def store_capability(self, user_id: str, capability: str):
        """Store a capability discovery (e.g., 'ffmpeg is installed and working')."""
        if self.memory:
            self.memory.add(
                f"[CAPABILITY] {capability}",
                user_id="global_system",
                metadata={"type": "capability"}
            )

    async def store_constraint(self, user_id: str, constraint: str):
        """Store a constraint discovery (e.g., 'port 3000 is blocked on VCN')."""
        if self.memory:
            self.memory.add(
                f"[CONSTRAINT] {constraint}",
                user_id="global_system",
                metadata={"type": "constraint"}
            )

    async def store_environment(self, user_id: str, env_snapshot: str):
        """Store an environment audit result for future reference."""
        if self.memory:
            self.memory.add(
                f"[ENVIRONMENT] {env_snapshot}",
                user_id="global_system",
                metadata={"type": "environment"}
            )

    async def process_task(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        msg = task.message

        if not self.memory:
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=True,
                text="Memory is temporarily unavailable. I'll still work, "
                     "but I won't remember things across conversations right now."
            )

        try:
            if intent == "store_memory" or intent == "chat":
                agent_response = task.routing.params.get("agent_response", "")
                await self._extract_memories(msg, agent_response=agent_response)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                                text="Memory extracted and stored.")

            elif intent == "store_capability":
                cap = msg.text or task.routing.params.get("capability", "")
                await self.store_capability(msg.user_id, cap)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                                text="Capability stored.")

            elif intent == "store_constraint":
                constraint = msg.text or task.routing.params.get("constraint", "")
                await self.store_constraint(msg.user_id, constraint)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                                text="Constraint stored.")

            elif intent == "store_environment":
                env = msg.text or task.routing.params.get("environment", "")
                await self.store_environment(msg.user_id, env)
                return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                                text="Environment snapshot stored.")

            elif intent == "search_memory":
                results = self.memory.search(msg.text, user_id=msg.user_id, limit=5)
                context = "\n".join([r["text"] for r in results])
                return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=context)

            elif intent == "search_procedures":
                # Search specifically for procedural knowledge (which is stored globally)
                query = f"how to {msg.text}" if not msg.text.lower().startswith("how") else msg.text
                results = self.memory.search(query, user_id="global_system", limit=5)
                # Filter for procedural entries
                procedures = [r["text"] for r in results if r.get("metadata", {}).get("type") in ("procedure", "capability")]
                if not procedures:
                    procedures = [r["text"] for r in results]  # fallback to all results
                return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                                text="\n".join(procedures))

            return TaskResult(task_id=task.task_id, agent=self.name, success=True,
                             text="No memory action taken.")

        except Exception as e:
            logger.exception("Cortex memory operation failed")
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error=str(e))

    async def fetch_context_for_routing(self, message: IncomingMessage) -> List[str]:
        """Fetch both factual and procedural context for routing."""
        if not self.memory or not message.text:
            return []
        try:
            # Search for relevant facts + procedures
            results = self.memory.search(message.text, user_id=message.user_id, limit=5)
            context = []
            for r in results:
                mem_type = r.get("metadata", {}).get("type", "fact")
                text = r["text"]
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
            logger.error(f"Memory fast-search failed: {e}")
            return []
