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
    "iris":      ("valentine.agents.iris",           "IrisAgent",       _make_sambanova),
    "echo":      ("valentine.agents.echo",           "EchoAgent",       _make_groq),
    "nexus":     ("valentine.agents.nexus",          "NexusAgent",      _make_primary_chain),
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

def main():
    supervisor = ProcessSupervisor()

    def handle_sigterm(signum, frame):
        logger.info("Received SIGINT/SIGTERM. Shutting down.")
        supervisor.running = False

    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("Valentine v2 process supervisor started.")
    supervisor.spawn_all()
    supervisor.spawn_bot()

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
