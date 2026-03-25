# src/valentine/nexus/workbench_api.py
import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from valentine.bus.redis_bus import RedisBus

logger = logging.getLogger(__name__)

app = FastAPI(title="Valentine Workbench API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global bus instance for the API
bus: RedisBus | None = None


@app.on_event("startup")
async def startup():
    global bus
    bus = RedisBus()
    logger.info("Workbench API connected to Redis")


@app.on_event("shutdown")
async def shutdown():
    if bus:
        await bus.close()


@app.get("/api/projects/{project_id}/status")
async def get_project_status(project_id: str):
    """Get the current Cloudflare tunnel URL for a project."""
    if not bus:
        return {"status": "error", "message": "Bus not initialized"}
    
    url = await bus.redis.get(f"valentine:workbench:preview_url:{project_id}")
    if url:
        return {"status": "live", "url": url.decode("utf-8")}
    return {"status": "offline", "url": None}


async def event_generator(request: Request, project_id: str) -> AsyncGenerator:
    """Stream SSE events from Redis pubsub for a specific project."""
    if not bus:
        return
        
    pubsub = bus.redis.pubsub()
    channel = f"valentine:workbench:events:{project_id}"
    await pubsub.subscribe(channel)
    
    try:
        while True:
            if await request.is_disconnected():
                logger.debug(f"Client disconnected from SSE channel: {channel}")
                break
                
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message:
                data = message["data"].decode("utf-8")
                yield {
                    "event": "update",
                    "data": data
                }
    finally:
        await pubsub.unsubscribe(channel)


@app.get("/api/projects/{project_id}/events")
async def project_events(request: Request, project_id: str):
    """SSE endpoint for project events (like 'reload')."""
    return EventSourceResponse(event_generator(request, project_id))

