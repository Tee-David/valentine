# src/valentine/agents/base.py
import abc
import asyncio
import logging
import signal
import time
from typing import Dict, Any

from valentine.bus.redis_bus import RedisBus
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.llm import LLMProvider

logger = logging.getLogger(__name__)

# Default per-task timeout (seconds).  Can be overridden per agent.
DEFAULT_TASK_TIMEOUT = 120


class BaseAgent(abc.ABC):
    def __init__(
        self,
        name: AgentName,
        llm: LLMProvider,
        bus: RedisBus,
        consumer_group: str = "agent_group",
        consumer_name: str = "worker_1",
        task_timeout: int = DEFAULT_TASK_TIMEOUT,
    ):
        self.name = name
        self.llm = llm
        self.bus = bus
        self.consumer_group = consumer_group
        self.consumer_name = consumer_name
        self.task_timeout = task_timeout

        self.task_stream = self.bus.stream_name(self.name.value, "task")
        self.result_stream = self.bus.stream_name(self.name.value, "result")

        self._shutdown_event = asyncio.Event()

    @property
    @abc.abstractmethod
    def system_prompt(self) -> str:
        """The core persona and instructions for this agent"""
        pass

    async def startup(self):
        logger.info(f"Agent {self.name.value} starting up. Connecting to bus...")
        await self.bus.check_health()

        # Register SIGTERM/SIGINT for graceful shutdown inside each process
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        logger.info(f"Agent {self.name.value} startup complete.")

    async def shutdown(self):
        logger.info(f"Agent {self.name.value} shutting down...")
        self._shutdown_event.set()
        await self.bus.close()
        logger.info(f"Agent {self.name.value} shutdown complete.")

    @abc.abstractmethod
    async def process_task(self, task: AgentTask) -> TaskResult:
        """Process the incoming task and return a TaskResult"""
        pass

    async def publish_result(self, result: TaskResult):
        # Write to the agent's result stream (for persistence / chaining)
        await self.bus.add_task(self.result_stream, result.to_dict())
        # Also publish to pub/sub so the Telegram bot (or other adapters) receive it
        await self.bus.publish("agent.response", result.to_dict())

    async def listen_for_tasks(self):
        logger.info(f"Agent {self.name.value} listening on {self.task_stream}...")
        while not self._shutdown_event.is_set():
            try:
                tasks = await self.bus.read_tasks(
                    self.task_stream,
                    self.consumer_group,
                    self.consumer_name,
                    count=1,
                    timeout_ms=1000,
                )
                for message_id, payload in tasks:
                    task = AgentTask.from_dict(payload)
                    logger.info(f"Agent {self.name.value} received task: {task.task_id}")

                    start = time.monotonic()
                    try:
                        result = await asyncio.wait_for(
                            self.process_task(task),
                            timeout=self.task_timeout,
                        )
                        if getattr(task.message, "chat_id", None):
                            result.chat_id = task.message.chat_id
                    except asyncio.TimeoutError:
                        elapsed = int(time.monotonic() - start)
                        logger.error(
                            f"Task {task.task_id} on {self.name.value} timed out "
                            f"after {elapsed}s"
                        )
                        result = TaskResult(
                            task_id=task.task_id,
                            agent=self.name,
                            success=False,
                            error=f"Processing timed out after {elapsed}s",
                        )
                        if getattr(task.message, "chat_id", None):
                            result.chat_id = task.message.chat_id
                    except Exception as e:
                        logger.exception(
                            f"Error processing task {task.task_id} on "
                            f"{self.name.value}: {e}"
                        )
                        result = TaskResult(
                            task_id=task.task_id,
                            agent=self.name,
                            success=False,
                            error=str(e),
                        )
                        if getattr(task.message, "chat_id", None):
                            result.chat_id = task.message.chat_id

                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    result.processing_time_ms = elapsed_ms

                    await self.publish_result(result)
                    await self.bus.acknowledge_task(
                        self.task_stream, self.consumer_group, message_id,
                    )
            except Exception as e:
                logger.error(f"Error reading tasks on {self.name.value}: {e}")
                await asyncio.sleep(1)

    async def is_healthy(self) -> bool:
        """Health check endpoint logic"""
        return await self.bus.check_health()
