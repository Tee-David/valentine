# src/valentine/nexus/workbench_api.py
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

try:
    from sse_starlette.sse import EventSourceResponse
except ImportError:
    EventSourceResponse = None  # SSE is optional

from valentine.bus.redis_bus import RedisBus

logger = logging.getLogger(__name__)

app = FastAPI(title="Valentine Workbench API")

# Serve the built Vite Mini App frontend as static files
# The frontend should be built to /opt/valentine/frontend-miniapp/dist/
_FRONTEND_DIST = Path(os.environ.get(
    "VALENTINE_MINIAPP_DIST",
    "/opt/valentine/frontend-miniapp/dist"
))
if _FRONTEND_DIST.is_dir():
    app.mount("/app", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="miniapp")
    logger.info("Serving Mini App frontend from %s", _FRONTEND_DIST)
else:
    logger.warning("Mini App dist not found at %s — /app will 404", _FRONTEND_DIST)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global bus instance for the API
bus: RedisBus | None = None
_tunnel_proc = None

async def _start_cloudflare_tunnel():
    """Start cloudflared to expose port 8000 and save URL to Redis."""
    import subprocess, re, shutil
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        logger.warning("cloudflared not found. Mini App will not have a public HTTPS URL.")
        return

    global _tunnel_proc
    _tunnel_proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", "http://localhost:8000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    
    url_pattern = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
    import time
    timeout = time.time() + 15
    while time.time() < timeout:
        line = _tunnel_proc.stderr.readline()
        if not line:
            await asyncio.sleep(0.1)
            continue
        match = url_pattern.search(line)
        if match:
            url = match.group(1)
            logger.info("Workbench Tunnel live at: %s", url)
            if bus:
                await bus.redis.set("valentine:workbench:live_url", url)
            return
            
    logger.error("Failed to extract Cloudflare URL for Workbench")

@app.on_event("startup")
async def startup():
    global bus
    bus = RedisBus()
    logger.info("Workbench API connected to Redis")
    asyncio.create_task(_start_cloudflare_tunnel())

@app.on_event("shutdown")
async def shutdown():
    global _tunnel_proc
    if _tunnel_proc:
        _tunnel_proc.terminate()
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


@app.get("/api/projects")
async def list_projects():
    """List all projects that currently have an active Cloudflare tunnel."""
    if not bus:
        return {"projects": []}

    # Scan Redis for all preview URL keys
    projects = []
    async for key in bus.redis.scan_iter("valentine:workbench:preview_url:*"):
        project_id = key.decode("utf-8").split(":")[-1]
        url = await bus.redis.get(key)
        projects.append({
            "id": project_id,
            "url": url.decode("utf-8") if url else None,
            "status": "live" if url else "offline",
        })
    return {"projects": projects}


@app.post("/api/action")
async def receive_miniapp_action(request: Request):
    """REST bridge for Mini App → Valentine agent communication.

    The React frontend uses this when opened via InlineKeyboard (where sendData
    is not available). The payload is published to Redis so ZeroClaw can route it.

    Expected body: {"chat_id": "...", "user_id": "...", "action": "...", "detail": "..."}
    """
    if not bus:
        return {"ok": False, "error": "Bus not initialized"}

    body = await request.json()
    chat_id = body.get("chat_id")
    user_id = body.get("user_id", "miniapp")
    action = body.get("action", "unknown")
    detail = body.get("detail", "")

    if not chat_id:
        return {"ok": False, "error": "chat_id is required"}

    # Publish as a synthetic incoming message on the router stream
    import uuid
    from datetime import datetime, timezone

    synthetic_msg = {
        "message_id": f"miniapp-{uuid.uuid4().hex[:8]}",
        "chat_id": chat_id,
        "user_id": user_id,
        "platform": "miniapp",
        "content_type": "text",
        "text": f"[MiniApp action={action}] {detail}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    task_data = {
        "task_id": str(uuid.uuid4()),
        "agent": "zeroclaw",
        "routing": {"intent": "incoming", "agent": "zeroclaw", "priority": "normal"},
        "message": synthetic_msg,
        "previous_results": [],
    }

    await bus.add_task(bus.ROUTER_STREAM, task_data)
    logger.info(f"MiniApp action bridged to ZeroClaw: {action} for chat {chat_id}")

    return {"ok": True, "task_id": task_data["task_id"]}
