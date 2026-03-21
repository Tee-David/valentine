# src/valentine/agents/nexus.py
import logging
import json
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
            consumer_name="nexus_1"
        )
        self.tools = {
            "get_weather": {
                "description": "Get current weather for a location",
                "parameters": {"location": "string"}
            },
            "get_crypto_price": {
                "description": "Get current cryptocurrency price",
                "parameters": {"symbol": "string (e.g., BTC, ETH)"}
            }
        }

    @property
    def system_prompt(self) -> str:
        tools_json = json.dumps(self.tools, indent=2)
        return f"""You are Nexus, the API Specialist and Integration agent for Valentine v2.
You have access to the following external APIs/tools:
{tools_json}

If the user request can be answered by one of these tools, respond with a JSON object:
{{"tool": "tool_name", "parameters": {{"param1": "value1"}}}}
Otherwise, respond normally and explain what API you would need to fulfill the request.
Output ONLY JSON if calling a tool."""

    async def _execute_tool(self, tool_name: str, params: Dict[str, Any]) -> str:
        try:
            if tool_name == "get_weather":
                loc = params.get("location", "Unknown")
                return f"The weather in {loc} is currently sunny and 72°F. (Mock Data)"
            elif tool_name == "get_crypto_price":
                sym = params.get("symbol", "BTC").upper()
                prices = {"BTC": "$65,000", "ETH": "$3,500", "SOL": "$150"}
                return f"The current price of {sym} is {prices.get(sym, 'unknown')}. (Mock Data)"
            else:
                return f"Tool {tool_name} not found."
        except Exception as e:
            return f"Error executing {tool_name}: {e}"

    async def process_task(self, task: AgentTask) -> TaskResult:
        msg = task.message
        
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": msg.text or ""}
        ]
        
        try:
            kwargs = {}
            if self.llm.provider_name in ["groq", "cerebras"]:
                kwargs["response_format"] = {"type": "json_object"}
                
            response_text = await self.llm.chat_completion(messages, temperature=0.1, **kwargs)
            clean_text = response_text.replace("```json", "").replace("```", "").strip()
            
            try:
                data = json.loads(clean_text)
                if isinstance(data, dict) and "tool" in data:
                    tool_name = data["tool"]
                    params = data.get("parameters", {})
                    
                    logger.info(f"Nexus calling tool '{tool_name}' with {params}")
                    tool_result = await self._execute_tool(tool_name, params)
                    
                    followup = messages + [
                        {"role": "assistant", "content": clean_text},
                        {"role": "user", "content": f"Tool Result: {tool_result}\nSummarize this smoothly for the user."}
                    ]
                    # Don't restrict followup to JSON
                    final_response = await self.llm.chat_completion(followup, temperature=0.5)
                    return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=final_response)
            except json.JSONDecodeError:
                pass
                
            return TaskResult(task_id=task.task_id, agent=self.name, success=True, text=response_text)
            
        except Exception as e:
            logger.exception("Nexus processing failed")
            return TaskResult(task_id=task.task_id, agent=self.name, success=False, error=str(e))
