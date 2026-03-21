# src/valentine/main.py
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
    format="%(asctime)s - %(processName)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("valentine.supervisor")

class ProcessSupervisor:
    def __init__(self):
        self.processes: Dict[str, multiprocessing.Process] = {}
        self.agent_registry: Dict[str, Dict[str, Any]] = {}
        self.running = True

    def register_agent(self, name: str, agent_class: Type, **kwargs):
        """Register an agent class and its initialization kwargs to be spawned"""
        self.agent_registry[name] = {"class": agent_class, "kwargs": kwargs}

    def _run_agent_process(self, agent_class: Type, name: str, kwargs: Dict[str, Any]):
        """Entry point for each agent process"""
        async def run():
            agent = agent_class(**kwargs)
            await agent.startup()
            await agent.listen_for_tasks()
            await agent.shutdown()

        try:
            logger.info(f"Process for agent {name} starting loop...")
            asyncio.run(run())
        except KeyboardInterrupt:
            # Expected during graceful shutdown
            pass
        except Exception as e:
            logger.exception(f"Agent {name} crashed with unhandled exception: {e}")
            sys.exit(1)

    def spawn_agent(self, name: str):
        """Spawns a specific registered agent in a background process"""
        if name not in self.agent_registry:
            logger.error(f"Cannot spawn unknown agent {name}")
            return

        registry_entry = self.agent_registry[name]
        p = multiprocessing.Process(
            target=self._run_agent_process,
            args=(registry_entry["class"], name, registry_entry["kwargs"]),
            name=f"valentine-agent-{name}",
            daemon=True
        )
        p.start()
        self.processes[name] = p
        logger.info(f"Spawned agent {name} (PID: {p.pid})")

    def _run_bot_process(self):
        """Entry point for the Telegram adapter process"""
        from valentine.nexus.telegram import TelegramAdapter
        from valentine.bus.redis_bus import RedisBus

        async def run():
            bus = RedisBus()
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
            logger.info("Telegram adapter process starting loop...")
            asyncio.run(run())
        except KeyboardInterrupt:
            pass
        except Exception as e:
            logger.exception(f"Telegram adapter crashed: {e}")
            sys.exit(1)

    def spawn_bot(self):
        p = multiprocessing.Process(
            target=self._run_bot_process,
            name="valentine-telegram-bot",
            daemon=True
        )
        p.start()
        self.processes["telegram_bot"] = p
        logger.info(f"Spawned telegram bot (PID: {p.pid})")

    def spawn_all(self):
        for name in self.agent_registry.keys():
            self.spawn_agent(name)

    def monitor(self):
        """Monitors and restarts crashed processes"""
        while self.running:
            for name, p in list(self.processes.items()):
                if not p.is_alive():
                    # Expected if stopping, unexpected if running
                    if self.running:
                        logger.warning(f"Agent {name} (PID {p.pid}) unexpectedly died. Restarting...")
                        self.spawn_agent(name)
            time.sleep(5)

    def shutdown(self):
        """Gracefully shutdown all agents"""
        self.running = False
        logger.info("Shutting down supervisor and all agent processes...")
        # Send SIGTERM to children
        for name, p in self.processes.items():
            if p.is_alive():
                logger.info(f"Terminating agent {name} (PID {p.pid})")
                p.terminate()
        
        # Wait and kill if necessary
        for name, p in self.processes.items():
            p.join(timeout=5)
            if p.is_alive():
                logger.warning(f"Force killing agent {name} (PID {p.pid})")
                p.kill()
        
        logger.info("All agent processes terminated.")

class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP health-check handler bound to the supervisor."""

    supervisor: "ProcessSupervisor | None" = None  # set before serving

    def do_GET(self):  # noqa: N802
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
        # Suppress default stderr logging for health checks
        pass


def _start_health_server(supervisor: ProcessSupervisor, port: int = 8080):
    """Run a tiny HTTP health-check server in a daemon thread."""
    HealthHandler.supervisor = supervisor
    server = HTTPServer(("127.0.0.1", port), HealthHandler)
    thread = Thread(target=server.serve_forever, daemon=True, name="health-check")
    thread.start()
    logger.info(f"Health check endpoint listening on http://127.0.0.1:{port}/health")
    return server


def main():
    supervisor = ProcessSupervisor()

    def handle_sigterm(signum, frame):
        logger.info("Received termination signal (SIGINT/SIGTERM). Initiating shutdown.")
        supervisor.running = False

    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)

    from valentine.agents import CortexAgent, OracleAgent, CodeSmithAgent, IrisAgent, EchoAgent, NexusAgent
    from valentine.orchestrator import ZeroClawRouter
    from valentine.models import AgentName
    from valentine.llm import FallbackChain, GroqClient, CerebrasClient, SambaNovaClient
    from valentine.bus.redis_bus import RedisBus

    primary_chain = FallbackChain([
        GroqClient(),
        CerebrasClient(),
        SambaNovaClient()
    ])
    bus = RedisBus()

    supervisor.register_agent(AgentName.ZEROCLAW, ZeroClawRouter, llm=primary_chain, bus=bus)
    supervisor.register_agent(AgentName.CORTEX, CortexAgent, llm=primary_chain, bus=bus)
    supervisor.register_agent(AgentName.ORACLE, OracleAgent, llm=primary_chain, bus=bus)
    supervisor.register_agent(AgentName.CODESMITH, CodeSmithAgent, llm=primary_chain, bus=bus)
    supervisor.register_agent(AgentName.IRIS, IrisAgent, llm=SambaNovaClient(), bus=bus)
    supervisor.register_agent(AgentName.ECHO, EchoAgent, llm=GroqClient(), bus=bus)
    supervisor.register_agent(AgentName.NEXUS, NexusAgent, llm=primary_chain, bus=bus)

    logger.info("Valentine v2 process supervisor started.")
    supervisor.spawn_all()
    supervisor.spawn_bot()

    # Start health check endpoint
    health_server = _start_health_server(supervisor)

    try:
        supervisor.monitor()
    except KeyboardInterrupt:
        pass
    finally:
        health_server.shutdown()
        supervisor.shutdown()

if __name__ == "__main__":
    # Ensure 'spawn' is used instead of 'fork' for safer async+multiprocessing
    multiprocessing.set_start_method("spawn")
    main()
