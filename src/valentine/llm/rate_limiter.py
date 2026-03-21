# src/valentine/llm/rate_limiter.py
"""Token-bucket style rate limiter for LLM API providers."""

from __future__ import annotations

import asyncio
import time
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """Enforces per-minute and per-day request limits for an API provider."""

    def __init__(self, provider_name: str, rpm: int, rpd: int):
        self.provider_name = provider_name
        self.rpm = rpm
        self.rpd = rpd

        self._minute_timestamps: list[float] = []
        self._day_timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a request slot is available, then consume one."""
        async with self._lock:
            now = time.monotonic()

            # Prune old timestamps
            cutoff_minute = now - 60
            self._minute_timestamps = [
                t for t in self._minute_timestamps if t > cutoff_minute
            ]
            cutoff_day = now - 86400
            self._day_timestamps = [
                t for t in self._day_timestamps if t > cutoff_day
            ]

            # Wait if at minute limit
            if len(self._minute_timestamps) >= self.rpm:
                wait = self._minute_timestamps[0] - cutoff_minute
                logger.info(
                    f"[{self.provider_name}] RPM limit ({self.rpm}) hit, "
                    f"waiting {wait:.1f}s"
                )
                await asyncio.sleep(wait)

            # Wait if at daily limit
            if len(self._day_timestamps) >= self.rpd:
                wait = self._day_timestamps[0] - cutoff_day
                logger.warning(
                    f"[{self.provider_name}] RPD limit ({self.rpd}) hit, "
                    f"waiting {wait:.1f}s"
                )
                await asyncio.sleep(wait)

            now = time.monotonic()
            self._minute_timestamps.append(now)
            self._day_timestamps.append(now)

    @property
    def remaining_rpm(self) -> int:
        cutoff = time.monotonic() - 60
        active = sum(1 for t in self._minute_timestamps if t > cutoff)
        return max(0, self.rpm - active)

    @property
    def remaining_rpd(self) -> int:
        cutoff = time.monotonic() - 86400
        active = sum(1 for t in self._day_timestamps if t > cutoff)
        return max(0, self.rpd - active)
