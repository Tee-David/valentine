"""
Docker-based sandbox for safe execution of untrusted code.

When CodeSmith needs to run something risky (full-stack apps,
user-submitted code, experimental installations), it can run
in an isolated Docker container instead of directly on the VM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field

from valentine.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SandboxResult:
    """Result of a sandboxed execution."""
    success: bool
    output: str = ""
    error: str = ""
    exit_code: int = 0
    container_id: str = ""
    files_created: list[str] = field(default_factory=list)


class DockerSandbox:
    """
    Runs code in isolated Docker containers.

    Features:
    - Memory limits (256MB default)
    - CPU limits (0.5 CPU default)
    - Network isolation (optional)
    - Auto-cleanup after execution
    - File extraction from container
    - Timeout enforcement
    """

    # Base images for different languages
    IMAGES = {
        "python": "python:3.11-slim",
        "node": "node:20-slim",
        "shell": "ubuntu:24.04",
    }

    DEFAULT_MEMORY = "256m"
    DEFAULT_CPU = "0.5"
    DEFAULT_TIMEOUT = 60

    def __init__(self):
        self._docker_available: bool | None = None

    async def is_available(self) -> bool:
        """Check if Docker is installed and accessible."""
        if self._docker_available is not None:
            return self._docker_available

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            self._docker_available = proc.returncode == 0
        except (FileNotFoundError, asyncio.TimeoutError):
            self._docker_available = False

        if not self._docker_available:
            logger.warning("Docker not available — sandbox features disabled")
        return self._docker_available

    async def run_code(
        self,
        code: str,
        language: str = "python",
        timeout: int = DEFAULT_TIMEOUT,
        memory: str = DEFAULT_MEMORY,
        cpu: str = DEFAULT_CPU,
        network: bool = False,
        volumes: dict[str, str] | None = None,
    ) -> SandboxResult:
        """
        Run code in an isolated Docker container.

        Args:
            code: The code to execute
            language: "python", "node", or "shell"
            timeout: Max execution time in seconds
            memory: Memory limit (e.g., "256m", "1g")
            cpu: CPU limit (e.g., "0.5", "1")
            network: Whether to allow network access
            volumes: Host path -> container path mappings
        """
        if not await self.is_available():
            return SandboxResult(
                success=False,
                error="Docker not available. Install Docker to use sandbox features.",
            )

        image = self.IMAGES.get(language, self.IMAGES["shell"])
        container_name = f"valentine-sandbox-{uuid.uuid4().hex[:8]}"

        # Write code to a temp file
        tmp_dir = f"/tmp/valentine-sandbox-{uuid.uuid4().hex[:8]}"
        os.makedirs(tmp_dir, exist_ok=True)

        if language == "python":
            code_file = os.path.join(tmp_dir, "main.py")
            cmd_in_container = ["python", "/sandbox/main.py"]
        elif language == "node":
            code_file = os.path.join(tmp_dir, "main.js")
            cmd_in_container = ["node", "/sandbox/main.js"]
        else:
            code_file = os.path.join(tmp_dir, "main.sh")
            cmd_in_container = ["bash", "/sandbox/main.sh"]

        with open(code_file, "w") as f:
            f.write(code)

        # Build docker run command
        docker_cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",
            "--memory", memory,
            "--cpus", cpu,
            "-v", f"{tmp_dir}:/sandbox:ro",
        ]

        # Output directory for generated files
        output_dir = os.path.join(tmp_dir, "output")
        os.makedirs(output_dir, exist_ok=True)
        docker_cmd.extend(["-v", f"{output_dir}:/output"])

        if not network:
            docker_cmd.append("--network=none")

        # Additional volumes
        if volumes:
            for host_path, container_path in volumes.items():
                docker_cmd.extend(["-v", f"{host_path}:{container_path}"])

        docker_cmd.append(image)
        docker_cmd.extend(cmd_in_container)

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            output = stdout.decode(errors="replace")[:50000]
            error = stderr.decode(errors="replace")[:10000]

            # Check for generated files
            files = []
            if os.path.isdir(output_dir):
                for f in os.listdir(output_dir):
                    files.append(os.path.join(output_dir, f))

            return SandboxResult(
                success=proc.returncode == 0,
                output=output.strip(),
                error=error.strip() if proc.returncode != 0 else "",
                exit_code=proc.returncode or 0,
                container_id=container_name,
                files_created=files,
            )

        except asyncio.TimeoutError:
            # Kill the container
            await self._kill_container(container_name)
            return SandboxResult(
                success=False,
                error=f"Execution timed out after {timeout}s",
                container_id=container_name,
            )
        except Exception as e:
            logger.exception("Docker sandbox execution failed")
            return SandboxResult(success=False, error=str(e))
        finally:
            # Cleanup temp files (keep output dir for file retrieval)
            try:
                os.remove(code_file)
            except OSError:
                pass

    async def run_shell(
        self,
        commands: list[str],
        timeout: int = DEFAULT_TIMEOUT,
        network: bool = False,
    ) -> SandboxResult:
        """Run shell commands in a sandbox."""
        script = "#!/bin/bash\nset -e\n" + "\n".join(commands)
        return await self.run_code(script, language="shell", timeout=timeout, network=network)

    async def run_project(
        self,
        project_dir: str,
        start_command: str,
        language: str = "node",
        timeout: int = 120,
        port: int | None = None,
    ) -> SandboxResult:
        """
        Run a full project in a sandbox (e.g., a React app).

        Args:
            project_dir: Local path to the project
            start_command: Command to run (e.g., "npm install && npm start")
            language: Base image language
            port: Port to expose (optional)
        """
        if not await self.is_available():
            return SandboxResult(success=False, error="Docker not available")

        image = self.IMAGES.get(language, self.IMAGES["node"])
        container_name = f"valentine-project-{uuid.uuid4().hex[:8]}"

        docker_cmd = [
            "docker", "run",
            "--name", container_name,
            "--rm",
            "--memory", "512m",
            "--cpus", "1",
            "-v", f"{project_dir}:/app",
            "-w", "/app",
        ]

        if port:
            docker_cmd.extend(["-p", f"{port}:{port}"])

        docker_cmd.extend([image, "bash", "-c", start_command])

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            return SandboxResult(
                success=proc.returncode == 0,
                output=stdout.decode(errors="replace")[:50000].strip(),
                error=stderr.decode(errors="replace")[:10000].strip() if proc.returncode != 0 else "",
                exit_code=proc.returncode or 0,
                container_id=container_name,
            )
        except asyncio.TimeoutError:
            await self._kill_container(container_name)
            return SandboxResult(success=False, error=f"Project timed out after {timeout}s")

    async def _kill_container(self, name: str):
        """Force kill a running container."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "kill", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            pass

    async def cleanup_old_containers(self):
        """Remove any lingering valentine sandbox containers."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "-a", "--filter", "name=valentine-sandbox",
                "--filter", "name=valentine-project", "-q",
                stdout=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            container_ids = stdout.decode().strip().split()
            if container_ids:
                await asyncio.create_subprocess_exec(
                    "docker", "rm", "-f", *container_ids,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except Exception:
            pass
