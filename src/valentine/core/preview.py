# src/valentine/core/preview.py
"""Cloudflare Tunnel preview system.

Starts a local dev server for a project, creates a Cloudflare Quick Tunnel
(no account required), and returns a public HTTPS URL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# How long to wait for cloudflared to print the tunnel URL
_TUNNEL_URL_TIMEOUT = 30
# How long to wait for the dev server to start
_SERVER_START_TIMEOUT = 15


@dataclass
class PreviewSession:
    """Tracks a running preview (dev server + tunnel)."""
    project_dir: str
    port: int
    url: str
    server_proc: Optional[subprocess.Popen] = field(default=None, repr=False)
    tunnel_proc: Optional[subprocess.Popen] = field(default=None, repr=False)

    def stop(self) -> None:
        """Kill both the dev server and the tunnel."""
        for proc in (self.tunnel_proc, self.server_proc):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass


def _detect_server_command(project_dir: str) -> tuple[str, int]:
    """Auto-detect the right dev server command and port for a project.

    Returns (command, expected_port).
    """
    pkg_json = os.path.join(project_dir, "package.json")
    if os.path.isfile(pkg_json):
        try:
            import json
            with open(pkg_json) as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            if "dev" in scripts:
                return "npm run dev", 3000
            if "start" in scripts:
                return "npm start", 3000
        except Exception:
            pass

    # Python projects
    manage_py = os.path.join(project_dir, "manage.py")
    if os.path.isfile(manage_py):
        return "python3 manage.py runserver 0.0.0.0:8000", 8000

    app_py = os.path.join(project_dir, "app.py")
    if os.path.isfile(app_py):
        return "python3 app.py", 5000

    # Static files fallback
    index_html = os.path.join(project_dir, "index.html")
    if os.path.isfile(index_html):
        return "python3 -m http.server 8080", 8080

    # Generic fallback
    return "python3 -m http.server 8080", 8080


def _find_cloudflared() -> Optional[str]:
    """Return the path to cloudflared binary, or None."""
    return shutil.which("cloudflared")


async def start_preview(
    project_dir: str,
    command: Optional[str] = None,
    port: Optional[int] = None,
) -> PreviewSession:
    """Start a dev server and Cloudflare Quick Tunnel.

    Args:
        project_dir: Path to the project to serve.
        command: Override the dev server command. Auto-detected if None.
        port: Override the port. Auto-detected if None.

    Returns:
        A PreviewSession with the public URL.

    Raises:
        RuntimeError: If cloudflared is not installed or tunnel fails.
    """
    cloudflared = _find_cloudflared()
    if not cloudflared:
        raise RuntimeError(
            "cloudflared is not installed. Install it with:\n"
            "  curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-linux-arm64 -o /usr/local/bin/cloudflared && "
            "chmod +x /usr/local/bin/cloudflared"
        )

    if not os.path.isdir(project_dir):
        raise RuntimeError(f"Project directory not found: {project_dir}")

    # Detect or use provided command/port
    detected_cmd, detected_port = _detect_server_command(project_dir)
    cmd = command or detected_cmd
    srv_port = port or detected_port

    # Start the dev server
    logger.info(f"Starting dev server: {cmd} (port {srv_port}) in {project_dir}")
    server_proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    # Give the server a moment to start
    await asyncio.sleep(2)
    if server_proc.poll() is not None:
        stderr = server_proc.stderr.read().decode() if server_proc.stderr else ""
        raise RuntimeError(f"Dev server exited immediately. stderr: {stderr[:500]}")

    # Start the Cloudflare Quick Tunnel
    tunnel_cmd = f"{cloudflared} tunnel --url http://localhost:{srv_port}"
    logger.info(f"Starting tunnel: {tunnel_cmd}")
    tunnel_proc = subprocess.Popen(
        tunnel_cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )

    # Parse the tunnel URL from cloudflared stderr output
    url = await _wait_for_tunnel_url(tunnel_proc)
    if not url:
        # Clean up on failure
        for proc in (tunnel_proc, server_proc):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        raise RuntimeError(
            "Cloudflare Tunnel failed to start. Check that cloudflared is working."
        )

    logger.info(f"Preview live at: {url}")
    return PreviewSession(
        project_dir=project_dir,
        port=srv_port,
        url=url,
        server_proc=server_proc,
        tunnel_proc=tunnel_proc,
    )


async def _wait_for_tunnel_url(proc: subprocess.Popen) -> Optional[str]:
    """Read cloudflared stderr until we find the tunnel URL."""
    url_pattern = re.compile(r"(https://[a-z0-9-]+\.trycloudflare\.com)")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _TUNNEL_URL_TIMEOUT

    while loop.time() < deadline:
        if proc.poll() is not None:
            return None

        # Read a line from stderr (cloudflared outputs there)
        try:
            line = await asyncio.wait_for(
                loop.run_in_executor(None, proc.stderr.readline),
                timeout=2,
            )
        except asyncio.TimeoutError:
            continue

        if not line:
            continue

        decoded = line.decode("utf-8", errors="replace")
        match = url_pattern.search(decoded)
        if match:
            return match.group(1)

    return None


# Global registry of active preview sessions
_active_sessions: dict[str, PreviewSession] = {}


async def create_preview(
    project_dir: str,
    command: Optional[str] = None,
    port: Optional[int] = None,
) -> str:
    """High-level: start a preview and track it. Returns a status message."""
    # Stop existing preview for the same directory
    if project_dir in _active_sessions:
        _active_sessions[project_dir].stop()
        del _active_sessions[project_dir]

    session = await start_preview(project_dir, command, port)
    _active_sessions[project_dir] = session
    return (
        f"Preview is live!\n"
        f"URL: {session.url}\n"
        f"Server: `{command or 'auto-detected'}` on port {session.port}\n"
        f"Project: {project_dir}\n\n"
        f"Send /stop_preview to shut it down."
    )


async def stop_preview(project_dir: Optional[str] = None) -> str:
    """Stop a running preview. If no dir given, stop all."""
    if project_dir and project_dir in _active_sessions:
        _active_sessions[project_dir].stop()
        del _active_sessions[project_dir]
        return f"Preview stopped for {project_dir}."

    if not project_dir and _active_sessions:
        for session in _active_sessions.values():
            session.stop()
        count = len(_active_sessions)
        _active_sessions.clear()
        return f"Stopped {count} preview(s)."

    return "No active previews to stop."
