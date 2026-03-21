# src/valentine/agents/nexus.py
from __future__ import annotations

import json
import logging
from typing import Dict, Any

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult

logger = logging.getLogger(__name__)


class NexusAgent(BaseAgent):
    def __init__(self, llm, bus):
        super().__init__(
            name=AgentName.NEXUS,
            llm=llm,
            bus=bus,
            consumer_group="nexus_workers",
            consumer_name="nexus_1",
        )
        self.tools = {
            "get_weather": {
                "description": "Get current weather for a location",
                "parameters": {"location": "string"},
            },
            "get_crypto_price": {
                "description": "Get current cryptocurrency price",
                "parameters": {"symbol": "string (e.g., BTC, ETH, SOL)"},
            },
        }

    @property
    def system_prompt(self) -> str:
        tools_json = json.dumps(self.tools, indent=2)
        return (
            "You are Valentine, a brilliant and charismatic personal AI assistant — "
            "currently operating in tool/API mode. You have access to external tools "
            "that let you fetch real-time data.\n\n"
            f"Available tools:\n{tools_json}\n\n"
            "RULES:\n"
            "- If the user's request matches a tool, respond with ONLY a JSON object:\n"
            '  {"tool": "tool_name", "parameters": {"param1": "value1"}}\n'
            "- If no tool matches, respond naturally and explain what you can't do yet.\n"
            "- Be warm and conversational — you're Valentine, not a tool dispatcher.\n"
            "- When you can't fulfill a request, be honest but suggest alternatives.\n\n"
            "Output ONLY JSON if calling a tool. Otherwise respond naturally."
        )

    @property
    def _synthesis_prompt(self) -> str:
        return (
            "You are Valentine, a brilliant and charismatic personal AI assistant. "
            "You just called a tool and got data back. Now present this information "
            "to the user naturally and conversationally. Don't just dump raw data — "
            "contextualize it, add relevant insight, and be helpful.\n\n"
            "Be warm and confident. You're Valentine."
        )

    async def _execute_tool(self, tool_name: str, params: Dict[str, Any]) -> str:
        try:
            if tool_name == "get_weather":
                loc = params.get("location", "Unknown")
                return f"The weather in {loc} is currently sunny and 72°F. (Mock Data)"
            elif tool_name == "get_crypto_price":
                sym = params.get("symbol", "BTC").upper()
                prices = {"BTC": "$65,000", "ETH": "$3,500", "SOL": "$150"}
                price = prices.get(sym, "unavailable")
                return f"The current price of {sym} is {price}. (Mock Data)"
            else:
                return f"Tool '{tool_name}' not found."
        except Exception as e:
            return f"Error executing {tool_name}: {e}"

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        chat_id = msg.chat_id
        target_prompt = msg.text or ""

        # Load conversation history
        history = await self.bus.get_history(chat_id) if chat_id else []

        # Save user message to history
        if chat_id and target_prompt:
            await self.bus.append_history(chat_id, "user", target_prompt)

        messages = [{"role": "system", "content": self.system_prompt}]
        messages.extend(history[:-1])
        messages.append({"role": "user", "content": target_prompt})

        try:
            kwargs = {}
            if self.llm.provider_name in ("groq", "cerebras"):
                kwargs["response_format"] = {"type": "json_object"}

            response_text = await self.llm.chat_completion(
                messages, temperature=0.1, **kwargs,
            )
            clean_text = response_text.replace("```json", "").replace("```", "").strip()

            try:
                data = json.loads(clean_text)
                if isinstance(data, dict) and "tool" in data:
                    tool_name = data["tool"]
                    params = data.get("parameters", {})

                    logger.info(f"Nexus calling tool '{tool_name}' with {params}")
                    tool_result = await self._execute_tool(tool_name, params)

                    # Use synthesis prompt for natural response
                    synthesis_messages = [
                        {"role": "system", "content": self._synthesis_prompt},
                    ]
                    # Include recent history for context
                    synthesis_messages.extend(history[-4:])
                    synthesis_messages.append(
                        {"role": "user", "content": (
                            f"The user asked: \"{target_prompt}\"\n\n"
                            f"Tool '{tool_name}' returned: {tool_result}\n\n"
                            "Respond to the user naturally with this information."
                        )}
                    )

                    final_response = await self.llm.chat_completion(
                        synthesis_messages, temperature=0.7,
                    )

                    if chat_id:
                        await self.bus.append_history(chat_id, "assistant", final_response[:500])

                    return TaskResult(
                        task_id=task.task_id, agent=self.name,
                        success=True, text=final_response,
                    )
            except json.JSONDecodeError:
                pass

            # Non-tool response (LLM answered directly)
            if chat_id:
                await self.bus.append_history(chat_id, "assistant", response_text[:500])

            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=True, text=response_text,
            )

        except Exception as e:
            logger.exception("Nexus processing failed")
            return TaskResult(
                task_id=task.task_id, agent=self.name,
                success=False, error=str(e),
            )
