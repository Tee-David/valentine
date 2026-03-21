# src/valentine/nexus/adapter.py
"""Platform adapter abstraction — one adapter per chat platform (Telegram, Discord, etc.)."""

import abc
from typing import Any

from valentine.models import IncomingMessage, TaskResult


class PlatformAdapter(abc.ABC):
    """ABC that every chat-platform integration must implement.

    Responsibilities:
    - Receive raw platform events and normalise them into IncomingMessage
    - Send TaskResult payloads back to users in the platform's native format
    - Manage platform-specific concerns (rate limits, typing indicators, media downloads)
    """

    @property
    @abc.abstractmethod
    def platform_name(self) -> str:
        """Unique slug for the platform (e.g. 'telegram', 'discord')."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def start(self) -> None:
        """Connect to the platform and begin receiving events."""
        ...

    @abc.abstractmethod
    async def stop(self) -> None:
        """Gracefully disconnect from the platform."""
        ...

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def send_result(self, result: TaskResult) -> None:
        """Deliver a TaskResult to the originating user/chat."""
        ...

    @abc.abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Show a typing / processing indicator in the chat."""
        ...

    # ------------------------------------------------------------------
    # Media
    # ------------------------------------------------------------------

    @abc.abstractmethod
    async def download_media(self, file_ref: Any) -> str:
        """Download a media attachment and return the local file path."""
        ...
