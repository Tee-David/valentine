# src/valentine/skills/manifest.py
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SkillManifest:
    """Parsed representation of a skill's metadata.

    Can be created from a ``skill.toml`` file (new format) or from a legacy
    ``.sh`` script that contains a ``# DESC:`` comment line.
    """

    name: str
    version: str
    description: str
    author: str = ""
    entrypoint: str = "run.sh"
    dependencies: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)  # JSON-schema for args
    risk_level: str = "low"  # low | medium | high
    # Absolute path to the directory (or file) backing this skill
    source_path: str = ""

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_toml(cls, path: str) -> SkillManifest:
        """Parse a ``skill.toml`` file and return a manifest."""
        path = os.path.realpath(path)
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

        skill = data.get("skill", data)  # accept [skill] table or flat

        name = skill.get("name", "")
        if not name:
            # Fall back to parent directory name
            name = os.path.basename(os.path.dirname(path))

        return cls(
            name=name,
            version=str(skill.get("version", "0.1.0")),
            description=skill.get("description", ""),
            author=skill.get("author", ""),
            entrypoint=skill.get("entrypoint", "run.sh"),
            dependencies=list(skill.get("dependencies", [])),
            parameters=dict(skill.get("parameters", {})),
            risk_level=skill.get("risk_level", "low"),
            source_path=os.path.dirname(path),
        )

    @classmethod
    def from_legacy_script(cls, script_path: str) -> SkillManifest:
        """Create a manifest from a legacy ``.sh`` file.

        Reads the first ``# DESC:`` comment line for the description, matching
        the behaviour in ``CodeSmithAgent._discover_skills``.
        """
        script_path = os.path.realpath(script_path)
        name = os.path.splitext(os.path.basename(script_path))[0]
        description = ""

        try:
            with open(script_path, "r") as fh:
                for line in fh:
                    if line.startswith("# DESC:"):
                        description = line.split("# DESC:", 1)[1].strip()
                        break
        except Exception as exc:
            logger.warning("Could not read legacy script %s: %s", script_path, exc)

        return cls(
            name=name,
            version="0.0.0",
            description=description,
            entrypoint=os.path.basename(script_path),
            source_path=os.path.dirname(script_path),
        )

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entrypoint": self.entrypoint,
            "dependencies": self.dependencies,
            "parameters": self.parameters,
            "risk_level": self.risk_level,
            "source_path": self.source_path,
        }

    def summary_line(self) -> str:
        """One-line summary suitable for embedding in a prompt."""
        if self.description:
            return f"  - {self.name}: {self.description}"
        return f"  - {self.name}"
