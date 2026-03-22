# src/valentine/agents/evolution.py
"""
Valentine's self-evolution system — automatically installs missing
tools, packages, and skills when it encounters something it can't do.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class InstallResult:
    success: bool
    tool_name: str
    method: str  # "apt", "pip", "npm", "skill", "already_installed", "unknown"
    message: str


class SelfEvolver:
    """
    When Valentine encounters a missing capability, this system:
    1. Identifies what's needed
    2. Determines the install method (apt, pip, npm, skill)
    3. Attempts installation
    4. Verifies the install worked
    5. Stores the result in memory for future reference
    """

    # Map of common tools to their install methods
    INSTALL_MAP: dict[str, dict] = {
        # System packages (apt)
        "pandoc": {"method": "apt", "package": "pandoc"},
        "ffmpeg": {"method": "apt", "package": "ffmpeg"},
        "sqlite3": {"method": "apt", "package": "sqlite3"},
        "jq": {"method": "apt", "package": "jq"},
        "htop": {"method": "apt", "package": "htop"},
        "libreoffice": {"method": "apt", "package": "libreoffice-core"},
        "imagemagick": {"method": "apt", "package": "imagemagick"},
        "wkhtmltopdf": {"method": "apt", "package": "wkhtmltopdf"},
        # Python packages (pip)
        "openpyxl": {"method": "pip", "package": "openpyxl"},
        "reportlab": {"method": "pip", "package": "reportlab"},
        "python-docx": {"method": "pip", "package": "python-docx"},
        "pandas": {"method": "pip", "package": "pandas"},
        "matplotlib": {"method": "pip", "package": "matplotlib"},
        "pillow": {"method": "pip", "package": "Pillow"},
        "beautifulsoup4": {"method": "pip", "package": "beautifulsoup4"},
        "pdfkit": {"method": "pip", "package": "pdfkit"},
        "xlsxwriter": {"method": "pip", "package": "XlsxWriter"},
        "markdown": {"method": "pip", "package": "markdown"},
        # npm packages
        "mermaid": {"method": "npm", "package": "@mermaid-js/mermaid-cli"},
    }

    # Map Python import names to INSTALL_MAP keys when they differ
    _IMPORT_TO_TOOL: dict[str, str] = {
        "PIL": "pillow",
        "bs4": "beautifulsoup4",
        "docx": "python-docx",
    }

    # Common error patterns and their corresponding tool suggestions
    _ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r"No module named ['\"](\w[\w.]*)"), "_python_module"),
        (re.compile(r"ModuleNotFoundError:.*['\"](\w[\w.]*)"), "_python_module"),
        (re.compile(r"(\w+): command not found"), "_command"),
        (re.compile(r"(\w+): not found"), "_command"),
        (re.compile(r"Cannot find module ['\"](\w[\w./-]*)"), "_npm_module"),
        (re.compile(r"Error: Cannot find module ['\"](\w[\w./-]*)"), "_npm_module"),
        (re.compile(r"ImportError:.*['\"](\w[\w.]*)"), "_python_module"),
        (re.compile(r"FileNotFoundError.*No such file.*['\"]?(/usr/bin/|)(\w+)"), "_command_from_path"),
    ]

    def __init__(self, allow_apt: bool = False):
        """
        Args:
            allow_apt: Whether to allow system package installation (requires sudo).
                       Default False for safety.
        """
        self.allow_apt = allow_apt
        self._install_history: list[InstallResult] = []

    def is_available(self, tool: str) -> bool:
        """Check if a tool/package is currently available."""
        # Check system tools via shutil.which
        if shutil.which(tool):
            return True

        # Check Python packages via importlib
        install_info = self.INSTALL_MAP.get(tool)
        if install_info and install_info["method"] == "pip":
            return self._is_python_package_available(tool)

        return False

    def _is_python_package_available(self, tool: str) -> bool:
        """Check if a Python package can be imported."""
        # Determine the import name — most pip packages use their own name,
        # but some differ (e.g. Pillow -> PIL, python-docx -> docx)
        import_name = tool.replace("-", "_")
        # Special cases
        import_overrides = {
            "pillow": "PIL",
            "python-docx": "docx",
            "beautifulsoup4": "bs4",
        }
        import_name = import_overrides.get(tool, import_name)

        try:
            importlib.import_module(import_name)
            return True
        except ImportError:
            return False

    async def ensure_available(self, tool: str) -> InstallResult:
        """Ensure a tool is available, installing it if needed."""
        if self.is_available(tool):
            result = InstallResult(
                True, tool, "already_installed", f"{tool} is already available"
            )
            self._install_history.append(result)
            return result

        install_info = self.INSTALL_MAP.get(tool)
        if not install_info:
            result = InstallResult(
                False, tool, "unknown", f"Don't know how to install '{tool}'"
            )
            self._install_history.append(result)
            return result

        method = install_info["method"]
        package = install_info["package"]

        if method == "apt":
            result = await self._install_apt(tool, package)
        elif method == "pip":
            result = await self._install_pip(tool, package)
        elif method == "npm":
            result = await self._install_npm(tool, package)
        else:
            result = InstallResult(
                False, tool, method, f"Unsupported install method: {method}"
            )

        self._install_history.append(result)
        return result

    async def _install_pip(self, tool: str, package: str) -> InstallResult:
        """Install a Python package via pip."""
        logger.info("Installing Python package: %s (pip install %s)", tool, package)
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-m", "pip", "install", "--quiet", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode == 0:
                # Invalidate module cache so the next import can find it
                importlib.invalidate_caches()
                verified = await self._verify_install(tool)
                if verified:
                    return InstallResult(
                        True, tool, "pip", f"Successfully installed {package} via pip"
                    )
                return InstallResult(
                    False, tool, "pip",
                    f"pip install {package} succeeded but verification failed",
                )

            err_text = (stderr or b"").decode().strip()
            return InstallResult(
                False, tool, "pip",
                f"pip install {package} failed (exit {proc.returncode}): {err_text[:200]}",
            )
        except asyncio.TimeoutError:
            return InstallResult(
                False, tool, "pip", f"pip install {package} timed out after 120s"
            )
        except OSError as exc:
            return InstallResult(
                False, tool, "pip", f"Failed to run pip: {exc}"
            )

    async def _install_apt(self, tool: str, package: str) -> InstallResult:
        """Install a system package via apt (requires allow_apt=True)."""
        if not self.allow_apt:
            return InstallResult(
                False, tool, "apt",
                f"System package installation not allowed (set allow_apt=True to enable). "
                f"Would install: sudo apt-get install -y {package}",
            )

        logger.info("Installing system package: %s (apt-get install %s)", tool, package)
        try:
            # Update package lists first
            update_proc = await asyncio.create_subprocess_exec(
                "sudo", "apt-get", "update", "-qq",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(update_proc.communicate(), timeout=60)

            proc = await asyncio.create_subprocess_exec(
                "sudo", "apt-get", "install", "-y", "-qq", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)

            if proc.returncode == 0:
                verified = await self._verify_install(tool)
                if verified:
                    return InstallResult(
                        True, tool, "apt",
                        f"Successfully installed {package} via apt",
                    )
                return InstallResult(
                    False, tool, "apt",
                    f"apt install {package} succeeded but verification failed",
                )

            err_text = (stderr or b"").decode().strip()
            return InstallResult(
                False, tool, "apt",
                f"apt install {package} failed (exit {proc.returncode}): {err_text[:200]}",
            )
        except asyncio.TimeoutError:
            return InstallResult(
                False, tool, "apt",
                f"apt install {package} timed out",
            )
        except OSError as exc:
            return InstallResult(
                False, tool, "apt", f"Failed to run apt-get: {exc}"
            )

    async def _install_npm(self, tool: str, package: str) -> InstallResult:
        """Install an npm package globally."""
        if not shutil.which("npm"):
            return InstallResult(
                False, tool, "npm", "npm is not installed; cannot install npm packages"
            )

        logger.info("Installing npm package: %s (npm install -g %s)", tool, package)
        try:
            proc = await asyncio.create_subprocess_exec(
                "npm", "install", "-g", package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            if proc.returncode == 0:
                verified = await self._verify_install(tool)
                if verified:
                    return InstallResult(
                        True, tool, "npm",
                        f"Successfully installed {package} via npm",
                    )
                return InstallResult(
                    False, tool, "npm",
                    f"npm install {package} succeeded but verification failed",
                )

            err_text = (stderr or b"").decode().strip()
            return InstallResult(
                False, tool, "npm",
                f"npm install -g {package} failed (exit {proc.returncode}): {err_text[:200]}",
            )
        except asyncio.TimeoutError:
            return InstallResult(
                False, tool, "npm",
                f"npm install -g {package} timed out after 120s",
            )
        except OSError as exc:
            return InstallResult(
                False, tool, "npm", f"Failed to run npm: {exc}"
            )

    async def _verify_install(self, tool: str) -> bool:
        """Verify a tool was installed successfully."""
        # For system commands, check shutil.which
        if shutil.which(tool):
            return True

        # For Python packages, try to import
        install_info = self.INSTALL_MAP.get(tool)
        if install_info and install_info["method"] == "pip":
            return self._is_python_package_available(tool)

        return False

    def get_install_history(self) -> list[InstallResult]:
        """Return history of all installation attempts."""
        return list(self._install_history)

    def suggest_install(self, error_message: str) -> str | None:
        """Given an error message, suggest what tool might be needed.

        E.g., "No module named 'openpyxl'" -> suggests "openpyxl"
              "pandoc: command not found" -> suggests "pandoc"
        """
        for pattern, resolver in self._ERROR_PATTERNS:
            match = pattern.search(error_message)
            if not match:
                continue

            if resolver == "_python_module":
                module_name = match.group(1).split(".")[0]  # top-level package
                # Check direct match in INSTALL_MAP
                if module_name in self.INSTALL_MAP:
                    return module_name
                # Check import-name-to-tool mapping
                if module_name in self._IMPORT_TO_TOOL:
                    return self._IMPORT_TO_TOOL[module_name]
                # Fall back to the module name itself as a guess
                return module_name

            elif resolver == "_command":
                cmd = match.group(1)
                if cmd in self.INSTALL_MAP:
                    return cmd
                return cmd

            elif resolver == "_npm_module":
                module_name = match.group(1).split("/")[0]
                if module_name in self.INSTALL_MAP:
                    return module_name
                return None

            elif resolver == "_command_from_path":
                cmd = match.group(2)
                if cmd in self.INSTALL_MAP:
                    return cmd
                return cmd

        return None
