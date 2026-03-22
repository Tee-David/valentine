# src/valentine/autonomy.py
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from enum import Enum

import redis.asyncio as aioredis

from valentine.config import settings

logger = logging.getLogger(__name__)


class AutonomyMode(str, Enum):
    SUPERVISED = "supervised"  # Asks approval for dangerous actions
    FULL = "full"              # Executes everything autonomously
    READONLY = "readonly"      # Only read operations allowed


class RiskLevel(str, Enum):
    LOW = "low"        # Read file, list directory, search
    MEDIUM = "medium"  # Write file, install package, git commit
    HIGH = "high"      # Shell exec, git push, deploy, delete, system commands


class AutonomyGate:
    """Controls action execution based on autonomy mode and risk level.

    In SUPERVISED mode:
    - LOW risk: auto-approve
    - MEDIUM risk: auto-approve (but log)
    - HIGH risk: queue for user approval via Telegram

    In FULL mode:
    - All actions auto-approved

    In READONLY mode:
    - Only LOW risk actions allowed
    - Everything else blocked
    """

    RISK_MAP: dict[str, RiskLevel] = {
        # Low risk
        "read": RiskLevel.LOW,
        "search": RiskLevel.LOW,
        "list": RiskLevel.LOW,
        "skill_list": RiskLevel.LOW,
        "web_search": RiskLevel.LOW,
        # Medium risk
        "write": RiskLevel.MEDIUM,
        "skill_install": RiskLevel.MEDIUM,
        "git_commit": RiskLevel.MEDIUM,
        "npm_install": RiskLevel.MEDIUM,
        # High risk
        "shell": RiskLevel.HIGH,
        "git_push": RiskLevel.HIGH,
        "deploy": RiskLevel.HIGH,
        "delete": RiskLevel.HIGH,
        "skill_uninstall": RiskLevel.HIGH,
    }

    # Redis keys for the approval queue
    APPROVAL_QUEUE = "valentine:approvals:pending"
    APPROVAL_RESULT = "valentine:approvals:result:{call_id}"

    def __init__(self, mode: AutonomyMode | None = None, bus=None):
        self.mode = mode or AutonomyMode(settings.autonomy_mode)
        self.bus = bus  # RedisBus instance for approval requests

    def classify_risk(self, action: str, command: str = "") -> RiskLevel:
        """Classify the risk level of an action.

        Args:
            action: The action type (shell, write, read, etc.)
            command: The specific command string (for shell actions).
        """
        # Direct lookup first
        if action in self.RISK_MAP:
            base_risk = self.RISK_MAP[action]
        else:
            # Unknown actions default to HIGH in supervised/readonly
            logger.warning("Unknown action %r – defaulting to HIGH risk", action)
            base_risk = RiskLevel.HIGH

        # For shell actions, escalate to HIGH if the command matches a
        # dangerous-command pattern from settings.
        if action == "shell" and command:
            cmd_lower = command.strip().lower()
            for dangerous in settings.autonomy_dangerous_commands:
                if cmd_lower.startswith(dangerous):
                    return RiskLevel.HIGH
        return base_risk

    async def check(
        self,
        action: str,
        command: str = "",
        chat_id: str = "",
        call_id: str = "",
    ) -> tuple[bool, str]:
        """Check if an action is allowed under current autonomy mode.

        Returns:
            (approved, reason) – a bool and a human-readable explanation.
        """
        risk = self.classify_risk(action, command)

        if self.mode == AutonomyMode.FULL:
            logger.debug("FULL autonomy – auto-approving %s (risk=%s)", action, risk.value)
            return True, "full autonomy"

        if self.mode == AutonomyMode.READONLY:
            if risk == RiskLevel.LOW:
                return True, "read-only: low risk allowed"
            logger.info("READONLY mode blocked %s (risk=%s)", action, risk.value)
            return False, f"read-only mode: {action} blocked (risk: {risk.value})"

        # --- SUPERVISED mode ---
        if risk == RiskLevel.LOW:
            return True, "supervised: low risk auto-approved"

        if risk == RiskLevel.MEDIUM:
            logger.info("SUPERVISED auto-approving medium-risk action: %s", action)
            return True, "supervised: medium risk auto-approved"

        # HIGH risk -> request user approval
        if self.bus and chat_id:
            approved = await self._request_approval(action, command, chat_id, call_id)
            if approved:
                return True, "user approved"
            return False, "user denied"

        # No bus or chat_id available – block by default
        return False, "supervised: high risk blocked (no approval channel)"

    async def _request_approval(
        self,
        action: str,
        command: str,
        chat_id: str,
        call_id: str,
    ) -> bool:
        """Queue an approval request and wait for user response.

        Publishes a request to the ``valentine:approvals:request`` channel.
        The Telegram adapter picks it up, shows inline buttons, and pushes
        the decision into a Redis list keyed by *call_id*.

        Timeout: 60 seconds.  Defaults to **denied** if no response.
        """
        if not call_id:
            call_id = str(uuid.uuid4())

        request = {
            "call_id": call_id,
            "action": action,
            "command": command,
            "chat_id": chat_id,
        }

        logger.info(
            "Requesting user approval for %s (call_id=%s, chat=%s)",
            action,
            call_id,
            chat_id,
        )

        # Publish so the Telegram adapter can display the prompt
        await self.bus.publish("valentine:approvals:request", request)

        # Wait for the decision on a per-request Redis list
        result_key = self.APPROVAL_RESULT.format(call_id=call_id)
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            result = await r.blpop(result_key, timeout=60)
            if result:
                data = json.loads(result[1])
                approved = data.get("approved", False)
                logger.info("Approval result for %s: %s", call_id, approved)
                return approved
            logger.info("Approval timed out for %s – denying", call_id)
            return False  # timeout -> denied
        except asyncio.CancelledError:
            logger.debug("Approval wait cancelled for %s", call_id)
            return False
        except Exception:
            logger.exception("Error waiting for approval %s", call_id)
            return False
        finally:
            await r.aclose()

    @staticmethod
    async def submit_approval(bus, call_id: str, approved: bool) -> None:
        """Submit an approval decision (called by the Telegram adapter).

        Pushes the result into the per-request Redis list so the waiting
        ``_request_approval`` call can pick it up.

        Args:
            bus: RedisBus instance (unused directly – we open our own
                 short-lived connection to guarantee ``decode_responses``).
            call_id: The approval request identifier.
            approved: Whether the user approved the action.
        """
        result_key = f"valentine:approvals:result:{call_id}"
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await r.rpush(result_key, json.dumps({"approved": approved}))
            await r.expire(result_key, 120)  # auto-cleanup after 2 min
            logger.info("Submitted approval for %s: %s", call_id, approved)
        finally:
            await r.aclose()
