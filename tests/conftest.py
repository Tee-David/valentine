# tests/conftest.py
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
