# src/valentine/mcp/client.py
from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from valentine.tools.registry import ToolDefinition

logger = logging.getLogger(__name__)


class MCPManager:
    """Manages connections to external MCP servers.

    Launches configured MCP servers as subprocesses (stdio transport),
    discovers their tools, and proxies tool calls.

    Each MCPManager instance should live in a single process -- MCP
    connections cannot be shared across processes.  The actual lifecycle
    (which process runs this) is handled by the orchestrator (Task 7).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._server_tools: dict[str, list[ToolDefinition]] = {}
        self._exit_stack: AsyncExitStack | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, server_configs: dict[str, dict[str, Any]]) -> list[ToolDefinition]:
        """Start all configured MCP servers and discover their tools.

        Args:
            server_configs: Mapping of *server_name* to a config dict with
                keys ``command`` (str), ``args`` (list[str]), and optionally
                ``env`` (dict[str, str]).

        Returns:
            Flat list of every :class:`ToolDefinition` discovered across all
            servers.
        """
        if not server_configs:
            logger.info("No MCP servers configured -- nothing to start")
            return []

        self._exit_stack = AsyncExitStack()
        await self._exit_stack.__aenter__()

        all_tools: list[ToolDefinition] = []

        for name, config in server_configs.items():
            try:
                tools = await self._connect_server(name, config)
                all_tools.extend(tools)
            except Exception:
                logger.exception("Failed to connect to MCP server '%s'", name)

        logger.info(
            "MCP startup complete: %d server(s) connected, %d tool(s) discovered",
            len(self._sessions),
            len(all_tools),
        )
        return all_tools

    async def _connect_server(self, name: str, config: dict[str, Any]) -> list[ToolDefinition]:
        """Connect to a single MCP server and discover its tools."""
        if self._exit_stack is None:
            raise RuntimeError("MCPManager.start() must be called before connecting servers")

        import os
        merged_env = os.environ.copy()
        if config.get("env"):
            merged_env.update(config["env"])

        server_params = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=merged_env,
        )

        logger.info("Connecting to MCP server '%s' (command=%s) ...", name, config["command"])

        # Enter both context managers via the exit stack so they stay open
        # until shutdown() is called.
        read_stream, write_stream = await self._exit_stack.enter_async_context(
            stdio_client(server_params),
        )
        session: ClientSession = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream),
        )
        await session.initialize()

        self._sessions[name] = session

        # Discover tools
        tools_result = await session.list_tools()
        tools: list[ToolDefinition] = []

        for mcp_tool in tools_result.tools:
            td = ToolDefinition(
                name=mcp_tool.name,
                description=mcp_tool.description or "",
                parameters=mcp_tool.inputSchema if mcp_tool.inputSchema else {},
                source="mcp",
                server_name=name,
            )
            tools.append(td)

        self._server_tools[name] = tools
        logger.info(
            "Connected to MCP server '%s' -- %d tool(s) available",
            name,
            len(tools),
        )
        return tools

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> str:
        """Call a tool on a specific MCP server.

        Args:
            server_name: The server that owns the tool.
            tool_name: Name of the tool to invoke.
            arguments: JSON-serialisable arguments for the tool.

        Returns:
            The textual output from the tool.

        Raises:
            KeyError: If the server is not connected.
            RuntimeError: If the tool call itself fails.
        """
        session = self._sessions.get(server_name)
        if session is None:
            raise KeyError(f"MCP server '{server_name}' is not connected")

        logger.info("Calling tool '%s' on server '%s'", tool_name, server_name)
        logger.debug("  arguments: %s", arguments)

        result = await session.call_tool(tool_name, arguments=arguments)

        # Concatenate all content parts into a single string.
        parts: list[str] = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(str(content))

        output = "\n".join(parts)

        if result.isError:
            logger.warning("Tool '%s' on '%s' returned an error: %s", tool_name, server_name, output)
            raise RuntimeError(f"MCP tool error ({server_name}/{tool_name}): {output}")

        logger.info("Tool '%s' on '%s' completed successfully", tool_name, server_name)
        return output

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_server_tools(self, server_name: str) -> list[ToolDefinition]:
        """Return tools discovered on *server_name* (empty list if unknown)."""
        return list(self._server_tools.get(server_name, []))

    def list_all_tools(self) -> list[ToolDefinition]:
        """Return every tool across all connected servers."""
        return [tool for tools in self._server_tools.values() for tool in tools]

    def is_connected(self, server_name: str) -> bool:
        """Check whether *server_name* has an active session."""
        return server_name in self._sessions

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Gracefully close every MCP server connection."""
        server_names = list(self._sessions.keys())

        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                logger.exception("Error while closing MCP exit stack")
            self._exit_stack = None

        self._sessions.clear()
        self._server_tools.clear()

        if server_names:
            logger.info("Shut down MCP servers: %s", ", ".join(server_names))
        else:
            logger.info("MCPManager shutdown (no servers were connected)")
