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
    import subprocess, re, shutil, time
    cloudflared = shutil.which("cloudflared") or os.path.expanduser("~/.local/bin/cloudflared")
    if not os.path.exists(cloudflared):
        logger.warning("cloudflared not found. Mini App will not have a public HTTPS URL.")
        return

    global _tunnel_proc
    _tunnel_proc = subprocess.Popen(
        [cloudflared, "tunnel", "--url", "http://localhost:8001"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    
    url_pattern = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
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


# ------------------------------------------------------------------
# Session Management (ChatGPT-style conversation threads)
# ------------------------------------------------------------------

_session_mgr = None

def _get_session_mgr():
    """Lazy-init the session manager with Redis."""
    global _session_mgr
    if _session_mgr is None:
        from valentine.core.session_manager import SessionManager
        redis_client = bus.redis if bus else None
        _session_mgr = SessionManager(redis_client=redis_client)
    return _session_mgr


@app.get("/api/sessions")
async def list_sessions(chat_id: str = None, user_id: str = None):
    """List all conversation sessions (like ChatGPT's sidebar)."""
    mgr = _get_session_mgr()
    sessions = await mgr.list_sessions(chat_id=chat_id, user_id=user_id)
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.title,
                "chat_id": s.chat_id,
                "project_path": s.project_path,
                "last_active": s.last_active,
                "message_count": len(s.messages),
                "has_summary": bool(s.summary),
            }
            for s in sessions
        ]
    }


@app.post("/api/sessions/new")
async def create_session(request: Request):
    """Create a new conversation session (like 'New Chat' button)."""
    body = await request.json()
    mgr = _get_session_mgr()
    session = await mgr.new_session(
        chat_id=body.get("chat_id", "default"),
        user_id=body.get("user_id", "user"),
        title=body.get("title", "New Conversation"),
        project_path=body.get("project_path"),
    )
    return {"ok": True, "session": session.to_dict()}


@app.post("/api/sessions/{session_id}/switch")
async def switch_session(session_id: str, request: Request):
    """Switch the active session for a chat."""
    body = await request.json()
    mgr = _get_session_mgr()
    session = await mgr.switch_session(
        chat_id=body.get("chat_id", "default"),
        session_id=session_id,
    )
    if session:
        return {"ok": True, "session": session.to_dict()}
    return {"ok": False, "error": "Session not found"}


@app.get("/api/sessions/{session_id}/history")
async def get_session_history(session_id: str):
    """Get conversation history for a specific session."""
    mgr = _get_session_mgr()
    session = await mgr._load_session(session_id)
    if not session:
        return {"ok": False, "error": "Session not found"}
    return {
        "ok": True,
        "title": session.title,
        "summary": session.summary,
        "messages": session.messages[-50:],  # Last 50 messages
        "total_messages": len(session.messages),
    }

