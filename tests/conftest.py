# tests/conftest.py
import sys
from unittest.mock import MagicMock

# Stub optional heavy dependencies so agents can be imported in tests
# without requiring the full runtime stack (Qdrant, DuckDuckGo, etc.)
for _optional_dep in ("mem0", "duckduckgo_search"):
    if _optional_dep not in sys.modules:
        sys.modules[_optional_dep] = MagicMock()

import pytest
from valentine.models import IncomingMessage, ContentType


@pytest.fixture
def sample_text_message() -> IncomingMessage:
    return IncomingMessage(
        message_id="123",
        chat_id="456",
        user_id="789",
        platform="telegram",
        content_type=ContentType.TEXT,
        text="Hello Valentine",
    )


@pytest.fixture
def sample_photo_message() -> IncomingMessage:
    return IncomingMessage(
        message_id="124",
        chat_id="456",
        user_id="789",
        platform="telegram",
        content_type=ContentType.PHOTO,
        media_path="/tmp/photo.jpg",
    )


@pytest.fixture
def sample_voice_message() -> IncomingMessage:
    return IncomingMessage(
        message_id="125",
        chat_id="456",
        user_id="789",
        platform="telegram",
        content_type=ContentType.VOICE,
        media_path="/tmp/voice.ogg",
    )
