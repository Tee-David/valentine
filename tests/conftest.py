# tests/conftest.py
import sys
from unittest.mock import AsyncMock, MagicMock

# Stub optional heavy dependencies so agents can be imported in tests
# without requiring the full runtime stack (Qdrant, DuckDuckGo, etc.)
for _optional_dep in ("mem0", "duckduckgo_search"):
    if _optional_dep not in sys.modules:
        sys.modules[_optional_dep] = MagicMock()

import pytest
from valentine.models import IncomingMessage, ContentType, AgentName, AgentTask, RoutingDecision


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


@pytest.fixture
def mock_llm():
    """Mock LLM that returns configurable responses."""
    llm = AsyncMock()
    llm.provider_name = "groq"
    llm.default_model = "test-model"
    llm.chat_completion = AsyncMock(return_value="Test response")
    llm.stream_chat_completion = AsyncMock()
    llm.image_completion = AsyncMock(return_value="I see an image of a cat.")
    llm.transcribe_audio = AsyncMock(return_value="Hello, this is a test.")
    return llm


@pytest.fixture
def mock_bus():
    """Mock Redis bus."""
    bus = AsyncMock()
    bus.redis = AsyncMock()
    bus.check_health = AsyncMock(return_value=True)
    bus.close = AsyncMock()

    _history = {}

    async def get_history(chat_id, limit=20):
        return list(_history.get(chat_id, []))[-limit:]

    async def append_history(chat_id, role, content):
        _history.setdefault(chat_id, []).append({"role": role, "content": content})

    async def clear_history(chat_id):
        _history.pop(chat_id, None)

    bus.get_history = AsyncMock(side_effect=get_history)
    bus.append_history = AsyncMock(side_effect=append_history)
    bus.clear_history = AsyncMock(side_effect=clear_history)
    bus.stream_name = MagicMock(side_effect=lambda agent, suffix: f"valentine:{agent}:{suffix}")
    bus.ROUTER_STREAM = "valentine:router:task"
    bus.add_task = AsyncMock()
    bus.publish = AsyncMock()
    bus.acknowledge_task = AsyncMock()
    return bus


def make_task_for(agent: AgentName, intent: str = "chat", text: str = "Hello", **msg_kwargs):
    """Helper to create AgentTask objects for testing."""
    msg = IncomingMessage(
        message_id="test-123", chat_id="chat-456", user_id="user-789",
        platform="telegram", content_type=msg_kwargs.pop("content_type", ContentType.TEXT),
        text=text, **msg_kwargs,
    )
    return AgentTask(
        task_id="task-001", agent=agent,
        routing=RoutingDecision(intent=intent, agent=agent),
        message=msg,
    )
