# src/valentine/bus/redis_bus.py
from __future__ import annotations

import redis.asyncio as redis
import json
import logging
from typing import Dict, Any, AsyncGenerator, List, Tuple

from valentine.config import settings

logger = logging.getLogger(__name__)

class RedisBus:
    def __init__(self, url: str | None = None):
        self.url = url or settings.redis_url
        self.redis = redis.from_url(self.url, decode_responses=False)
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        
        # Channel conventions
        self.ROUTER_STREAM = "zeroclaw.route"
        
    def stream_name(self, agent: str, type_: str) -> str:
        """Returns the stream name based on convention e.g., 'agent.oracle.task'"""
        return f"agent.{agent}.{type_}"

    async def check_health(self) -> bool:
        """Connection health check"""
        try:
            return await self.redis.ping()
        except Exception as e:
            logger.error(f"Redis connection failed: {e}")
            return False

    async def close(self):
        await self.pubsub.close()
        await self.redis.aclose()

    # --- Pub / Sub Management ---
    async def publish(self, channel: str, message: Dict[str, Any] | str) -> int:
        """Publish to a Redis Pub/Sub channel. Accepts a dict (auto-serialised) or raw string."""
        payload = message if isinstance(message, str) else json.dumps(message)
        return await self.redis.publish(channel, payload)

    async def subscribe(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
        await self.pubsub.subscribe(channel)
        async for message in self.pubsub.listen():
            if message["type"] == "message":
                try:
                    yield json.loads(message["data"])
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON message on {channel}")

    async def unsubscribe(self, channel: str):
        await self.pubsub.unsubscribe(channel)

    # --- Redis Streams (Task Queues) ---
    async def add_task(self, stream: str, task_kwargs: Dict[str, Any]) -> str:
        """Add a task payload to a stream"""
        data = {"payload": json.dumps(task_kwargs)}
        message_id = await self.redis.xadd(stream, data)
        # Parse return bytes to str if needed
        return message_id.decode("utf-8") if isinstance(message_id, bytes) else message_id

    async def read_tasks(
        self, 
        stream: str, 
        group: str, 
        consumer: str, 
        count: int = 1, 
        timeout_ms: int = 0
    ) -> List[Tuple[str, Dict[str, Any]]]:
        """Read tasks from a stream via a consumer group"""
        try:
            # Ensure consumer group exists, catch specific error if it already does
            await self.redis.xgroup_create(stream, group, mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise e

        # Read from group (> indicates messages not delivered to other consumers in group)
        result = await self.redis.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=timeout_ms
        )
        
        tasks = []
        if result:
            for _, messages in result:
                for message_id, data in messages:
                    payload_raw = data.get(b'payload') or data.get('payload')
                    if payload_raw:
                        payload = json.loads(payload_raw)
                        m_id = message_id.decode("utf-8") if isinstance(message_id, bytes) else message_id
                        tasks.append((m_id, payload))
        return tasks

    async def acknowledge_task(self, stream: str, group: str, message_id: str):
        """Acknowledge task completion in consumer group"""
        await self.redis.xack(stream, group, message_id)
