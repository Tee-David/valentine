# src/valentine/tools/registry.py
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

import redis.asyncio as redis

from valentine.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    source: str  # "mcp", "skill", or "builtin"
    server_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "source": self.source,
            "server_name": self.server_name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ToolDefinition:
        return cls(
            name=data["name"],
            description=data["description"],
            parameters=data.get("parameters", {}),
            source=data["source"],
            server_name=data.get("server_name"),
        )


@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict

    def __post_init__(self):
        if not self.call_id:
            self.call_id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ToolCall:
        return cls(
            call_id=data["call_id"],
            tool_name=data["tool_name"],
            arguments=data.get("arguments", {}),
        )


@dataclass
class ToolResult:
    call_id: str
    success: bool
    output: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "success": self.success,
            "output": self.output,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ToolResult:
        return cls(
            call_id=data["call_id"],
            success=data["success"],
            output=data.get("output"),
            error=data.get("error"),
        )


class ToolRegistry:
    """Central registry for all available tools (MCP, skills, built-in).

    Stored in Redis so all agent processes can access it.
    Uses redis key 'valentine:tools:registry' as a hash map.
    """

    REDIS_KEY = "valentine:tools:registry"

    def __init__(self, redis_url: str | None = None):
        self.url = redis_url or settings.redis_url
        self.redis = redis.from_url(self.url, decode_responses=True)

    async def register(self, tool: ToolDefinition) -> None:
        """Register a tool in the registry (stored in Redis)."""
        await self.redis.hset(self.REDIS_KEY, tool.name, json.dumps(tool.to_dict()))
        logger.info("Registered tool: %s (source=%s)", tool.name, tool.source)

    async def unregister(self, tool_name: str) -> None:
        """Remove a tool from the registry."""
        removed = await self.redis.hdel(self.REDIS_KEY, tool_name)
        if removed:
            logger.info("Unregistered tool: %s", tool_name)
        else:
            logger.warning("Tool not found for unregister: %s", tool_name)

    async def get_tool(self, tool_name: str) -> ToolDefinition | None:
        """Get a specific tool by name."""
        raw = await self.redis.hget(self.REDIS_KEY, tool_name)
        if raw is None:
            return None
        return ToolDefinition.from_dict(json.loads(raw))

    async def list_tools(self, source: str | None = None) -> list[ToolDefinition]:
        """List all tools, optionally filtered by source."""
        raw_map = await self.redis.hgetall(self.REDIS_KEY)
        tools = [ToolDefinition.from_dict(json.loads(v)) for v in raw_map.values()]
        if source is not None:
            tools = [t for t in tools if t.source == source]
        return tools

    async def clear(self) -> None:
        """Clear all tools from registry (used on startup rebuild)."""
        await self.redis.delete(self.REDIS_KEY)
        logger.info("Cleared tool registry")

    def format_tools_for_llm(self, tools: list[ToolDefinition]) -> str:
        """Format tool list as a string for injection into LLM system prompts."""
        if not tools:
            return "No tools available."
        lines = []
        for tool in tools:
            params = ", ".join(tool.parameters.get("properties", {}).keys())
            line = f"- {tool.name}: {tool.description}"
            if params:
                line += f" (params: {params})"
            lines.append(line)
        return "\n".join(lines)
