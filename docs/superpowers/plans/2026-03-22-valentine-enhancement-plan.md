# Valentine Enhancement Plan: MCP + Dynamic Skills + Autonomy

**Date:** 2026-03-22
**Goal:** Transform Valentine from a closed multi-agent system into a fully extensible autonomous agent platform with external tool access, dynamic skill installation, and configurable autonomy modes.

---

## Overview

Three integrated enhancements:
1. **MCP Client Integration** — Connect to external MCP servers (GitHub, SQLite, filesystem, web search, etc.)
2. **Dynamic Skills System** — Install/manage/execute skills from GitHub repos at runtime
3. **Autonomy Modes** — Configurable supervised vs full-auto execution with approval gates

These share a **unified Tool Registry** that makes all tools (MCP + skills + built-in) discoverable by any agent.

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         Tool Registry            │
                    │  (MCP tools + Skills + Built-in) │
                    └──────┬──────────┬────────────────┘
                           │          │
              ┌────────────┴──┐  ┌────┴───────────┐
              │  MCP Manager  │  │ Skills Manager  │
              │ (stdio/SSE)   │  │ (git + local)   │
              └───────────────┘  └─────────────────┘
                     │                    │
         ┌───────────┼────────┐    ┌──────┴──────┐
         │GitHub MCP │SQLite  │    │Shell scripts │
         │Server     │MCP     │    │Python skills │
         └───────────┴────────┘    └─────────────┘
```

---

## Task Breakdown

### Task 1: Tool Registry & Models
**File:** `src/valentine/tools/registry.py`, modify `src/valentine/models.py`
**Dependencies:** None (foundation layer)

Add shared data models and a registry that all agents can query:

- `ToolDefinition` dataclass: name, description, parameters (JSON schema), source (mcp/skill/builtin)
- `ToolCall` dataclass: tool_name, arguments, call_id
- `ToolResult` dataclass: call_id, success, output, error
- `ToolRegistry` class: register(), discover(), get_tool(), list_tools(), call_tool()

### Task 2: MCP Client Manager
**File:** `src/valentine/mcp/client.py`, `src/valentine/mcp/__init__.py`
**Dependencies:** Task 1

Build an async MCP client that:
- Reads MCP server configs from settings
- Launches MCP servers via stdio subprocess or connects via SSE/HTTP
- Discovers tools from each server on startup
- Registers discovered tools in the Tool Registry
- Proxies tool calls to the correct MCP server
- Handles server lifecycle (start, health check, restart, shutdown)

Implementation approach: Use the `mcp` Python SDK (`pip install mcp`) which provides `ClientSession`, `StdioServerParameters`, and `stdio_client` context manager. Each configured server gets a persistent `ClientSession`.

### Task 3: Dynamic Skills Manager
**File:** `src/valentine/skills/manager.py`, `src/valentine/skills/manifest.py`, `src/valentine/skills/__init__.py`
**Dependencies:** Task 1

Upgrade from hardcoded .sh scanning to a full plugin system:
- `SkillManifest` dataclass: name, version, description, author, entrypoint, dependencies, parameters
- `SkillsManager` class:
  - `install(source: str)` — Clone from git URL or copy from built-in
  - `uninstall(name: str)` — Remove skill directory
  - `list_installed()` — Scan skills_dir for manifests
  - `execute(name: str, args: dict)` — Run skill with sandboxing (timeout, resource limits, denylist)
  - `validate(path: str)` — Check manifest, entrypoint exists, no dangerous patterns
- Skills register themselves in the Tool Registry on discovery
- Support both shell scripts (.sh) and Python scripts (.py) as entrypoints
- Skill manifest format: `skill.toml` in skill directory root

### Task 4: Autonomy Mode System
**File:** `src/valentine/autonomy.py`, modify `src/valentine/config.py`
**Dependencies:** Task 1

Add configurable execution modes:
- `AutonomyMode` enum: SUPERVISED, FULL, READONLY
- `AutonomyGate` class:
  - Checks if a tool call requires approval based on mode + tool risk level
  - In SUPERVISED mode: queues dangerous actions for user approval via Telegram inline buttons
  - In FULL mode: executes all actions without approval
  - In READONLY mode: blocks all write/execute actions
- Per-tool risk classification: LOW (read), MEDIUM (write file), HIGH (shell exec, git push, deploy)
- Approval flow via Redis: gate publishes approval request, waits for user response

### Task 5: Config & Dependencies Update
**File:** `src/valentine/config.py`, `pyproject.toml`, `.env.template`
**Dependencies:** None (can run in parallel with Task 1)

Add to Settings:
```python
# MCP
mcp_servers: dict[str, dict] = {}  # name -> {command, args, env}

# Autonomy
autonomy_mode: str = "supervised"  # supervised | full | readonly

# Skills
skills_git_sources: list[str] = []  # pre-installed skill repo URLs
skills_allow_network: bool = False  # skills can make network calls
skills_max_timeout: int = 60
```

Add to pyproject.toml:
```toml
"mcp>=1.0",
```

Update .env.template with MCP server examples and GitHub PAT placeholder.

### Task 6: Integrate Tool Registry into Agents
**File:** Modify `src/valentine/orchestrator/zeroclaw.py`, `src/valentine/agents/codesmith.py`, `src/valentine/agents/oracle.py`
**Dependencies:** Tasks 1-4

- **ZeroClaw**: Include available tools in routing prompt so it knows what tools exist. Add a new routing target "tool_call" when the user's request maps to a specific tool.
- **CodeSmith**: Replace hardcoded skill system with SkillsManager. Add MCP tool calling to action set. New actions: `{"action": "tool", "name": "github_create_issue", "args": {...}}`
- **Oracle**: Can use MCP tools for enhanced web search, database queries, etc.

### Task 7: Main Entry Point & Lifecycle
**File:** Modify `src/valentine/main.py`
**Dependencies:** Tasks 1-5

- Initialize MCP Manager on startup (before spawning agents)
- Initialize Skills Manager and scan installed skills
- Populate Tool Registry
- Pass registry reference to agent processes
- Add MCP server health to /health endpoint
- Graceful shutdown: close all MCP server connections

### Task 8: MCP Server Configs (Pre-built)
**File:** `configs/mcp-servers.example.toml`
**Dependencies:** Task 2

Provide example configurations for popular MCP servers:
- GitHub (`@modelcontextprotocol/server-github`)
- Filesystem (`@modelcontextprotocol/server-filesystem`)
- SQLite (`mcp-server-sqlite`)
- Web Search (Brave/DuckDuckGo MCP servers)

---

## Implementation Order

**Phase A (parallel — foundation):**
- Task 1: Tool Registry & Models
- Task 5: Config & Dependencies

**Phase B (parallel — core systems):**
- Task 2: MCP Client Manager
- Task 3: Dynamic Skills Manager
- Task 4: Autonomy Mode System

**Phase C (sequential — integration):**
- Task 6: Agent Integration
- Task 7: Main Entry Point
- Task 8: Example Configs

---

## Key Design Decisions

1. **MCP over custom tool protocols** — Industry standard, hundreds of existing servers
2. **Tool Registry as shared state** — All agents see all tools, ZeroClaw routes intelligently
3. **skill.toml manifest** — Simple, human-readable, git-friendly
4. **Autonomy via Redis approval queue** — Fits existing bus architecture, works across processes
5. **Process-safe registry** — Registry serialized to Redis on startup, agents read from Redis (no shared memory needed across processes)
