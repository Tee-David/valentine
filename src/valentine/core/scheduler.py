# src/valentine/scheduler.py
"""
Valentine's proactive scheduler — runs tasks on cron-like schedules.

Enables Valentine to work autonomously:
- "Check server health every hour"
- "Summarize AI news every morning at 8 AM"
- "Monitor my website every 10 minutes, alert if down"

Jobs are stored in Redis for persistence across restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis

from valentine.config import settings

logger = logging.getLogger(__name__)

# Redis keys
JOBS_KEY = "valentine:scheduler:jobs"         # Hash of all scheduled jobs
RESULTS_KEY = "valentine:scheduler:results"   # Recent results (sorted set by timestamp)


@dataclass
class ScheduledJob:
    """A scheduled recurring task."""
    job_id: str
    name: str                    # human-readable name
    chat_id: str                 # Telegram chat to send results to
    user_id: str                 # who created this job
    instruction: str             # what to do (sent to ZeroClaw as a message)
    cron_expression: str         # simplified cron: "every 10m", "every 1h", "daily 08:00"
    interval_seconds: int        # resolved interval in seconds
    enabled: bool = True
    last_run: float = 0.0       # timestamp of last execution
    next_run: float = 0.0       # timestamp of next scheduled run
    run_count: int = 0
    last_result: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "instruction": self.instruction,
            "cron_expression": self.cron_expression,
            "interval_seconds": self.interval_seconds,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "last_result": self.last_result,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ScheduledJob:
        return cls(
            job_id=data["job_id"],
            name=data["name"],
            chat_id=data["chat_id"],
            user_id=data["user_id"],
            instruction=data["instruction"],
            cron_expression=data["cron_expression"],
            interval_seconds=int(data["interval_seconds"]),
            enabled=data.get("enabled", True),
            last_run=float(data.get("last_run", 0)),
            next_run=float(data.get("next_run", 0)),
            run_count=int(data.get("run_count", 0)),
            last_result=data.get("last_result", ""),
            created_at=float(data.get("created_at", time.time())),
        )


def parse_schedule(expression: str) -> int:
    """
    Parse a simplified cron expression into interval seconds.

    Supported formats:
    - "every 5m" or "every 5 minutes" -> 300
    - "every 1h" or "every 1 hour" -> 3600
    - "every 30s" -> 30 (minimum 30s)
    - "daily" or "every day" -> 86400
    - "hourly" -> 3600
    - "weekly" -> 604800
    - "every 08:00" or "daily 08:00" -> 86400 (daily, specific time handled separately)
    """
    expr = expression.lower().strip()

    # Named intervals
    if expr in ("daily", "every day"):
        return 86400
    if expr in ("hourly", "every hour"):
        return 3600
    if expr in ("weekly", "every week"):
        return 604800

    # Parse "every Xm", "every Xh", "every Xs"
    match = re.match(
        r"every\s+(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|days?)",
        expr,
    )
    if match:
        value = int(match.group(1))
        unit = match.group(2)[0]  # first char: s, m, h, d
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = value * multipliers.get(unit, 60)
        return max(seconds, 30)  # minimum 30 seconds

    # "daily HH:MM" format -> treat as daily interval
    if "daily" in expr or re.match(r"every\s+\d{1,2}:\d{2}", expr):
        return 86400

    # Default: 1 hour
    logger.warning(f"Could not parse schedule '{expression}', defaulting to 1 hour")
    return 3600


class Scheduler:
    """
    Redis-backed job scheduler that runs in its own process.

    Checks for due jobs every 10 seconds. When a job is due,
    it injects the instruction into the ZeroClaw routing stream
    as if the user sent a message.
    """

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.redis_url
        self._redis: aioredis.Redis | None = None
        self._running = False

    async def _connect(self):
        if not self._redis:
            self._redis = aioredis.from_url(self.redis_url, decode_responses=True)

    async def close(self):
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # -- Job CRUD ------------------------------------------------------

    async def create_job(
        self,
        name: str,
        chat_id: str,
        user_id: str,
        instruction: str,
        schedule: str,
    ) -> ScheduledJob:
        """Create a new scheduled job."""
        await self._connect()

        interval = parse_schedule(schedule)
        job = ScheduledJob(
            job_id=str(uuid.uuid4())[:8],
            name=name,
            chat_id=chat_id,
            user_id=user_id,
            instruction=instruction,
            cron_expression=schedule,
            interval_seconds=interval,
            next_run=time.time() + interval,
        )

        await self._redis.hset(JOBS_KEY, job.job_id, json.dumps(job.to_dict()))
        logger.info(f"Created job '{name}' (every {interval}s) for chat {chat_id}")
        return job

    async def delete_job(self, job_id: str) -> bool:
        """Delete a scheduled job."""
        await self._connect()
        removed = await self._redis.hdel(JOBS_KEY, job_id)
        return removed > 0

    async def list_jobs(self, chat_id: str | None = None) -> list[ScheduledJob]:
        """List all jobs, optionally filtered by chat_id."""
        await self._connect()
        raw = await self._redis.hgetall(JOBS_KEY)
        jobs = []
        for data in raw.values():
            job = ScheduledJob.from_dict(json.loads(data))
            if chat_id is None or job.chat_id == chat_id:
                jobs.append(job)
        return sorted(jobs, key=lambda j: j.created_at)

    async def get_job(self, job_id: str) -> ScheduledJob | None:
        """Get a specific job."""
        await self._connect()
        raw = await self._redis.hget(JOBS_KEY, job_id)
        if raw:
            return ScheduledJob.from_dict(json.loads(raw))
        return None

    async def toggle_job(self, job_id: str) -> ScheduledJob | None:
        """Enable/disable a job."""
        job = await self.get_job(job_id)
        if job:
            job.enabled = not job.enabled
            await self._redis.hset(JOBS_KEY, job.job_id, json.dumps(job.to_dict()))
        return job

    async def format_jobs_list(self, chat_id: str) -> str:
        """Format jobs list for Telegram display."""
        jobs = await self.list_jobs(chat_id)
        if not jobs:
            return (
                "No scheduled jobs. Tell me something like "
                "'Check my server every hour' to create one."
            )

        lines = []
        for j in jobs:
            status = "ON" if j.enabled else "OFF"
            if j.last_run:
                last = datetime.fromtimestamp(
                    j.last_run, tz=timezone.utc
                ).strftime("%H:%M")
            else:
                last = "never"
            lines.append(
                f"[{status}] {j.name} ({j.cron_expression})\n"
                f"  ID: {j.job_id} | Runs: {j.run_count} | Last: {last}"
            )
        return "\n\n".join(lines)

    # -- Execution Loop ------------------------------------------------

    async def run_loop(self):
        """
        Main scheduler loop. Checks for due jobs every 10 seconds.
        Runs in its own process.
        """
        await self._connect()
        self._running = True
        logger.info("Scheduler loop started")

        while self._running:
            try:
                now = time.time()
                raw = await self._redis.hgetall(JOBS_KEY)

                for job_data in raw.values():
                    job = ScheduledJob.from_dict(json.loads(job_data))

                    if not job.enabled:
                        continue

                    if now >= job.next_run:
                        logger.info(
                            f"Scheduler: executing job '{job.name}' ({job.job_id})"
                        )
                        await self._execute_job(job)

                        # Update job state
                        job.last_run = now
                        job.next_run = now + job.interval_seconds
                        job.run_count += 1
                        await self._redis.hset(
                            JOBS_KEY, job.job_id, json.dumps(job.to_dict())
                        )

            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")

            await asyncio.sleep(10)  # check every 10 seconds

    async def _execute_job(self, job: ScheduledJob):
        """
        Execute a scheduled job by injecting it into the ZeroClaw routing stream
        as if the user sent a message.
        """
        from valentine.bus.redis_bus import RedisBus
        from valentine.models import (
            AgentName, AgentTask, ContentType, IncomingMessage,
            MessageSource, RoutingDecision,
        )

        bus = RedisBus()
        try:
            msg = IncomingMessage(
                message_id=f"sched-{job.job_id}-{job.run_count}",
                chat_id=job.chat_id,
                user_id=job.user_id,
                platform=MessageSource.TELEGRAM,
                content_type=ContentType.TEXT,
                text=f"[Scheduled: {job.name}] {job.instruction}",
            )
            task = AgentTask(
                task_id=str(uuid.uuid4()),
                agent=AgentName.ZEROCLAW,
                routing=RoutingDecision(
                    intent="scheduled_task", agent=AgentName.ZEROCLAW
                ),
                message=msg,
            )
            await bus.add_task(bus.ROUTER_STREAM, task.to_dict())
            logger.info(
                f"Injected scheduled task for job '{job.name}' into routing stream"
            )
        except Exception as e:
            logger.error(f"Failed to execute job '{job.name}': {e}")
            job.last_result = f"Error: {e}"
        finally:
            await bus.close()

    def stop(self):
        self._running = False
