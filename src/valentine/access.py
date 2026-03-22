# src/valentine/access.py
"""User access control for Valentine.

Stores allowed users in Redis so admins can grant/revoke access
without redeploying. If no users are explicitly allowed, access
is open (backwards compatible).

Keys:
    valentine:access:users  — Redis SET of allowed Telegram user IDs
    valentine:access:mode   — "open" (anyone) or "restricted" (allowlist only)
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_ACCESS_USERS_KEY = "valentine:access:users"
_ACCESS_MODE_KEY = "valentine:access:mode"


class AccessControl:
    """Manage user access via Redis."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def get_mode(self) -> str:
        """Return 'open' or 'restricted'."""
        mode = await self.redis.get(_ACCESS_MODE_KEY)
        if mode:
            return mode if isinstance(mode, str) else mode.decode()
        return "restricted"

    async def set_mode(self, mode: str) -> None:
        """Set access mode: 'open' or 'restricted'."""
        if mode not in ("open", "restricted"):
            raise ValueError("Mode must be 'open' or 'restricted'")
        await self.redis.set(_ACCESS_MODE_KEY, mode)

    async def is_allowed(self, user_id: str, is_admin: bool = False) -> bool:
        """Check if a user is allowed to use the bot."""
        # Admins always have access
        if is_admin:
            return True

        mode = await self.get_mode()
        if mode == "open":
            return True

        # Restricted mode: check allowlist
        return await self.redis.sismember(_ACCESS_USERS_KEY, str(user_id))

    async def allow_user(self, user_id: str, user_name: Optional[str] = None) -> bool:
        """Add a user to the allowlist. Returns True if newly added."""
        added = await self.redis.sadd(_ACCESS_USERS_KEY, str(user_id))
        if user_name:
            # Store name for display purposes
            await self.redis.hset("valentine:access:names", str(user_id), user_name)
        return bool(added)

    async def revoke_user(self, user_id: str) -> bool:
        """Remove a user from the allowlist. Returns True if was present."""
        removed = await self.redis.srem(_ACCESS_USERS_KEY, str(user_id))
        await self.redis.hdel("valentine:access:names", str(user_id))
        return bool(removed)

    async def list_users(self) -> list[dict]:
        """Return list of allowed users with their names."""
        user_ids = await self.redis.smembers(_ACCESS_USERS_KEY)
        names = await self.redis.hgetall("valentine:access:names") or {}
        # Decode bytes if needed
        result = []
        for uid in user_ids:
            uid_str = uid if isinstance(uid, str) else uid.decode()
            name = names.get(uid_str) or names.get(uid) or "Unknown"
            if isinstance(name, bytes):
                name = name.decode()
            result.append({"user_id": uid_str, "name": name})
        return sorted(result, key=lambda x: x["name"])

    async def user_count(self) -> int:
        """Return number of allowed users."""
        return await self.redis.scard(_ACCESS_USERS_KEY)
