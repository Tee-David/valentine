# src/valentine/orchestrator/zeroclaw.py
from __future__ import annotations

import json
import logging
from typing import List

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, RoutingDecision, IncomingMessage, ContentType

logger = logging.getLogger(__name__)


class ZeroClawRouter(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.ZEROCLAW,
            llm=llm,
            bus=bus,
            consumer_group="zeroclaw_routers",
            consumer_name="zeroclaw_1",
        )
        self.task_stream = self.bus.ROUTER_STREAM

    @property
    def system_prompt(self) -> str:
        return """You are ZeroClaw, the Master Orchestrator.
Your ONLY job is to analyze each incoming user message and decide which sub-agent should handle it.

Available Agents:
1. "oracle" — DEFAULT. Casual chat, questions, research, web search, summarization, games, conversation, general knowledge. Use this for anything that doesn't clearly fit another agent.
2. "codesmith" — Code generation, debugging, DevOps, shell commands, programming questions, GitHub operations, server management, skill installation/management, file operations, deployment tasks. Use when the user asks to:
   - Write, debug, or explain code
   - Run commands or scripts
   - Manage GitHub repos (clone, push, pull, PRs, issues)
   - Check server status, CPU, memory, disk
   - Install/list/run skills
   - Deploy or manage services
3. "iris" — Vision and images ONLY. Use when:
   - The user sends a PHOTO/IMAGE (content_type is "photo")
   - The user asks to GENERATE/CREATE/MAKE an image
   - The user asks for OCR or screenshot analysis
4. "echo" — Voice/audio ONLY. Use when content_type is "voice".

RULES:
- If content_type is "photo" → ALWAYS route to "iris", regardless of text.
- If content_type is "voice" → ALWAYS route to "echo".
- If the user asks to generate/create/make an image → route to "iris".
- GitHub, git, deploy, server, skills, shell → route to "codesmith".
- If unsure → route to "oracle".

Output ONLY valid JSON:
{"intent": "short description", "agent": "oracle|codesmith|iris|echo", "priority": "normal", "chain": []}

No markdown. No explanation. JSON only."""

    async def publish_result(self, result: TaskResult):
        # ZeroClaw results are internal — don't broadcast to the bot
        await self.bus.add_task(self.result_stream, result.to_dict())

    async def _fetch_context(self, message: IncomingMessage) -> List[str]:
        return []

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        context_items = await self._fetch_context(msg)

        # Build a rich prompt that includes content type and media info
        prompt_parts = []
        if msg.text:
            prompt_parts.append(f"User message: {msg.text}")
        else:
            prompt_parts.append("User message: (no text)")

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

            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_text)

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

            routing = RoutingDecision(
                intent=data.get("intent", "chat"),
                agent=target_agent,
                priority=data.get("priority", "normal"),
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
