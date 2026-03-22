# src/valentine/agents/nexus.py
from __future__ import annotations

import json
import logging
from typing import Dict, Any

import httpx

from valentine.agents.base import BaseAgent
from valentine.identity import identity_block
from valentine.models import AgentName, AgentTask, TaskResult
from valentine.utils import safe_parse_json

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
            identity_block()
            + "Currently operating in tool/API mode. You have access to external tools "
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
            identity_block()
            + "You just called a tool and got data back. Now present this information "
            "to the user naturally and conversationally. Don't just dump raw data — "
            "contextualize it, add relevant insight, and be helpful."
        )

    async def _execute_tool(self, tool_name: str, params: Dict[str, Any]) -> str:
        try:
            if tool_name == "get_weather":
                return await self._get_weather(params)
            elif tool_name == "get_crypto_price":
                return await self._get_crypto_price(params)
            else:
                return f"Tool '{tool_name}' is not available."
        except Exception as e:
            logger.error(f"Tool {tool_name} failed: {e}")
            return "I couldn't fetch that data right now. Please try again in a moment."

    async def _get_weather(self, params: dict) -> str:
        """Fetch real weather from Open-Meteo (free, no API key)."""
        location = params.get("location", "London")
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Geocode
            geo_resp = await client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1},
            )
            geo_data = geo_resp.json()
            results = geo_data.get("results")
            if not results:
                return f"Couldn't find location '{location}'."
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            place_name = results[0].get("name", location)
            country = results[0].get("country", "")
            # Weather
            weather_resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
                    "temperature_unit": "celsius",
                },
            )
            weather = weather_resp.json().get("current", {})
            temp_c = weather.get("temperature_2m", "N/A")
            temp_f = round(temp_c * 9/5 + 32, 1) if isinstance(temp_c, (int, float)) else "N/A"
            humidity = weather.get("relative_humidity_2m", "N/A")
            wind = weather.get("wind_speed_10m", "N/A")
            return (
                f"Weather in {place_name}, {country}: "
                f"{temp_c}°C ({temp_f}°F), "
                f"humidity {humidity}%, wind {wind} km/h"
            )

    async def _get_crypto_price(self, params: dict) -> str:
        """Fetch real crypto prices from CoinGecko (free, no API key)."""
        symbol = params.get("symbol", "BTC").upper()
        symbol_map = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "ADA": "cardano", "DOT": "polkadot", "DOGE": "dogecoin",
            "XRP": "ripple", "MATIC": "matic-network", "AVAX": "avalanche-2",
            "LINK": "chainlink", "BNB": "binancecoin", "LTC": "litecoin",
        }
        coin_id = symbol_map.get(symbol, symbol.lower())
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd", "include_24hr_change": "true"},
            )
            data = resp.json()
        if coin_id not in data:
            return f"Couldn't find price for '{symbol}'. Try BTC, ETH, SOL, etc."
        price = data[coin_id]["usd"]
        change = data[coin_id].get("usd_24h_change")
        change_str = f" ({change:+.2f}% 24h)" if change is not None else ""
        return f"{symbol}: ${price:,.2f}{change_str}"

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
            data = safe_parse_json(response_text)
            if data is not None and isinstance(data, dict) and "tool" in data:
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
