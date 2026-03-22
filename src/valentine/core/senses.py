# src/valentine/agents/senses.py
"""
Valentine's environmental awareness — lets the agent understand
its own capabilities, installed tools, system resources, and constraints.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from valentine.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SystemInfo:
    """Snapshot of the current system environment."""

    hostname: str = ""
    os_name: str = ""
    os_version: str = ""
    architecture: str = ""
    cpu_count: int = 0
    memory_total_mb: int = 0
    memory_available_mb: int = 0
    disk_free_gb: float = 0.0
    python_version: str = ""
    uptime: str = ""


@dataclass
class EnvironmentMap:
    """Complete map of Valentine's capabilities and environment."""

    system: SystemInfo
    installed_runtimes: dict[str, str]  # {"python3": "/usr/bin/python3 (3.11.2)"}
    installed_tools: dict[str, bool]  # {"git": True, "docker": False, ...}
    available_skills: list[str]
    mcp_servers: list[str]
    workspace_path: str
    workspace_free_mb: int
    network_available: bool

    def to_prompt(self) -> str:
        """Format as a string suitable for injection into LLM system prompts."""
        lines: list[str] = []

        lines.append("=== Environment Awareness ===")

        # System
        s = self.system
        lines.append(
            f"System: {s.os_name} {s.os_version} ({s.architecture}) | "
            f"Host: {s.hostname}"
        )
        lines.append(
            f"CPU cores: {s.cpu_count} | "
            f"RAM: {s.memory_available_mb}MB free / {s.memory_total_mb}MB total | "
            f"Disk: {s.disk_free_gb:.1f}GB free"
        )
        lines.append(f"Python: {s.python_version}")
        if s.uptime:
            lines.append(f"Uptime: {s.uptime}")

        # Runtimes
        if self.installed_runtimes:
            lines.append("\nInstalled runtimes:")
            for name, version_info in sorted(self.installed_runtimes.items()):
                lines.append(f"  {name}: {version_info}")

        # Tools
        available = sorted(k for k, v in self.installed_tools.items() if v)
        missing = sorted(k for k, v in self.installed_tools.items() if not v)
        if available:
            lines.append(f"\nAvailable tools: {', '.join(available)}")
        if missing:
            lines.append(f"Missing tools: {', '.join(missing)}")

        # Skills
        if self.available_skills:
            lines.append(f"\nSkills: {', '.join(self.available_skills)}")

        # MCP
        if self.mcp_servers:
            lines.append(f"MCP servers: {', '.join(self.mcp_servers)}")

        # Workspace
        lines.append(
            f"\nWorkspace: {self.workspace_path} ({self.workspace_free_mb}MB free)"
        )
        lines.append(
            f"Network: {'available' if self.network_available else 'unavailable'}"
        )

        return "\n".join(lines)


class EnvironmentScanner:
    """Scans and maps Valentine's runtime environment."""

    TOOLS_TO_CHECK = [
        "git", "docker", "node", "npm", "npx", "python3", "pip",
        "ffmpeg", "pandoc", "libreoffice", "curl", "wget", "jq",
        "sqlite3", "redis-cli", "htop",
    ]

    RUNTIMES_TO_CHECK = [
        ("python3", "--version"),
        ("node", "--version"),
        ("npm", "--version"),
        ("docker", "--version"),
        ("git", "--version"),
    ]

    async def scan(self) -> EnvironmentMap:
        """Perform a full environment scan."""
        system_task = self._scan_system()
        runtimes_task = self._scan_runtimes()
        network_task = self._check_network()

        system, runtimes, network = await asyncio.gather(
            system_task, runtimes_task, network_task,
        )

        tools = self._scan_tools()
        available_skills = self._scan_skills()
        mcp_servers = list(settings.mcp_servers.keys())

        workspace_path = settings.workspace_dir
        workspace_free_mb = 0
        try:
            os.makedirs(workspace_path, exist_ok=True)
            usage = shutil.disk_usage(workspace_path)
            workspace_free_mb = int(usage.free / (1024 * 1024))
        except OSError:
            logger.warning("Could not stat workspace path: %s", workspace_path)

        return EnvironmentMap(
            system=system,
            installed_runtimes=runtimes,
            installed_tools=tools,
            available_skills=available_skills,
            mcp_servers=mcp_servers,
            workspace_path=workspace_path,
            workspace_free_mb=workspace_free_mb,
            network_available=network,
        )

    async def _scan_system(self) -> SystemInfo:
        """Get system-level info (OS, CPU, RAM, disk)."""
        info = SystemInfo(
            hostname=platform.node(),
            os_name=platform.system(),
            os_version=platform.release(),
            architecture=platform.machine(),
            cpu_count=os.cpu_count() or 0,
            python_version=platform.python_version(),
        )

        # Memory from /proc/meminfo (Linux)
        try:
            meminfo_path = Path("/proc/meminfo")
            if meminfo_path.exists():
                content = meminfo_path.read_text()
                for line in content.splitlines():
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        info.memory_total_mb = kb // 1024
                    elif line.startswith("MemAvailable:"):
                        kb = int(line.split()[1])
                        info.memory_available_mb = kb // 1024
        except (OSError, ValueError, IndexError):
            logger.debug("Could not read /proc/meminfo")

        # Disk free space at root
        try:
            usage = shutil.disk_usage("/")
            info.disk_free_gb = round(usage.free / (1024 ** 3), 2)
        except OSError:
            logger.debug("Could not stat disk usage for /")

        # Uptime from /proc/uptime (Linux)
        try:
            uptime_path = Path("/proc/uptime")
            if uptime_path.exists():
                raw = uptime_path.read_text().split()[0]
                total_seconds = int(float(raw))
                days, remainder = divmod(total_seconds, 86400)
                hours, remainder = divmod(remainder, 3600)
                minutes, _ = divmod(remainder, 60)
                parts: list[str] = []
                if days:
                    parts.append(f"{days}d")
                if hours:
                    parts.append(f"{hours}h")
                parts.append(f"{minutes}m")
                info.uptime = " ".join(parts)
        except (OSError, ValueError, IndexError):
            logger.debug("Could not read /proc/uptime")

        return info

    async def _scan_runtimes(self) -> dict[str, str]:
        """Check installed runtime versions."""
        results: dict[str, str] = {}

        async def _check_one(name: str, flag: str) -> None:
            path = shutil.which(name)
            if not path:
                return
            try:
                proc = await asyncio.create_subprocess_exec(
                    name, flag,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
                output = (stdout or stderr or b"").decode().strip()
                # Take only first line
                version_line = output.splitlines()[0] if output else "unknown"
                results[name] = f"{path} ({version_line})"
            except (asyncio.TimeoutError, OSError, IndexError) as exc:
                logger.debug("Runtime check failed for %s: %s", name, exc)
                results[name] = f"{path} (version unknown)"

        await asyncio.gather(
            *(_check_one(name, flag) for name, flag in self.RUNTIMES_TO_CHECK)
        )
        return results

    def _scan_tools(self) -> dict[str, bool]:
        """Check which tools are available via shutil.which."""
        return {tool: shutil.which(tool) is not None for tool in self.TOOLS_TO_CHECK}

    async def _check_network(self) -> bool:
        """Check if outbound network access is available."""
        try:
            # Try a TCP connection to a well-known DNS resolver
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("8.8.8.8", 53),
                timeout=3,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    def _scan_skills(self) -> list[str]:
        """Discover available skills from configured skill directories."""
        skills: list[str] = []
        for skills_dir in (settings.skills_dir, settings.skills_builtin_dir):
            try:
                base = Path(skills_dir)
                if not base.is_dir():
                    continue
                for child in sorted(base.iterdir()):
                    if child.is_dir() or child.suffix in (".sh", ".py"):
                        skills.append(child.stem)
            except OSError:
                continue
        return skills

    async def quick_scan(self) -> str:
        """Fast scan returning a formatted string for LLM context injection.

        Skips slow checks (runtime versions, network) for speed.
        """
        system = await self._scan_system()
        tools = self._scan_tools()

        available = sorted(k for k, v in tools.items() if v)
        missing = sorted(k for k, v in tools.items() if not v)

        lines = [
            f"System: {system.os_name} {system.os_version} | "
            f"CPU: {system.cpu_count} | "
            f"RAM: {system.memory_available_mb}MB free / {system.memory_total_mb}MB | "
            f"Disk: {system.disk_free_gb:.1f}GB free",
            f"Python: {system.python_version}",
            f"Tools: {', '.join(available)}",
        ]
        if missing:
            lines.append(f"Missing: {', '.join(missing)}")

        return "\n".join(lines)
