# src/valentine/skills/manager.py
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile

from valentine.config import settings
from valentine.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

# Patterns that are never allowed inside skill scripts.
_DANGEROUS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"rm\s+-rf\s+/(?:\s|$|\*)"),
    re.compile(r"mkfs\b"),
    re.compile(r"dd\s+if="),
    re.compile(r":\(\)\s*\{"),             # fork-bomb
    re.compile(r">\(\)\s*\{\s*:\|:&\s*\}"),  # fork-bomb variant
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
]


class SkillsManager:
    """Manages skill installation, discovery, and execution.

    Skills can come from:
    - Git repositories (cloned to *skills_dir*)
    - Built-in scripts (*builtin_dir*)
    - Legacy ``.sh`` files (backward compatible)
    """

    def __init__(
        self,
        skills_dir: str | None = None,
        builtin_dir: str | None = None,
    ) -> None:
        self.skills_dir = skills_dir or settings.skills_dir
        self.builtin_dir = builtin_dir or settings.skills_builtin_dir
        self.denylist = [
            "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "shutdown", "reboot",
            ":(){", "fork bomb", ">(){ :|:& };:",
        ]

    # ------------------------------------------------------------------
    # Installation
    # ------------------------------------------------------------------

    async def install_from_git(
        self,
        url: str,
        branch: str = "main",
    ) -> SkillManifest:
        """Clone a skill repository and install it.

        Steps:
        1. Clone to a temporary directory (shallow, single branch).
        2. Validate the clone (``skill.toml`` + entrypoint + safety scan).
        3. Move to ``skills_dir/<skill_name>/``.
        4. Return the parsed manifest.
        """
        tmp_dir = tempfile.mkdtemp(prefix="valentine_skill_")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", "--branch", branch, url, tmp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.skills_max_timeout,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git clone failed (exit {proc.returncode}): "
                    f"{stderr.decode(errors='replace').strip()}"
                )

            valid, reason = self.validate_skill(tmp_dir)
            if not valid:
                raise ValueError(f"Skill validation failed: {reason}")

            toml_path = os.path.join(tmp_dir, "skill.toml")
            manifest = SkillManifest.from_toml(toml_path)

            dest = os.path.join(self.skills_dir, manifest.name)
            if os.path.exists(dest):
                shutil.rmtree(dest)

            os.makedirs(self.skills_dir, exist_ok=True)
            shutil.move(tmp_dir, dest)
            manifest.source_path = dest
            logger.info("Installed skill '%s' from %s", manifest.name, url)
            return manifest

        except asyncio.TimeoutError:
            raise RuntimeError(
                f"git clone timed out after {settings.skills_max_timeout}s"
            )
        finally:
            # Clean up temp dir if it still exists (e.g. validation failed)
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    async def install_from_builtin(self, name: str) -> SkillManifest:
        """Copy a built-in skill into the installed skills directory."""
        # Check for a directory-based built-in first, then a legacy .sh file
        src_dir = os.path.join(self.builtin_dir, name)
        src_sh = os.path.join(self.builtin_dir, f"{name}.sh")

        if os.path.isdir(src_dir):
            dest = os.path.join(self.skills_dir, name)
            os.makedirs(self.skills_dir, exist_ok=True)
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(src_dir, dest)

            toml_path = os.path.join(dest, "skill.toml")
            if os.path.isfile(toml_path):
                manifest = SkillManifest.from_toml(toml_path)
            else:
                manifest = self._manifest_from_dir(dest)
            manifest.source_path = dest
            logger.info("Installed built-in skill '%s'", name)
            return manifest

        if os.path.isfile(src_sh):
            os.makedirs(self.skills_dir, exist_ok=True)
            dst = os.path.join(self.skills_dir, f"{name}.sh")
            shutil.copy2(src_sh, dst)
            os.chmod(dst, 0o755)
            manifest = SkillManifest.from_legacy_script(dst)
            logger.info("Installed built-in legacy skill '%s'", name)
            return manifest

        raise FileNotFoundError(f"Built-in skill '{name}' not found.")

    async def uninstall(self, name: str) -> bool:
        """Remove an installed skill by name. Returns True on success."""
        # Directory-based skill
        skill_dir = os.path.join(self.skills_dir, name)
        if os.path.isdir(skill_dir):
            shutil.rmtree(skill_dir)
            logger.info("Uninstalled skill '%s'", name)
            return True

        # Legacy .sh skill
        skill_sh = os.path.join(self.skills_dir, f"{name}.sh")
        if os.path.isfile(skill_sh):
            os.remove(skill_sh)
            logger.info("Uninstalled legacy skill '%s'", name)
            return True

        logger.warning("Skill '%s' not found for uninstall", name)
        return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_all(self) -> list[SkillManifest]:
        """Scan *skills_dir* and *builtin_dir* for all available skills.

        Handles both:
        - New format: directories containing ``skill.toml``
        - Legacy format: standalone ``.sh`` files
        """
        seen: dict[str, SkillManifest] = {}

        for base_dir in (self.skills_dir, self.builtin_dir):
            if not os.path.isdir(base_dir):
                continue

            for entry in sorted(os.listdir(base_dir)):
                entry_path = os.path.join(base_dir, entry)

                # Directory-based skill
                if os.path.isdir(entry_path):
                    toml_path = os.path.join(entry_path, "skill.toml")
                    if os.path.isfile(toml_path):
                        try:
                            m = SkillManifest.from_toml(toml_path)
                            seen.setdefault(m.name, m)
                        except Exception as exc:
                            logger.warning(
                                "Bad skill.toml in %s: %s", entry_path, exc,
                            )
                    else:
                        # Directory without skill.toml -- try legacy .sh files
                        m = self._manifest_from_dir(entry_path)
                        if m is not None:
                            seen.setdefault(m.name, m)
                    continue

                # Legacy standalone .sh file
                if entry.endswith(".sh") and os.path.isfile(entry_path):
                    m = SkillManifest.from_legacy_script(entry_path)
                    seen.setdefault(m.name, m)

        return list(seen.values())

    def get_skill(self, name: str) -> SkillManifest | None:
        """Return a specific skill's manifest, or *None*."""
        for m in self.discover_all():
            if m.name == name:
                return m
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        args: dict | str = "",
    ) -> str:
        """Execute a skill by name with optional arguments.

        - Validates skill exists
        - Checks the entrypoint against the denylist
        - Runs with a timeout
        - Captures and returns stdout/stderr
        """
        manifest = self.get_skill(name)
        if manifest is None:
            return f"Skill '{name}' not found."

        entrypoint = os.path.join(manifest.source_path, manifest.entrypoint)
        if not os.path.isfile(entrypoint):
            return f"Entrypoint '{manifest.entrypoint}' missing for skill '{name}'."

        # Safety: scan entrypoint for dangerous patterns
        if not self._is_entrypoint_safe(entrypoint):
            return f"Blocked: skill '{name}' contains dangerous commands."

        # Build the command
        if manifest.entrypoint.endswith(".py"):
            cmd_parts = ["python3", entrypoint]
        else:
            cmd_parts = ["bash", entrypoint]

        # Append arguments
        if isinstance(args, dict):
            for key, value in args.items():
                cmd_parts.extend([f"--{key}", str(value)])
        elif isinstance(args, str) and args:
            cmd_parts.extend(args.split())

        workspace = settings.workspace_dir
        os.makedirs(workspace, exist_ok=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.skills_max_timeout,
            )

            out = stdout.decode(errors="replace").strip()
            err = stderr.decode(errors="replace").strip()

            if proc.returncode == 0:
                return out if out else "[Skill ran successfully]"
            return f"[Skill error (exit {proc.returncode})]: {err or out}"

        except asyncio.TimeoutError:
            logger.warning("Skill '%s' timed out", name)
            return f"Skill '{name}' timed out after {settings.skills_max_timeout}s."
        except Exception as exc:
            logger.exception("Error running skill '%s'", name)
            return f"Error running skill '{name}': {exc}"

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_skill(self, skill_dir: str) -> tuple[bool, str]:
        """Validate a skill directory before installation.

        Checks:
        1. ``skill.toml`` exists and is parseable.
        2. The declared entrypoint file exists.
        3. The entrypoint has no dangerous patterns.
        """
        skill_dir = os.path.realpath(skill_dir)

        toml_path = os.path.join(skill_dir, "skill.toml")
        if not os.path.isfile(toml_path):
            return False, "Missing skill.toml"

        try:
            manifest = SkillManifest.from_toml(toml_path)
        except Exception as exc:
            return False, f"Invalid skill.toml: {exc}"

        entrypoint = os.path.join(skill_dir, manifest.entrypoint)
        if not os.path.isfile(entrypoint):
            return False, (
                f"Entrypoint '{manifest.entrypoint}' does not exist"
            )

        # Ensure entrypoint stays within the skill directory
        real_entry = os.path.realpath(entrypoint)
        if not real_entry.startswith(os.path.realpath(skill_dir)):
            return False, "Entrypoint path traversal detected"

        if not self._is_entrypoint_safe(entrypoint):
            return False, "Entrypoint contains dangerous patterns"

        return True, "OK"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_entrypoint_safe(self, path: str) -> bool:
        """Return False if the file contains any dangerous pattern."""
        try:
            with open(path, "r") as fh:
                content = fh.read()
        except Exception:
            return False

        lower = content.lower()
        for bad in self.denylist:
            if bad in lower:
                return False

        for pat in _DANGEROUS_PATTERNS:
            if pat.search(content):
                return False

        return True

    @staticmethod
    def _manifest_from_dir(dir_path: str) -> SkillManifest | None:
        """Build a manifest from a directory that has no ``skill.toml``.

        Looks for ``run.sh``, ``run.py``, or any ``.sh`` file as a fallback.
        """
        for candidate in ("run.sh", "run.py"):
            entry = os.path.join(dir_path, candidate)
            if os.path.isfile(entry):
                m = SkillManifest.from_legacy_script(entry)
                m.name = os.path.basename(dir_path)
                m.entrypoint = candidate
                m.source_path = dir_path
                return m

        # Fallback: first .sh file
        try:
            for f in sorted(os.listdir(dir_path)):
                if f.endswith(".sh"):
                    entry = os.path.join(dir_path, f)
                    m = SkillManifest.from_legacy_script(entry)
                    m.name = os.path.basename(dir_path)
                    m.entrypoint = f
                    m.source_path = dir_path
                    return m
        except OSError:
            pass

        return None
