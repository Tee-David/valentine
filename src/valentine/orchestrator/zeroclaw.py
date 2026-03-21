# src/valentine/orchestrator/zeroclaw.py
import json
import logging
from typing import List

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, RoutingDecision, IncomingMessage

logger = logging.getLogger(__name__)

class ZeroClawRouter(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.ZEROCLAW, 
            llm=llm, 
            bus=bus,
            consumer_group="zeroclaw_routers",
            consumer_name="zeroclaw_1"
        )
        # Override the task stream to listen to the main ingress topic where Nexus publishes
        self.task_stream = self.bus.ROUTER_STREAM

    @property
    def system_prompt(self) -> str:
        return """You are ZeroClaw, the Master Orchestrator AI.
Your ONLY job is to analyze incoming user messages, extract the context, and route the task to exactly one appropriate sub-agent.

Available Agents:
1. "oracle": Default agent. Good for casual conversation, web search, answering general questions, reasoning, summarizing.
2. "codesmith": Code generation, DevOps, debugging, terminal shell commands, github.
3. "iris": Vision tasks. Image analysis, OCR, visual questions, or image generation.
4. "echo": Voice synthesis, speech-to-text.

Output ONLY valid JSON matching this schema:
{
  "intent": "short description of user goal",
  "agent": "oracle|codesmith|iris|echo",
  "priority": "normal|urgent",
  "chain": []
}
NEVER output anything outside the JSON block. Do not format as markdown.
"""

    async def publish_result(self, result: TaskResult):
        # ZeroClaw's results are internal routing confirmations — don't broadcast to bot
        await self.bus.add_task(self.result_stream, result.to_dict())

    async def _fetch_context(self, message: IncomingMessage) -> List[str]:
        # TODO in Phase 5: Hook into Cortex directly to query relevant memories
        return []

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        context_items = await self._fetch_context(msg)
        
        prompt = f"Message: {msg.text}\n"
        if msg.media_path:
             prompt += f"Attached Media: {msg.media_path}\n"
        if context_items:
             prompt += f"Context: {context_items}\n"
             
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        try:
            # We try to force JSON output
            kwargs = {}
            if self.llm.provider_name in ["groq", "cerebras"]:
                kwargs["response_format"] = {"type": "json_object"}
                
            response_text = await self.llm.chat_completion(
                messages, 
                temperature=0.1, 
                **kwargs
            )
            
            # Clean up potential markdown formatting
            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            data = json.loads(clean_text)
            
            # Determine target agent
            agent_str = data.get("agent", "oracle").lower()
            try:
                target_agent = AgentName(agent_str)
            except ValueError:
                target_agent = AgentName.ORACLE
                
            routing = RoutingDecision(
                intent=data.get("intent", "chat"),
                agent=target_agent,
                priority=data.get("priority", "normal"),
                chain=[AgentName(a) for a in data.get("chain", [])] if data.get("chain") else None,
                memory_context=context_items
            )
            
            # Formulate the delegated task
            delegated_task = AgentTask(
                task_id=task.task_id,
                agent=target_agent,
                routing=routing,
                message=msg,
                previous_results=task.previous_results
            )
            
            # Append it to the target agent's stream
            target_stream = self.bus.stream_name(target_agent.value, "task")
            await self.bus.add_task(target_stream, delegated_task.to_dict())
            
            logger.info(f"ZeroClaw routed task {delegated_task.task_id} to {target_agent.value}")
            
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                text=f"Task routed to {target_agent.value}"
            )
            
        except Exception as e:
            logger.error(f"ZeroClaw failed to parse intent, routing to Oracle fallback: {e}")
            fallback_routing = RoutingDecision(intent="fallback", agent=AgentName.ORACLE)
            fallback_task = AgentTask(
                task_id=task.task_id,
                agent=AgentName.ORACLE,
                routing=fallback_routing,
                message=msg
            )
            await self.bus.add_task(self.bus.stream_name("oracle", "task"), fallback_task.to_dict())
            return TaskResult(task_id=task.task_id, agent=self.name, success=True, text="Fallback routing applied")
