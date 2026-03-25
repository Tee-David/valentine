# src/valentine/main.py
from __future__ import annotations

import asyncio
import json
import logging
import multiprocessing
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread
from typing import Dict, Type, Any

from valentine.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("valentine.supervisor")


# ------------------------------------------------------------------
# Factory helpers — called inside each child process so nothing
# unpicklable crosses the process boundary.
# ------------------------------------------------------------------

def _make_bus():
    from valentine.bus.redis_bus import RedisBus
    return RedisBus()


def _make_primary_chain():
    from valentine.llm import FallbackChain, GroqClient, CerebrasClient, SambaNovaClient
    return FallbackChain([GroqClient(), CerebrasClient(), SambaNovaClient()])


def _make_groq():
    from valentine.llm import GroqClient
    return GroqClient()


def _make_sambanova():
    from valentine.llm import SambaNovaClient
    return SambaNovaClient()


# Maps agent name -> (agent_class_import_path, llm_factory)
AGENT_REGISTRY: dict[str, tuple[str, Any]] = {
    "zeroclaw":  ("valentine.orchestrator.zeroclaw", "ZeroClawRouter",  _make_primary_chain),
    "cortex":    ("valentine.agents.cortex",         "CortexAgent",     _make_primary_chain),
    "oracle":    ("valentine.agents.oracle",         "OracleAgent",     _make_primary_chain),
    "codesmith": ("valentine.agents.codesmith",      "CodeSmithAgent",  _make_primary_chain),
    "iris":      ("valentine.agents.iris",           "IrisAgent",       _make_groq),
    "echo":      ("valentine.agents.echo",           "EchoAgent",       _make_groq),
    "nexus":     ("valentine.agents.nexus",          "NexusAgent",      _make_primary_chain),
    "browser":   ("valentine.agents.browser",        "BrowserAgent",    _make_primary_chain),
}


def _import_agent_class(module_path: str, class_name: str):
    """Dynamically import an agent class by module path and class name."""
    import importlib
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


# ------------------------------------------------------------------
# Process entry points
# ------------------------------------------------------------------

def _run_agent_process(agent_name: str):
    """Entry point for each agent subprocess. Creates its own bus + LLM."""
    module_path, class_name, llm_factory = AGENT_REGISTRY[agent_name]

    async def run():
        agent_class = _import_agent_class(module_path, class_name)
        bus = _make_bus()
        llm = llm_factory()
        agent = agent_class(llm=llm, bus=bus)
        await agent.startup()
        await agent.listen_for_tasks()
        await agent.shutdown()

    try:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info(f"Process for agent {agent_name} starting loop...")
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Agent {agent_name} crashed: {e}")
        sys.exit(1)


def _run_bot_process():
    """Entry point for the Telegram adapter subprocess."""
    from valentine.nexus.telegram import TelegramAdapter

    async def run():
        bus = _make_bus()
        adapter = TelegramAdapter(bus=bus)
        await adapter.start()
        stop_event = asyncio.Event()
        try:
            await stop_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await adapter.stop()

    try:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info("Telegram adapter process starting...")
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Telegram adapter crashed: {e}")
        sys.exit(1)


def _run_mcp_bridge_process():
    """Entry point for the MCP bridge subprocess.

    Connects to all configured MCP servers, registers discovered tools in the
    shared Tool Registry, and proxies tool-call requests from agents via a
    Redis stream.
    """
    from valentine.mcp.client import MCPManager
    from valentine.tools.registry import ToolRegistry, ToolDefinition

    async def run():
        if not settings.mcp_servers:
            logger.info("No MCP servers configured. MCP bridge idle.")
            # Stay alive so the supervisor doesn't restart us in a loop.
            await asyncio.Event().wait()
            return

        mcp = MCPManager()
        registry = ToolRegistry()
        bus = _make_bus()

        try:
            # Connect to all MCP servers and discover tools
            tools = await mcp.start(settings.mcp_servers)
            logger.info(
                "MCP bridge discovered %d tools from %d servers",
                len(tools),
                len(settings.mcp_servers),
            )

            # Register in shared Tool Registry
            for tool in tools:
                await registry.register(tool)

            # Listen for tool call requests from agents
            stream = "valentine:mcp:requests"
            group = "mcp_bridge"
            consumer = "bridge_1"

            try:
                await bus.redis.xgroup_create(stream, group, mkstream=True)
            except Exception as e:
                if "BUSYGROUP" not in str(e):
                    raise

            logger.info("MCP bridge listening for tool call requests...")
            while True:
                try:
                    result = await bus.redis.xreadgroup(
                        group, consumer, {stream: ">"}, count=1, block=1000,
                    )
                    if not result:
                        continue

                    for _, messages in result:
                        for msg_id, data in messages:
                            payload_raw = data.get(b"payload") or data.get("payload")
                            if not payload_raw:
                                continue
                            request = json.loads(payload_raw)

                            call_id = request["call_id"]
                            server_name = request["server_name"]
                            tool_name = request["tool_name"]
                            arguments = request.get("arguments", {})

                            try:
                                output = await mcp.call_tool(
                                    server_name, tool_name, arguments,
                                )
                                response = {
                                    "call_id": call_id,
                                    "success": True,
                                    "output": output,
                                }
                            except Exception as e:
                                response = {
                                    "call_id": call_id,
                                    "success": False,
                                    "error": str(e),
                                }

                            # Publish result back for the requesting agent
                            result_key = f"valentine:mcp:results:{call_id}"
                            await bus.redis.rpush(result_key, json.dumps(response))
                            await bus.redis.expire(result_key, 120)

                            await bus.redis.xack(stream, group, msg_id)

                except Exception as e:
                    logger.error("MCP bridge error: %s", e)
                    await asyncio.sleep(1)
        finally:
            await mcp.shutdown()
            await registry.close()
            await bus.close()

    try:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
        )
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"MCP bridge crashed: {e}")
        sys.exit(1)


def _run_scheduler_process():
    """Entry point for the scheduler subprocess."""
    from valentine.core.scheduler import Scheduler

    async def run():
        scheduler = Scheduler()
        try:
            await scheduler.run_loop()
        finally:
            await scheduler.close()

    try:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info("Scheduler process starting...")
        asyncio.run(run())
    except Exception as e:
        logger.exception(f"Scheduler crashed: {e}")
        sys.exit(1)


def _run_workbench_api():
    """Entry point for the Telegram Mini App API backend."""
    import uvicorn
    from valentine.nexus.workbench_api import app

    try:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s",
        )
        logger.info("Workbench API starting on port 8001...")
        uvicorn.run(app, host="0.0.0.0", port=8001, log_level=settings.log_level.lower())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception(f"Workbench API crashed: {e}")
        sys.exit(1)


def _init_skills_in_registry():
    """Pre-populate the Tool Registry with installed skills (runs in main process)."""
    from valentine.skills.manager import SkillsManager
    from valentine.tools.registry import ToolRegistry, ToolDefinition

    async def _init():
        manager = SkillsManager(settings.skills_dir, settings.skills_builtin_dir)
        manifests = manager.discover_all()

        registry = ToolRegistry()
        try:
            for m in manifests:
                tool = ToolDefinition(
                    name=f"skill:{m.name}",
                    description=m.description,
                    parameters=m.parameters,
                    source="skill",
                )
                await registry.register(tool)
            logger.info("Registered %d skills in Tool Registry", len(manifests))
        finally:
            await registry.close()

    asyncio.run(_init())


# ------------------------------------------------------------------
# Supervisor
# ------------------------------------------------------------------

class ProcessSupervisor:
    def __init__(self):
        self.processes: Dict[str, multiprocessing.Process] = {}
        self.running = True

    def spawn_agent(self, name: str):
        if name not in AGENT_REGISTRY:
            logger.error(f"Unknown agent: {name}")
            return
        p = multiprocessing.Process(
            target=_run_agent_process,
            args=(name,),
            name=f"valentine-{name}",
            daemon=True,
        )
        p.start()
        self.processes[name] = p
        logger.info(f"Spawned {name} (PID: {p.pid})")

    def spawn_bot(self):
        p = multiprocessing.Process(
            target=_run_bot_process,
            name="valentine-telegram",
            daemon=True,
        )
        p.start()
        self.processes["telegram_bot"] = p
        logger.info(f"Spawned telegram bot (PID: {p.pid})")

    def spawn_mcp_bridge(self):
        p = multiprocessing.Process(
            target=_run_mcp_bridge_process,
            name="valentine-mcp-bridge",
            daemon=True,
        )
        p.start()
        self.processes["mcp_bridge"] = p
        logger.info(f"Spawned MCP bridge (PID: {p.pid})")

    def spawn_scheduler(self):
        p = multiprocessing.Process(
            target=_run_scheduler_process,
            name="valentine-scheduler",
            daemon=True,
        )
        p.start()
        self.processes["scheduler"] = p
        logger.info(f"Spawned scheduler (PID: {p.pid})")

    def spawn_workbench_api(self):
        p = multiprocessing.Process(
            target=_run_workbench_api,
            name="valentine-workbench",
            daemon=True,
        )
        p.start()
        self.processes["workbench_api"] = p
        logger.info(f"Spawned workbench API (PID: {p.pid})")

    def spawn_all(self):
        for name in AGENT_REGISTRY:
            self.spawn_agent(name)

    def monitor(self):
        while self.running:
            for name, p in list(self.processes.items()):
                if not p.is_alive() and self.running:
                    logger.warning(f"{name} (PID {p.pid}) died. Restarting...")
                    if name == "telegram_bot":
                        self.spawn_bot()
                    elif name == "mcp_bridge":
                        self.spawn_mcp_bridge()
                    elif name == "scheduler":
                        self.spawn_scheduler()
                    elif name == "workbench_api":
                        self.spawn_workbench_api()
                    else:
                        self.spawn_agent(name)
            time.sleep(5)

    def shutdown(self):
        self.running = False
        logger.info("Shutting down all processes...")
        for name, p in self.processes.items():
            if p.is_alive():
                logger.info(f"Terminating {name} (PID {p.pid})")
                p.terminate()
        for name, p in self.processes.items():
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing {name} (PID {p.pid})")
                p.kill()
        logger.info("All processes terminated.")


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------

class HealthHandler(BaseHTTPRequestHandler):
    supervisor: ProcessSupervisor | None = None

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        sup = self.__class__.supervisor
        statuses = {}
        all_ok = True
        if sup:
            for name, proc in sup.processes.items():
                alive = proc.is_alive()
                statuses[name] = "up" if alive else "down"
                if not alive:
                    all_ok = False
        body = json.dumps({"status": "ok" if all_ok else "degraded", "agents": statuses})
        self.send_response(200 if all_ok else 503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, fmt, *args):
        pass


def _start_health_server(supervisor: ProcessSupervisor, port: int = 8080):
    HealthHandler.supervisor = supervisor
    server = HTTPServer(("127.0.0.1", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True, name="health-check").start()
    logger.info(f"Health check on http://127.0.0.1:{port}/health")
    return server


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def _validate_secrets() -> dict[str, bool]:
    """Check all required secrets at startup and return availability map.
    Logs warnings for missing secrets so operators know what's misconfigured.
    """
    import os
    secrets_map = {
        "GROQ_API_KEY": bool(settings.groq_api_key),
        "CEREBRAS_API_KEY": bool(settings.cerebras_api_key),
        "SAMBANOVA_API_KEY": bool(settings.sambanova_api_key),
        "TELEGRAM_BOT_TOKEN": bool(settings.telegram_bot_token),
        "GITHUB_PAT": bool(os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN") or os.getenv("GITHUB_PAT")),
        "REDIS_URL": bool(settings.redis_url),
    }

    available = [k for k, v in secrets_map.items() if v]
    missing = [k for k, v in secrets_map.items() if not v]

    if available:
        logger.info("✅ Available integrations: %s", ", ".join(available))
    if missing:
        logger.warning("⚠️  Missing secrets (features disabled): %s", ", ".join(missing))

    # Critical check — can't run without at least one LLM and the bot token
    if not any([secrets_map["GROQ_API_KEY"], secrets_map["CEREBRAS_API_KEY"], secrets_map["SAMBANOVA_API_KEY"]]):
        logger.critical("❌ No LLM API keys configured! Valentine cannot function.")
    if not secrets_map["TELEGRAM_BOT_TOKEN"]:
        logger.critical("❌ No TELEGRAM_BOT_TOKEN! Bot will not start.")

    return secrets_map


def main():
    supervisor = ProcessSupervisor()

    def handle_sigterm(signum, frame):
        logger.info("Received SIGINT/SIGTERM. Shutting down.")
        supervisor.running = False

    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("Valentine process supervisor started.")

    # Validate secrets before anything else
    _validate_secrets()

    # Pre-populate Tool Registry with installed skills before agents start
    try:
        _init_skills_in_registry()
    except Exception as e:
        logger.warning("Skills registry init failed (non-fatal): %s", e)

    supervisor.spawn_all()
    supervisor.spawn_bot()
    supervisor.spawn_mcp_bridge()
    supervisor.spawn_scheduler()
    supervisor.spawn_workbench_api()

    health_server = _start_health_server(supervisor)

    try:
        supervisor.monitor()
    except KeyboardInterrupt:
        pass
    finally:
        health_server.shutdown()
        supervisor.shutdown()


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
