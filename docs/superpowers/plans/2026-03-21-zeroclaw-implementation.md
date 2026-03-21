# ZeroClaw Multi-Agent System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Valentine v2 — a multi-agent AI assistant with ZeroClaw orchestrator, 5 specialized sub-agents, Redis message bus, and Telegram interface. All LLM inference via free APIs (Groq, Cerebras, SambaNova).

**Architecture:** Microservice sub-agents as independent async Python processes communicating via Redis Streams/pub-sub. ZeroClaw routes intent to specialized agents (CodeSmith, Oracle, Iris, Echo, Cortex). Nexus handles Telegram I/O. Memory via Mem0 + Qdrant.

**Tech Stack:** Python 3.11+, asyncio, httpx, redis[hiredis], python-telegram-bot, pydantic, mem0ai, qdrant-client, openai-whisper, edge-tts, duckduckgo-search

**Spec:** `docs/superpowers/specs/2026-03-21-zeroclaw-architecture-design.md`

---

## Task 1: Project Scaffold & Configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/valentine/__init__.py`
- Create: `src/valentine/config.py`
- Create: `src/valentine/models.py`
- Create: `src/valentine/utils.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Create pyproject.toml with all dependencies**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "valentine"
version = "2.0.0"
description = "ZeroClaw multi-agent AI assistant"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "redis[hiredis]>=5.0",
    "python-telegram-bot>=21.0",
    "pydantic>=2.0",
    "pydantic-settings>=2.0",
    "duckduckgo-search>=6.0",
    "edge-tts>=6.1",
    "qdrant-client>=1.9",
    "sentence-transformers>=3.0",
    "openai-whisper>=20231117",
    "pydub>=0.25",
    "python-dotenv>=1.0",
    "structlog>=24.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.14",
    "ruff>=0.5",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
target-version = "py311"
line-length = 120
```

- [ ] **Step 2: Create package structure directories**

Run:
```bash
mkdir -p src/valentine/llm src/valentine/bus src/valentine/orchestrator src/valentine/agents src/valentine/nexus tests workspace scripts
touch src/valentine/__init__.py src/valentine/llm/__init__.py src/valentine/bus/__init__.py src/valentine/orchestrator/__init__.py src/valentine/agents/__init__.py src/valentine/nexus/__init__.py tests/__init__.py
```

- [ ] **Step 3: Create config.py with all settings**

```python
# src/valentine/config.py
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # API Keys
    groq_api_key: str = Field(default="")
    cerebras_api_key: str = Field(default="")
    sambanova_api_key: str = Field(default="")
    telegram_bot_token: str = Field(default="")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Qdrant
    qdrant_host: str = Field(default="localhost")
    qdrant_port: int = Field(default=6333)

    # Model mappings per provider
    groq_base_url: str = "https://api.groq.com/openai/v1"
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    sambanova_base_url: str = "https://api.sambanova.ai/v1"

    # Default models per provider
    groq_default_model: str = "llama-3.1-8b-instant"
    groq_reasoning_model: str = "qwen-qwq-32b"
    groq_whisper_model: str = "whisper-large-v3-turbo"
    cerebras_default_model: str = "qwen-3-32b"
    sambanova_default_model: str = "QwQ-32B"
    sambanova_vision_model: str = "Qwen2.5-VL-72B"

    # Agent config
    workspace_dir: str = Field(default="/tmp/valentine/workspace")
    max_shell_timeout: int = Field(default=30)
    allowed_shell_dirs: list[str] = Field(default_factory=lambda: ["/tmp/valentine/workspace"])

    # Rate limits (requests per minute)
    groq_rpm: int = 30
    cerebras_rpm: int = 30
    sambanova_rpm: int = 20

    # Rate limits (requests per day)
    groq_rpd: int = 14400
    cerebras_rpd: int = 1000
    sambanova_rpd: int = 10000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
```

- [ ] **Step 4: Create models.py with all shared data models**

```python
# src/valentine/models.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ContentType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    DOCUMENT = "document"
    VIDEO = "video"


class AgentName(str, Enum):
    CODESMITH = "codesmith"
    ORACLE = "oracle"
    IRIS = "iris"
    ECHO = "echo"
    CORTEX = "cortex"


class Priority(str, Enum):
    NORMAL = "normal"
    URGENT = "urgent"


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    user_id: str
    platform: str
    content_type: ContentType
    text: str | None = None
    media_path: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "platform": self.platform,
            "content_type": self.content_type.value,
            "text": self.text,
            "media_path": self.media_path,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> IncomingMessage:
        return cls(
            message_id=data["message_id"],
            chat_id=data["chat_id"],
            user_id=data["user_id"],
            platform=data["platform"],
            content_type=ContentType(data["content_type"]),
            text=data.get("text"),
            media_path=data.get("media_path"),
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


@dataclass
class RoutingDecision:
    intent: str
    agent: AgentName
    priority: Priority = Priority.NORMAL
    chain: list[AgentName] | None = None
    params: dict = field(default_factory=dict)
    memory_context: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "agent": self.agent.value,
            "priority": self.priority.value,
            "chain": [a.value for a in self.chain] if self.chain else None,
            "params": self.params,
            "memory_context": self.memory_context,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RoutingDecision:
        return cls(
            intent=data["intent"],
            agent=AgentName(data["agent"]),
            priority=Priority(data.get("priority", "normal")),
            chain=[AgentName(a) for a in data["chain"]] if data.get("chain") else None,
            params=data.get("params", {}),
            memory_context=data.get("memory_context", []),
        )


@dataclass
class AgentTask:
    task_id: str
    agent: AgentName
    routing: RoutingDecision
    message: IncomingMessage
    previous_results: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.task_id:
            self.task_id = str(uuid.uuid4())

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent.value,
            "routing": self.routing.to_dict(),
            "message": self.message.to_dict(),
            "previous_results": self.previous_results,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AgentTask:
        return cls(
            task_id=data["task_id"],
            agent=AgentName(data["agent"]),
            routing=RoutingDecision.from_dict(data["routing"]),
            message=IncomingMessage.from_dict(data["message"]),
            previous_results=data.get("previous_results", []),
        )


@dataclass
class TaskResult:
    task_id: str
    agent: AgentName
    success: bool
    content_type: ContentType = ContentType.TEXT
    text: str | None = None
    media_path: str | None = None
    error: str | None = None
    processing_time_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent.value,
            "success": self.success,
            "content_type": self.content_type.value,
            "text": self.text,
            "media_path": self.media_path,
            "error": self.error,
            "processing_time_ms": self.processing_time_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TaskResult:
        return cls(
            task_id=data["task_id"],
            agent=AgentName(data["agent"]),
            success=data["success"],
            content_type=ContentType(data.get("content_type", "text")),
            text=data.get("text"),
            media_path=data.get("media_path"),
            error=data.get("error"),
            processing_time_ms=data.get("processing_time_ms", 0),
        )
```

- [ ] **Step 5: Create utils.py with shared utilities**

```python
# src/valentine/utils.py
import structlog


def setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
```

- [ ] **Step 6: Create conftest.py with shared test fixtures**

```python
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
```

- [ ] **Step 7: Write tests for models serialization**

```python
# tests/test_models.py
from valentine.models import (
    IncomingMessage, RoutingDecision, AgentTask, TaskResult,
    ContentType, AgentName, Priority,
)


class TestIncomingMessage:
    def test_to_dict_roundtrip(self, sample_text_message):
        data = sample_text_message.to_dict()
        restored = IncomingMessage.from_dict(data)
        assert restored.message_id == sample_text_message.message_id
        assert restored.content_type == ContentType.TEXT
        assert restored.text == "Hello Valentine"

    def test_from_dict_with_media(self, sample_photo_message):
        data = sample_photo_message.to_dict()
        restored = IncomingMessage.from_dict(data)
        assert restored.content_type == ContentType.PHOTO
        assert restored.media_path == "/tmp/photo.jpg"


class TestRoutingDecision:
    def test_to_dict_roundtrip(self):
        rd = RoutingDecision(
            intent="code_generation",
            agent=AgentName.CODESMITH,
            chain=[AgentName.IRIS, AgentName.CODESMITH],
            params={"language": "python"},
            memory_context=["user likes concise code"],
        )
        data = rd.to_dict()
        restored = RoutingDecision.from_dict(data)
        assert restored.agent == AgentName.CODESMITH
        assert restored.chain == [AgentName.IRIS, AgentName.CODESMITH]
        assert restored.memory_context == ["user likes concise code"]


class TestAgentTask:
    def test_auto_generates_task_id(self, sample_text_message):
        rd = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="", agent=AgentName.ORACLE, routing=rd, message=sample_text_message)
        assert task.task_id != ""

    def test_to_dict_roundtrip(self, sample_text_message):
        rd = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="t1", agent=AgentName.ORACLE, routing=rd, message=sample_text_message)
        data = task.to_dict()
        restored = AgentTask.from_dict(data)
        assert restored.task_id == "t1"
        assert restored.agent == AgentName.ORACLE


class TestTaskResult:
    def test_success_result(self):
        result = TaskResult(
            task_id="t1", agent=AgentName.ORACLE, success=True,
            text="Here is my answer", processing_time_ms=150,
        )
        data = result.to_dict()
        restored = TaskResult.from_dict(data)
        assert restored.success is True
        assert restored.text == "Here is my answer"

    def test_error_result(self):
        result = TaskResult(
            task_id="t2", agent=AgentName.CODESMITH, success=False,
            error="API rate limited", processing_time_ms=50,
        )
        assert result.error == "API rate limited"
        assert result.text is None
```

- [ ] **Step 8: Run tests to verify scaffold**

Run: `cd /home/teedavid/Desktop/Projects/valentine && pip install -e ".[dev]" && pytest tests/test_models.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit scaffold**

```bash
git init
git add pyproject.toml src/ tests/ workspace/.gitkeep
git commit -m "feat: project scaffold with config, models, and utils"
```

---

## Task 2: LLM Provider Abstraction & Groq Client

**Files:**
- Create: `src/valentine/llm/provider.py`
- Create: `src/valentine/llm/groq.py`
- Create: `src/valentine/llm/quota.py`
- Create: `tests/test_llm_provider.py`

- [ ] **Step 1: Write failing test for LLM provider**

```python
# tests/test_llm_provider.py
import pytest
from unittest.mock import AsyncMock, patch
from valentine.llm.provider import LLMProvider, LLMResponse, FallbackChain
from valentine.llm.groq import GroqClient


class TestLLMResponse:
    def test_create_response(self):
        resp = LLMResponse(text="hello", model="llama-3.1-8b", provider="groq", usage={"tokens": 10})
        assert resp.text == "hello"
        assert resp.provider == "groq"


class TestGroqClient:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        client = GroqClient(api_key="test-key")
        # Mock the httpx call
        mock_response = {
            "choices": [{"message": {"content": "Hello!"}}],
            "model": "llama-3.1-8b-instant",
            "usage": {"total_tokens": 15},
        }
        with patch.object(client, "_post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.chat(
                messages=[{"role": "user", "content": "Hi"}],
                model="llama-3.1-8b-instant",
            )
        assert result.text == "Hello!"
        assert result.provider == "groq"

    @pytest.mark.asyncio
    async def test_chat_with_system_prompt(self):
        client = GroqClient(api_key="test-key")
        mock_response = {
            "choices": [{"message": {"content": "I am Valentine"}}],
            "model": "llama-3.1-8b-instant",
            "usage": {"total_tokens": 20},
        }
        with patch.object(client, "_post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.chat(
                messages=[
                    {"role": "system", "content": "You are Valentine"},
                    {"role": "user", "content": "Who are you?"},
                ],
                model="llama-3.1-8b-instant",
            )
        assert result.text == "I am Valentine"


class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_uses_primary_when_available(self):
        primary = AsyncMock()
        primary.chat.return_value = LLMResponse(text="ok", model="m", provider="groq", usage={})
        chain = FallbackChain(providers=[primary])
        result = await chain.chat(messages=[{"role": "user", "content": "test"}], model="m")
        assert result.text == "ok"
        primary.chat.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_on_primary_failure(self):
        primary = AsyncMock()
        primary.chat.side_effect = Exception("rate limited")
        secondary = AsyncMock()
        secondary.chat.return_value = LLMResponse(text="fallback", model="m", provider="cerebras", usage={})
        chain = FallbackChain(providers=[primary, secondary])
        result = await chain.chat(messages=[{"role": "user", "content": "test"}], model="m")
        assert result.text == "fallback"
        assert result.provider == "cerebras"

    @pytest.mark.asyncio
    async def test_raises_when_all_fail(self):
        p1 = AsyncMock()
        p1.chat.side_effect = Exception("fail1")
        p2 = AsyncMock()
        p2.chat.side_effect = Exception("fail2")
        chain = FallbackChain(providers=[p1, p2])
        with pytest.raises(Exception, match="All LLM providers failed"):
            await chain.chat(messages=[{"role": "user", "content": "test"}], model="m")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_llm_provider.py -v`
Expected: FAIL — modules don't exist yet

- [ ] **Step 3: Implement LLMProvider base and LLMResponse**

```python
# src/valentine/llm/provider.py
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from valentine.utils import get_logger

log = get_logger("llm.provider")


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict = field(default_factory=dict)


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class FallbackChain:
    def __init__(self, providers: list[LLMProvider]) -> None:
        self.providers = providers

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse:
        errors = []
        for provider in self.providers:
            try:
                return await provider.chat(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except Exception as e:
                log.warning("provider_failed", provider=getattr(provider, "name", "unknown"), error=str(e))
                errors.append(e)
        raise Exception(f"All LLM providers failed: {errors}")

    async def close(self) -> None:
        for p in self.providers:
            await p.close()
```

- [ ] **Step 4: Implement Groq client**

```python
# src/valentine/llm/groq.py
from __future__ import annotations

import httpx

from valentine.llm.provider import LLMProvider, LLMResponse
from valentine.utils import get_logger

log = get_logger("llm.groq")


class GroqClient(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, base_url: str = "https://api.groq.com/openai/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60.0,
        )

    async def _post(self, path: str, json: dict) -> dict:
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        data = await self._post("/chat/completions", payload)
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(
            text=text,
            model=data.get("model", model),
            provider=self.name,
            usage=data.get("usage", {}),
        )

    async def transcribe(self, audio_path: str, model: str = "whisper-large-v3-turbo") -> str:
        """Transcribe audio using Groq Whisper API."""
        with open(audio_path, "rb") as f:
            resp = await self._client.post(
                "/audio/transcriptions",
                data={"model": model},
                files={"file": ("audio.wav", f, "audio/wav")},
            )
        resp.raise_for_status()
        return resp.json()["text"]

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_llm_provider.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/valentine/llm/ tests/test_llm_provider.py
git commit -m "feat: LLM provider abstraction with Groq client and fallback chain"
```

---

## Task 3: Cerebras & SambaNova Clients + QuotaTracker

**Files:**
- Create: `src/valentine/llm/cerebras.py`
- Create: `src/valentine/llm/sambanova.py`
- Create: `src/valentine/llm/quota.py`
- Modify: `tests/test_llm_provider.py`

- [ ] **Step 1: Write tests for Cerebras, SambaNova, and QuotaTracker**

Append to `tests/test_llm_provider.py`:

```python
from valentine.llm.cerebras import CerebrasClient
from valentine.llm.sambanova import SambaNovaClient
from valentine.llm.quota import QuotaTracker


class TestCerebrasClient:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        client = CerebrasClient(api_key="test-key")
        mock_response = {
            "choices": [{"message": {"content": "Cerebras reply"}}],
            "model": "qwen-3-32b",
            "usage": {"total_tokens": 25},
        }
        with patch.object(client, "_post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.chat(messages=[{"role": "user", "content": "Hi"}], model="qwen-3-32b")
        assert result.text == "Cerebras reply"
        assert result.provider == "cerebras"


class TestSambaNovaClient:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self):
        client = SambaNovaClient(api_key="test-key")
        mock_response = {
            "choices": [{"message": {"content": "SambaNova reply"}}],
            "model": "QwQ-32B",
            "usage": {"total_tokens": 30},
        }
        with patch.object(client, "_post", new_callable=AsyncMock, return_value=mock_response):
            result = await client.chat(messages=[{"role": "user", "content": "Hi"}], model="QwQ-32B")
        assert result.text == "SambaNova reply"
        assert result.provider == "sambanova"


class TestQuotaTracker:
    @pytest.mark.asyncio
    async def test_record_and_check_under_limit(self):
        tracker = QuotaTracker()
        tracker.record_request("groq")
        assert tracker.is_available("groq", rpm_limit=30, rpd_limit=14400) is True

    @pytest.mark.asyncio
    async def test_over_rpm_limit(self):
        tracker = QuotaTracker()
        for _ in range(31):
            tracker.record_request("groq")
        assert tracker.is_available("groq", rpm_limit=30, rpd_limit=14400) is False

    @pytest.mark.asyncio
    async def test_preemptive_threshold(self):
        tracker = QuotaTracker()
        for _ in range(25):  # 25/30 = 83% > 80% threshold
            tracker.record_request("groq")
        assert tracker.should_preempt("groq", rpm_limit=30, threshold=0.8) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_llm_provider.py -v`
Expected: FAIL — new modules don't exist

- [ ] **Step 3: Implement Cerebras client**

```python
# src/valentine/llm/cerebras.py
from __future__ import annotations

import httpx

from valentine.llm.provider import LLMProvider, LLMResponse
from valentine.utils import get_logger

log = get_logger("llm.cerebras")


class CerebrasClient(LLMProvider):
    name = "cerebras"

    def __init__(self, api_key: str, base_url: str = "https://api.cerebras.ai/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60.0,
        )

    async def _post(self, path: str, json: dict) -> dict:
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        data = await self._post("/chat/completions", payload)
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(
            text=text,
            model=data.get("model", model),
            provider=self.name,
            usage=data.get("usage", {}),
        )

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Implement SambaNova client**

```python
# src/valentine/llm/sambanova.py
from __future__ import annotations

import base64
import httpx

from valentine.llm.provider import LLMProvider, LLMResponse
from valentine.utils import get_logger

log = get_logger("llm.sambanova")


class SambaNovaClient(LLMProvider):
    name = "sambanova"

    def __init__(self, api_key: str, base_url: str = "https://api.sambanova.ai/v1") -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60.0,
        )

    async def _post(self, path: str, json: dict) -> dict:
        resp = await self._client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        data = await self._post("/chat/completions", payload)
        text = data["choices"][0]["message"]["content"]
        return LLMResponse(
            text=text,
            model=data.get("model", model),
            provider=self.name,
            usage=data.get("usage", {}),
        )

    async def chat_with_image(
        self,
        text: str,
        image_path: str,
        model: str,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Send text + image to a multimodal model."""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": text or "Describe this image in detail."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        })

        return await self.chat(messages=messages, model=model)

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 5: Implement QuotaTracker**

```python
# src/valentine/llm/quota.py
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from valentine.utils import get_logger

log = get_logger("llm.quota")


@dataclass
class ProviderQuota:
    minute_window: deque = field(default_factory=deque)
    day_window: deque = field(default_factory=deque)


class QuotaTracker:
    """Tracks API request rates per provider using sliding windows."""

    def __init__(self) -> None:
        self._quotas: dict[str, ProviderQuota] = {}

    def _get_quota(self, provider: str) -> ProviderQuota:
        if provider not in self._quotas:
            self._quotas[provider] = ProviderQuota()
        return self._quotas[provider]

    def _clean_window(self, window: deque, max_age: float) -> None:
        now = time.monotonic()
        while window and (now - window[0]) > max_age:
            window.popleft()

    def record_request(self, provider: str) -> None:
        quota = self._get_quota(provider)
        now = time.monotonic()
        quota.minute_window.append(now)
        quota.day_window.append(now)

    def get_rpm(self, provider: str) -> int:
        quota = self._get_quota(provider)
        self._clean_window(quota.minute_window, 60.0)
        return len(quota.minute_window)

    def get_rpd(self, provider: str) -> int:
        quota = self._get_quota(provider)
        self._clean_window(quota.day_window, 86400.0)
        return len(quota.day_window)

    def is_available(self, provider: str, rpm_limit: int, rpd_limit: int) -> bool:
        return self.get_rpm(provider) < rpm_limit and self.get_rpd(provider) < rpd_limit

    def should_preempt(self, provider: str, rpm_limit: int, threshold: float = 0.8) -> bool:
        """Return True if provider is above threshold of its RPM limit."""
        return self.get_rpm(provider) >= int(rpm_limit * threshold)
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/test_llm_provider.py -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/valentine/llm/ tests/test_llm_provider.py
git commit -m "feat: Cerebras, SambaNova clients and QuotaTracker"
```

---

## Task 4: Redis Message Bus

**Files:**
- Create: `src/valentine/bus/redis_bus.py`
- Create: `tests/test_redis_bus.py`

- [ ] **Step 1: Write failing tests for Redis bus**

```python
# tests/test_redis_bus.py
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from valentine.bus.redis_bus import RedisBus


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1234-0")
    redis.xread = AsyncMock(return_value=[])
    redis.xack = AsyncMock()
    redis.xgroup_create = AsyncMock()
    redis.publish = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.close = AsyncMock()
    return redis


class TestRedisBus:
    @pytest.mark.asyncio
    async def test_publish_to_stream(self, mock_redis):
        bus = RedisBus.__new__(RedisBus)
        bus._redis = mock_redis
        bus._log = MagicMock()

        await bus.publish_to_stream("stream:test", {"key": "value"})
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "stream:test"

    @pytest.mark.asyncio
    async def test_publish_pubsub(self, mock_redis):
        bus = RedisBus.__new__(RedisBus)
        bus._redis = mock_redis
        bus._log = MagicMock()

        await bus.publish_pubsub("pubsub:nexus.respond", {"text": "hello"})
        mock_redis.publish.assert_called_once()
        call_args = mock_redis.publish.call_args
        assert call_args[0][0] == "pubsub:nexus.respond"

    @pytest.mark.asyncio
    async def test_health_check(self, mock_redis):
        bus = RedisBus.__new__(RedisBus)
        bus._redis = mock_redis
        bus._log = MagicMock()

        result = await bus.health_check()
        assert result is True
        mock_redis.ping.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_redis_bus.py -v`
Expected: FAIL — module doesn't exist

- [ ] **Step 3: Implement RedisBus**

```python
# src/valentine/bus/redis_bus.py
from __future__ import annotations

import json
from typing import AsyncIterator

import redis.asyncio as aioredis

from valentine.utils import get_logger


class RedisBus:
    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self._url = url
        self._redis: aioredis.Redis | None = None
        self._log = get_logger("bus.redis")

    async def connect(self) -> None:
        self._redis = aioredis.from_url(self._url, decode_responses=True)
        await self._redis.ping()
        self._log.info("redis_connected", url=self._url)

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._log.info("redis_disconnected")

    async def health_check(self) -> bool:
        try:
            return await self._redis.ping()
        except Exception:
            return False

    # --- Streams (reliable, persistent) ---

    async def publish_to_stream(self, stream: str, data: dict) -> str:
        """Add a message to a Redis Stream. Returns the message ID."""
        payload = {"data": json.dumps(data)}
        msg_id = await self._redis.xadd(stream, payload)
        self._log.debug("stream_publish", stream=stream, msg_id=msg_id)
        return msg_id

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        """Create a consumer group on a stream, ignore if it already exists."""
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def read_from_stream(
        self, stream: str, group: str, consumer: str, count: int = 1, block: int = 5000
    ) -> list[tuple[str, dict]]:
        """Read messages from a stream as a consumer group member.
        Returns list of (message_id, data) tuples.
        """
        results = await self._redis.xreadgroup(
            groupname=group, consumername=consumer,
            streams={stream: ">"}, count=count, block=block,
        )
        messages = []
        if results:
            for _stream_name, entries in results:
                for msg_id, fields in entries:
                    data = json.loads(fields["data"])
                    messages.append((msg_id, data))
        return messages

    async def ack_message(self, stream: str, group: str, msg_id: str) -> None:
        """Acknowledge a message in a consumer group."""
        await self._redis.xack(stream, group, msg_id)

    # --- Pub/Sub (fire-and-forget) ---

    async def publish_pubsub(self, channel: str, data: dict) -> None:
        """Publish a message to a pub/sub channel."""
        await self._redis.publish(channel, json.dumps(data))
        self._log.debug("pubsub_publish", channel=channel)

    async def subscribe(self, channel: str) -> AsyncIterator[dict]:
        """Subscribe to a pub/sub channel and yield messages."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield json.loads(message["data"])
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_redis_bus.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/bus/ tests/test_redis_bus.py
git commit -m "feat: Redis message bus with Streams and pub/sub"
```

---

## Task 5: Base Agent Framework

**Files:**
- Create: `src/valentine/agents/base.py`
- Create: `tests/test_base_agent.py`

- [ ] **Step 1: Write failing test for BaseAgent**

```python
# tests/test_base_agent.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from valentine.agents.base import BaseAgent
from valentine.models import AgentTask, AgentName, RoutingDecision, IncomingMessage, ContentType, TaskResult


class ConcreteAgent(BaseAgent):
    """Test implementation of BaseAgent."""
    name = AgentName.ORACLE

    async def process(self, task: AgentTask) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            agent=self.name,
            success=True,
            text="test response",
        )


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish_to_stream = AsyncMock()
    bus.publish_pubsub = AsyncMock()
    bus.ensure_consumer_group = AsyncMock()
    bus.read_from_stream = AsyncMock(return_value=[])
    bus.ack_message = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    return llm


class TestBaseAgent:
    def test_stream_names(self):
        agent = ConcreteAgent.__new__(ConcreteAgent)
        agent.name = AgentName.ORACLE
        assert agent.task_stream == "stream:agent.oracle.task"
        assert agent.result_stream == "stream:agent.oracle.result"

    @pytest.mark.asyncio
    async def test_handle_task_publishes_result(self, mock_bus, mock_llm):
        agent = ConcreteAgent(bus=mock_bus, llm=mock_llm)
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text="test",
        )
        routing = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="t1", agent=AgentName.ORACLE, routing=routing, message=msg)

        await agent.handle_task(task)
        mock_bus.publish_to_stream.assert_called_once()
        call_data = mock_bus.publish_to_stream.call_args[0]
        assert call_data[0] == "stream:agent.oracle.result"

    @pytest.mark.asyncio
    async def test_handle_task_catches_errors(self, mock_bus, mock_llm):
        class FailingAgent(BaseAgent):
            name = AgentName.ORACLE
            async def process(self, task):
                raise ValueError("something broke")

        agent = FailingAgent(bus=mock_bus, llm=mock_llm)
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text="test",
        )
        routing = RoutingDecision(intent="chat", agent=AgentName.ORACLE)
        task = AgentTask(task_id="t2", agent=AgentName.ORACLE, routing=routing, message=msg)

        await agent.handle_task(task)
        call_data = mock_bus.publish_to_stream.call_args[0]
        assert call_data[0] == "stream:agent.oracle.result"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_base_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Implement BaseAgent**

```python
# src/valentine/agents/base.py
from __future__ import annotations

import time
from abc import ABC, abstractmethod

from valentine.bus.redis_bus import RedisBus
from valentine.llm.provider import FallbackChain
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger


class BaseAgent(ABC):
    name: AgentName

    def __init__(self, bus: RedisBus, llm: FallbackChain) -> None:
        self.bus = bus
        self.llm = llm
        self._log = get_logger(f"agent.{self.name.value}")
        self._running = False

    @property
    def task_stream(self) -> str:
        return f"stream:agent.{self.name.value}.task"

    @property
    def result_stream(self) -> str:
        return f"stream:agent.{self.name.value}.result"

    @property
    def consumer_group(self) -> str:
        return f"group:{self.name.value}"

    @abstractmethod
    async def process(self, task: AgentTask) -> TaskResult:
        """Process a task and return a result. Implemented by each agent."""
        ...

    async def handle_task(self, task: AgentTask) -> None:
        """Wrap process() with error handling and result publishing."""
        start = time.monotonic()
        try:
            result = await self.process(task)
            result.processing_time_ms = int((time.monotonic() - start) * 1000)
        except Exception as e:
            self._log.error("task_failed", task_id=task.task_id, error=str(e))
            result = TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=False,
                content_type=ContentType.TEXT,
                error=str(e),
                processing_time_ms=int((time.monotonic() - start) * 1000),
            )
        await self.bus.publish_to_stream(self.result_stream, result.to_dict())

    async def start(self) -> None:
        """Main loop: listen for tasks on the stream and process them."""
        self._running = True
        await self.bus.ensure_consumer_group(self.task_stream, self.consumer_group)
        self._log.info("agent_started", agent=self.name.value)

        while self._running:
            messages = await self.bus.read_from_stream(
                self.task_stream, self.consumer_group, self.name.value, count=1, block=5000,
            )
            for msg_id, data in messages:
                task = AgentTask.from_dict(data)
                self._log.info("task_received", task_id=task.task_id)
                await self.handle_task(task)
                await self.bus.ack_message(self.task_stream, self.consumer_group, msg_id)

    async def stop(self) -> None:
        self._running = False
        self._log.info("agent_stopping", agent=self.name.value)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_base_agent.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/base.py tests/test_base_agent.py
git commit -m "feat: BaseAgent ABC with lifecycle, error handling, stream wiring"
```

---

## Task 6: ZeroClaw Intent Router

**Files:**
- Create: `src/valentine/orchestrator/zeroclaw.py`
- Create: `tests/test_zeroclaw.py`

- [ ] **Step 1: Write failing tests for ZeroClaw**

```python
# tests/test_zeroclaw.py
import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from valentine.orchestrator.zeroclaw import ZeroClaw
from valentine.models import IncomingMessage, ContentType, AgentName, RoutingDecision
from valentine.llm.provider import LLMResponse


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish_to_stream = AsyncMock()
    bus.publish_pubsub = AsyncMock()
    bus.ensure_consumer_group = AsyncMock()
    bus.read_from_stream = AsyncMock(return_value=[])
    bus.ack_message = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    return llm


@pytest.fixture
def mock_cortex():
    cortex = AsyncMock()
    cortex.fetch_context = AsyncMock(return_value=["user prefers Python"])
    return cortex


class TestZeroClawRouting:
    @pytest.mark.asyncio
    async def test_routes_code_request(self, mock_bus, mock_llm, mock_cortex):
        zc = ZeroClaw(bus=mock_bus, llm=mock_llm, cortex_fetch=mock_cortex.fetch_context)
        mock_llm.chat.return_value = LLMResponse(
            text=json.dumps({"intent": "code_generation", "agent": "codesmith", "priority": "normal", "chain": None, "params": {}}),
            model="llama-3.1-8b", provider="groq", usage={},
        )
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT,
            text="write me a Python script to sort files",
        )
        decision = await zc.classify(msg)
        assert decision.agent == AgentName.CODESMITH
        assert decision.intent == "code_generation"

    @pytest.mark.asyncio
    async def test_routes_voice_to_echo(self, mock_bus, mock_llm, mock_cortex):
        zc = ZeroClaw(bus=mock_bus, llm=mock_llm, cortex_fetch=mock_cortex.fetch_context)
        msg = IncomingMessage(
            message_id="2", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.VOICE,
            media_path="/tmp/voice.ogg",
        )
        decision = await zc.classify(msg)
        assert decision.agent == AgentName.ECHO

    @pytest.mark.asyncio
    async def test_routes_photo_to_iris(self, mock_bus, mock_llm, mock_cortex):
        zc = ZeroClaw(bus=mock_bus, llm=mock_llm, cortex_fetch=mock_cortex.fetch_context)
        msg = IncomingMessage(
            message_id="3", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.PHOTO,
            media_path="/tmp/photo.jpg",
        )
        decision = await zc.classify(msg)
        assert decision.agent == AgentName.IRIS

    @pytest.mark.asyncio
    async def test_defaults_to_oracle_on_unknown(self, mock_bus, mock_llm, mock_cortex):
        zc = ZeroClaw(bus=mock_bus, llm=mock_llm, cortex_fetch=mock_cortex.fetch_context)
        mock_llm.chat.return_value = LLMResponse(
            text="not valid json", model="m", provider="groq", usage={},
        )
        msg = IncomingMessage(
            message_id="4", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text="hey",
        )
        decision = await zc.classify(msg)
        assert decision.agent == AgentName.ORACLE

    @pytest.mark.asyncio
    async def test_injects_memory_context(self, mock_bus, mock_llm, mock_cortex):
        zc = ZeroClaw(bus=mock_bus, llm=mock_llm, cortex_fetch=mock_cortex.fetch_context)
        mock_llm.chat.return_value = LLMResponse(
            text=json.dumps({"intent": "chat", "agent": "oracle", "priority": "normal", "chain": None, "params": {}}),
            model="m", provider="groq", usage={},
        )
        msg = IncomingMessage(
            message_id="5", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text="hello",
        )
        decision = await zc.classify(msg)
        assert "user prefers Python" in decision.memory_context
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_zeroclaw.py -v`
Expected: FAIL

- [ ] **Step 3: Implement ZeroClaw**

```python
# src/valentine/orchestrator/zeroclaw.py
from __future__ import annotations

import json
import uuid
from typing import Callable, Awaitable

from valentine.bus.redis_bus import RedisBus
from valentine.llm.provider import FallbackChain
from valentine.models import (
    AgentName, AgentTask, ContentType, IncomingMessage,
    Priority, RoutingDecision, TaskResult,
)
from valentine.utils import get_logger

log = get_logger("orchestrator.zeroclaw")

ROUTER_SYSTEM_PROMPT = """You are ZeroClaw, an intent classification router. Given a user message, classify the intent and choose which agent should handle it.

Agents available:
- codesmith: code generation, debugging, shell commands, file operations, GitHub, DevOps
- oracle: general questions, research, web search, reasoning, conversation, chat
- iris: image analysis, OCR, image generation, visual Q&A
- echo: voice/audio transcription and text-to-speech (only for audio messages)
- cortex: explicit memory operations ("remember this", "what did I say about...")

Rules:
- Voice messages ALWAYS go to echo
- Photos/images ALWAYS go to iris
- If the intent is ambiguous, default to oracle
- For multi-step tasks, set "chain" to an ordered list of agents

Respond ONLY with valid JSON:
{"intent": "<intent_name>", "agent": "<agent_name>", "priority": "normal", "chain": null, "params": {}}
"""


class ZeroClaw:
    def __init__(
        self,
        bus: RedisBus,
        llm: FallbackChain,
        cortex_fetch: Callable[[str], Awaitable[list[str]]] | None = None,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.cortex_fetch = cortex_fetch
        self._running = False

    async def classify(self, message: IncomingMessage) -> RoutingDecision:
        """Classify intent and return routing decision."""
        # Fast-path: media types route directly without LLM
        if message.content_type == ContentType.VOICE:
            memory = await self._fetch_memory(message.user_id)
            return RoutingDecision(intent="voice_transcription", agent=AgentName.ECHO, memory_context=memory)

        if message.content_type == ContentType.PHOTO:
            memory = await self._fetch_memory(message.user_id)
            return RoutingDecision(intent="image_analysis", agent=AgentName.IRIS, memory_context=memory)

        if message.content_type == ContentType.DOCUMENT:
            memory = await self._fetch_memory(message.user_id)
            return RoutingDecision(intent="document_analysis", agent=AgentName.IRIS, memory_context=memory)

        # Text messages: use LLM to classify
        memory = await self._fetch_memory(message.user_id)

        try:
            response = await self.llm.chat(
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": message.text or ""},
                ],
                model="llama-3.1-8b-instant",
                temperature=0.0,
                max_tokens=256,
            )
            data = json.loads(response.text)
            return RoutingDecision(
                intent=data.get("intent", "chat"),
                agent=AgentName(data.get("agent", "oracle")),
                priority=Priority(data.get("priority", "normal")),
                chain=[AgentName(a) for a in data["chain"]] if data.get("chain") else None,
                params=data.get("params", {}),
                memory_context=memory,
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning("classification_failed", error=str(e), fallback="oracle")
            return RoutingDecision(intent="chat", agent=AgentName.ORACLE, memory_context=memory)

    async def _fetch_memory(self, user_id: str) -> list[str]:
        if self.cortex_fetch:
            try:
                return await self.cortex_fetch(user_id)
            except Exception as e:
                log.warning("memory_fetch_failed", error=str(e))
        return []

    async def route(self, message: IncomingMessage) -> None:
        """Classify and dispatch a message to the appropriate agent."""
        decision = await self.classify(message)
        log.info("routed", intent=decision.intent, agent=decision.agent.value)

        task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent=decision.agent,
            routing=decision,
            message=message,
        )

        if decision.chain:
            # For chained tasks, send to the first agent in the chain
            first = decision.chain[0]
            await self.bus.publish_to_stream(f"stream:agent.{first.value}.task", task.to_dict())
        else:
            await self.bus.publish_to_stream(f"stream:agent.{decision.agent.value}.task", task.to_dict())

    async def handle_result(self, result: TaskResult, original_task: AgentTask) -> None:
        """Handle agent result — chain to next agent or send to Nexus."""
        chain = original_task.routing.chain
        if chain:
            current_idx = next(
                (i for i, a in enumerate(chain) if a == result.agent), -1
            )
            if current_idx >= 0 and current_idx < len(chain) - 1:
                next_agent = chain[current_idx + 1]
                next_task = AgentTask(
                    task_id=original_task.task_id,
                    agent=next_agent,
                    routing=original_task.routing,
                    message=original_task.message,
                    previous_results=original_task.previous_results + [result.text or ""],
                )
                await self.bus.publish_to_stream(
                    f"stream:agent.{next_agent.value}.task", next_task.to_dict()
                )
                return

        # Final result — send to Nexus
        await self.bus.publish_pubsub("pubsub:nexus.respond", {
            "chat_id": original_task.message.chat_id,
            "result": result.to_dict(),
        })

    async def start(self) -> None:
        """Main loop: read from route stream, classify, dispatch."""
        self._running = True
        stream = "stream:zeroclaw.route"
        group = "group:zeroclaw"
        await self.bus.ensure_consumer_group(stream, group)
        log.info("zeroclaw_started")

        while self._running:
            messages = await self.bus.read_from_stream(stream, group, "zeroclaw", count=1, block=5000)
            for msg_id, data in messages:
                incoming = IncomingMessage.from_dict(data)
                await self.route(incoming)
                await self.bus.ack_message(stream, group, msg_id)

    async def stop(self) -> None:
        self._running = False
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_zeroclaw.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/orchestrator/ tests/test_zeroclaw.py
git commit -m "feat: ZeroClaw intent router with LLM classification and memory injection"
```

---

## Task 7: Oracle — Research & Reasoning Agent

**Files:**
- Create: `src/valentine/agents/oracle.py`
- Create: `tests/test_oracle_agent.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_oracle_agent.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from valentine.agents.oracle import OracleAgent
from valentine.models import (
    AgentTask, AgentName, RoutingDecision, IncomingMessage,
    ContentType, TaskResult,
)
from valentine.llm.provider import LLMResponse


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish_to_stream = AsyncMock()
    return bus


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(
        text="Paris is the capital of France.",
        model="qwen-3-32b", provider="cerebras", usage={},
    )
    return llm


@pytest.fixture
def make_task():
    def _make(text: str, intent: str = "chat") -> AgentTask:
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text=text,
        )
        routing = RoutingDecision(
            intent=intent, agent=AgentName.ORACLE,
            memory_context=["user is a developer"],
        )
        return AgentTask(task_id="t1", agent=AgentName.ORACLE, routing=routing, message=msg)
    return _make


class TestOracleAgent:
    @pytest.mark.asyncio
    async def test_chat_response(self, mock_bus, mock_llm, make_task):
        agent = OracleAgent(bus=mock_bus, llm=mock_llm)
        task = make_task("What is the capital of France?")
        result = await agent.process(task)
        assert result.success is True
        assert "Paris" in result.text

    @pytest.mark.asyncio
    async def test_includes_memory_in_prompt(self, mock_bus, mock_llm, make_task):
        agent = OracleAgent(bus=mock_bus, llm=mock_llm)
        task = make_task("help me out")
        await agent.process(task)
        call_args = mock_llm.chat.call_args
        messages = call_args[1]["messages"] if "messages" in call_args[1] else call_args[0][0]
        system_msg = next(m for m in messages if m["role"] == "system")
        assert "user is a developer" in system_msg["content"]

    @pytest.mark.asyncio
    async def test_web_search_intent(self, mock_bus, mock_llm, make_task):
        agent = OracleAgent(bus=mock_bus, llm=mock_llm)
        task = make_task("search the web for latest AI news", intent="web_search")
        with patch.object(agent, "_web_search", new_callable=AsyncMock, return_value="AI news: big stuff happened"):
            result = await agent.process(task)
        assert result.success is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_oracle_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Implement OracleAgent**

```python
# src/valentine/agents/oracle.py
from __future__ import annotations

from duckduckgo_search import DDGS

from valentine.agents.base import BaseAgent
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger

log = get_logger("agent.oracle")

ORACLE_SYSTEM_PROMPT = """You are Oracle, Valentine's world-class research and reasoning agent. You are a brilliant analyst who provides deep, accurate, and well-reasoned responses.

Traits:
- You cite sources and distinguish fact from speculation
- You admit uncertainty rather than guessing
- You are concise but thorough
- You handle casual conversation naturally and warmly
- When given web search results, you synthesize them into a clear answer

{memory_context}
"""


class OracleAgent(BaseAgent):
    name = AgentName.ORACLE

    async def process(self, task: AgentTask) -> TaskResult:
        text = task.message.text or ""
        intent = task.routing.intent
        memory = task.routing.memory_context

        # Build system prompt with memory
        memory_section = ""
        if memory:
            memory_section = "What you know about this user:\n" + "\n".join(f"- {m}" for m in memory)
        system = ORACLE_SYSTEM_PROMPT.format(memory_context=memory_section)

        # If web search intent, search first and include results
        search_context = ""
        if intent in ("web_search", "research"):
            search_context = await self._web_search(text)

        messages = [{"role": "system", "content": system}]

        if search_context:
            messages.append({"role": "user", "content": f"Web search results for context:\n{search_context}\n\nUser question: {text}"})
        else:
            # Include previous results from chain if any
            if task.previous_results:
                context = "\n".join(task.previous_results)
                messages.append({"role": "user", "content": f"Context from previous analysis:\n{context}\n\nUser request: {text}"})
            else:
                messages.append({"role": "user", "content": text})

        response = await self.llm.chat(messages=messages, model="qwen-3-32b")
        return TaskResult(
            task_id=task.task_id,
            agent=self.name,
            success=True,
            content_type=ContentType.TEXT,
            text=response.text,
        )

    async def _web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web using DuckDuckGo."""
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            if not results:
                return ""
            return "\n".join(
                f"- {r.get('title', '')}: {r.get('body', '')}" for r in results
            )
        except Exception as e:
            log.warning("web_search_failed", error=str(e))
            return ""
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_oracle_agent.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/oracle.py tests/test_oracle_agent.py
git commit -m "feat: Oracle agent with chat, web search, and memory integration"
```

---

## Task 8: CodeSmith — Code & DevOps Agent

**Files:**
- Create: `src/valentine/agents/codesmith.py`
- Create: `tests/test_codesmith.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_codesmith.py
import pytest
from unittest.mock import AsyncMock, patch
from valentine.agents.codesmith import CodeSmithAgent
from valentine.models import (
    AgentTask, AgentName, RoutingDecision, IncomingMessage,
    ContentType, TaskResult,
)
from valentine.llm.provider import LLMResponse


@pytest.fixture
def mock_bus():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(
        text="```python\ndef hello():\n    print('hello')\n```",
        model="qwen-qwq-32b", provider="groq", usage={},
    )
    return llm


@pytest.fixture
def make_task():
    def _make(text: str, intent: str = "code_generation") -> AgentTask:
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT, text=text,
        )
        routing = RoutingDecision(intent=intent, agent=AgentName.CODESMITH)
        return AgentTask(task_id="t1", agent=AgentName.CODESMITH, routing=routing, message=msg)
    return _make


class TestCodeSmithAgent:
    @pytest.mark.asyncio
    async def test_code_generation(self, mock_bus, mock_llm, make_task):
        agent = CodeSmithAgent(bus=mock_bus, llm=mock_llm)
        task = make_task("write a hello world function in python")
        result = await agent.process(task)
        assert result.success is True
        assert result.text is not None

    @pytest.mark.asyncio
    async def test_shell_execution(self, mock_bus, mock_llm, make_task):
        agent = CodeSmithAgent(bus=mock_bus, llm=mock_llm)
        output = await agent._execute_shell("echo hello", timeout=5)
        assert "hello" in output

    @pytest.mark.asyncio
    async def test_shell_blocks_dangerous_commands(self, mock_bus, mock_llm, make_task):
        agent = CodeSmithAgent(bus=mock_bus, llm=mock_llm)
        output = await agent._execute_shell("rm -rf /", timeout=5)
        assert "blocked" in output.lower() or "denied" in output.lower()

    @pytest.mark.asyncio
    async def test_file_read(self, mock_bus, mock_llm, make_task, tmp_path):
        agent = CodeSmithAgent(bus=mock_bus, llm=mock_llm)
        agent._allowed_dirs = [str(tmp_path)]
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello content")
        content = await agent._read_file(str(test_file))
        assert content == "hello content"

    @pytest.mark.asyncio
    async def test_file_read_blocked_outside_allowed(self, mock_bus, mock_llm, make_task):
        agent = CodeSmithAgent(bus=mock_bus, llm=mock_llm)
        agent._allowed_dirs = ["/tmp/valentine/workspace"]
        content = await agent._read_file("/etc/passwd")
        assert "denied" in content.lower() or "blocked" in content.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_codesmith.py -v`
Expected: FAIL

- [ ] **Step 3: Implement CodeSmithAgent**

```python
# src/valentine/agents/codesmith.py
from __future__ import annotations

import asyncio
import os
import re

from valentine.agents.base import BaseAgent
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger

log = get_logger("agent.codesmith")

CODESMITH_SYSTEM_PROMPT = """You are CodeSmith, Valentine's senior full-stack engineer agent. You write clean, production-quality code and debug with surgical precision.

Traits:
- You reason through problems step by step before writing code
- You write code that is correct, secure, and well-structured
- You explain your approach concisely
- You handle errors by diagnosing root causes, not guessing
- When asked to run commands, you describe what you'll do first

{memory_context}

Tools available to you (the system will execute these):
- Shell execution: you can request shell commands to be run
- File read/write: you can read and write files in the workspace
- The user will see your text response — include code blocks with language tags
"""

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"rm\s+-rf\s+~",
    r"mkfs\.",
    r"dd\s+if=",
    r":\(\)\{",  # fork bomb
    r"chmod\s+-R\s+777\s+/",
    r"curl.*\|\s*sh",
    r"wget.*\|\s*sh",
]


class CodeSmithAgent(BaseAgent):
    name = AgentName.CODESMITH

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._allowed_dirs = list(settings.allowed_shell_dirs)

    async def process(self, task: AgentTask) -> TaskResult:
        text = task.message.text or ""
        memory = task.routing.memory_context

        memory_section = ""
        if memory:
            memory_section = "What you know about this user:\n" + "\n".join(f"- {m}" for m in memory)

        system = CODESMITH_SYSTEM_PROMPT.format(memory_context=memory_section)

        messages = [{"role": "system", "content": system}]

        if task.previous_results:
            context = "\n".join(task.previous_results)
            messages.append({"role": "user", "content": f"Context from previous analysis:\n{context}\n\nUser request: {text}"})
        else:
            messages.append({"role": "user", "content": text})

        response = await self.llm.chat(messages=messages, model="qwen-qwq-32b")
        return TaskResult(
            task_id=task.task_id,
            agent=self.name,
            success=True,
            content_type=ContentType.TEXT,
            text=response.text,
        )

    async def _execute_shell(self, command: str, timeout: int = 30) -> str:
        """Execute a shell command in a sandboxed environment."""
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command):
                return f"BLOCKED: Command denied — matches dangerous pattern"

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=settings.workspace_dir if os.path.isdir(settings.workspace_dir) else "/tmp",
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode() if stdout else ""
            errors = stderr.decode() if stderr else ""
            if errors:
                output += f"\nSTDERR: {errors}"
            return output or "(no output)"
        except asyncio.TimeoutError:
            proc.kill()
            return f"TIMEOUT: Command exceeded {timeout}s limit"
        except Exception as e:
            return f"ERROR: {str(e)}"

    async def _read_file(self, path: str) -> str:
        """Read a file if it's within allowed directories."""
        abs_path = os.path.abspath(path)
        if not any(abs_path.startswith(os.path.abspath(d)) for d in self._allowed_dirs):
            return f"DENIED: Path {path} is outside allowed directories"
        try:
            with open(abs_path) as f:
                return f.read()
        except Exception as e:
            return f"ERROR: {str(e)}"

    async def _write_file(self, path: str, content: str) -> str:
        """Write a file if it's within allowed directories."""
        abs_path = os.path.abspath(path)
        if not any(abs_path.startswith(os.path.abspath(d)) for d in self._allowed_dirs):
            return f"DENIED: Path {path} is outside allowed directories"
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w") as f:
                f.write(content)
            return f"OK: Written {len(content)} bytes to {path}"
        except Exception as e:
            return f"ERROR: {str(e)}"
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_codesmith.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/codesmith.py tests/test_codesmith.py
git commit -m "feat: CodeSmith agent with sandboxed shell, file ops, and code generation"
```

---

## Task 9: Iris — Vision & Image Agent

**Files:**
- Create: `src/valentine/agents/iris.py`
- Create: `tests/test_iris.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_iris.py
import pytest
from unittest.mock import AsyncMock, patch
from valentine.agents.iris import IrisAgent
from valentine.models import (
    AgentTask, AgentName, RoutingDecision, IncomingMessage,
    ContentType, TaskResult,
)
from valentine.llm.provider import LLMResponse


@pytest.fixture
def mock_bus():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(
        text="This image shows a sunset over mountains.",
        model="Qwen2.5-VL-72B", provider="sambanova", usage={},
    )
    return llm


@pytest.fixture
def make_task():
    def _make(content_type=ContentType.PHOTO, text=None, media_path="/tmp/test.jpg"):
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=content_type,
            text=text, media_path=media_path,
        )
        routing = RoutingDecision(intent="image_analysis", agent=AgentName.IRIS)
        return AgentTask(task_id="t1", agent=AgentName.IRIS, routing=routing, message=msg)
    return _make


class TestIrisAgent:
    @pytest.mark.asyncio
    async def test_image_analysis(self, mock_bus, mock_llm, make_task):
        agent = IrisAgent(bus=mock_bus, llm=mock_llm)
        task = make_task()
        with patch.object(agent, "_analyze_image", new_callable=AsyncMock, return_value="sunset over mountains"):
            result = await agent.process(task)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_image_generation_intent(self, mock_bus, mock_llm, make_task):
        agent = IrisAgent(bus=mock_bus, llm=mock_llm)
        task = make_task(content_type=ContentType.TEXT, text="generate an image of a cat", media_path=None)
        task.routing.intent = "image_generation"
        with patch.object(agent, "_generate_image", new_callable=AsyncMock, return_value="/tmp/generated.jpg"):
            result = await agent.process(task)
        assert result.success is True
        assert result.content_type == ContentType.PHOTO
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_iris.py -v`
Expected: FAIL

- [ ] **Step 3: Implement IrisAgent**

```python
# src/valentine/agents/iris.py
from __future__ import annotations

import base64
import os

import httpx

from valentine.agents.base import BaseAgent
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger

log = get_logger("agent.iris")

IRIS_SYSTEM_PROMPT = """You are Iris, Valentine's vision and image analysis agent. You see with precision and describe exactly what is in the image.

Traits:
- You describe what you actually observe, not what you expect
- You separate observation from interpretation
- You are thorough but concise
- For OCR, you extract text exactly as written
- For diagrams/charts, you describe the structure and data
"""


class IrisAgent(BaseAgent):
    name = AgentName.IRIS

    async def process(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent

        if intent == "image_generation":
            return await self._handle_generation(task)
        else:
            return await self._handle_analysis(task)

    async def _handle_analysis(self, task: AgentTask) -> TaskResult:
        """Analyze an image using multimodal LLM."""
        media_path = task.message.media_path
        text = task.message.text or "Describe this image in detail."

        if media_path and os.path.exists(media_path):
            analysis = await self._analyze_image(media_path, text)
        else:
            analysis = "No image provided for analysis."

        return TaskResult(
            task_id=task.task_id,
            agent=self.name,
            success=True,
            content_type=ContentType.TEXT,
            text=analysis,
        )

    async def _handle_generation(self, task: AgentTask) -> TaskResult:
        """Generate an image from a text prompt."""
        prompt = task.message.text or ""
        try:
            image_path = await self._generate_image(prompt)
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                content_type=ContentType.PHOTO,
                text=f"Generated image for: {prompt}",
                media_path=image_path,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=False,
                error=f"Image generation failed: {str(e)}",
            )

    async def _analyze_image(self, image_path: str, prompt: str) -> str:
        """Send image to multimodal model for analysis."""
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()

        messages = [
            {"role": "system", "content": IRIS_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ]
        response = await self.llm.chat(messages=messages, model=settings.sambanova_vision_model)
        return response.text

    async def _generate_image(self, prompt: str) -> str:
        """Generate image via Pollinations.ai API."""
        safe_prompt = prompt.replace(" ", "%20")
        url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1024&height=1024&nologo=true"

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        output_path = os.path.join(settings.workspace_dir, f"generated_{hash(prompt) & 0xFFFFFF}.jpg")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(resp.content)

        return output_path
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_iris.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/iris.py tests/test_iris.py
git commit -m "feat: Iris agent with image analysis, OCR, and generation"
```

---

## Task 10: Echo — Voice Agent

**Files:**
- Create: `src/valentine/agents/echo.py`
- Create: `tests/test_echo.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_echo.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from valentine.agents.echo import EchoAgent
from valentine.models import (
    AgentTask, AgentName, RoutingDecision, IncomingMessage,
    ContentType, TaskResult,
)


@pytest.fixture
def mock_bus():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    return AsyncMock()


@pytest.fixture
def make_task():
    def _make(media_path="/tmp/voice.ogg"):
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.VOICE,
            media_path=media_path,
        )
        routing = RoutingDecision(intent="voice_transcription", agent=AgentName.ECHO)
        return AgentTask(task_id="t1", agent=AgentName.ECHO, routing=routing, message=msg)
    return _make


class TestEchoAgent:
    @pytest.mark.asyncio
    async def test_transcription(self, mock_bus, mock_llm, make_task):
        agent = EchoAgent(bus=mock_bus, llm=mock_llm)
        task = make_task()
        with patch.object(agent, "_transcribe", new_callable=AsyncMock, return_value="hello world"):
            result = await agent.process(task)
        assert result.success is True
        assert result.text == "hello world"

    @pytest.mark.asyncio
    async def test_tts(self, mock_bus, mock_llm):
        agent = EchoAgent(bus=mock_bus, llm=mock_llm)
        with patch("valentine.agents.echo.edge_tts") as mock_edge:
            mock_communicate = AsyncMock()
            mock_edge.Communicate.return_value = mock_communicate
            mock_communicate.save = AsyncMock()
            path = await agent._text_to_speech("hello world")
        assert path.endswith(".mp3")

    @pytest.mark.asyncio
    async def test_missing_media_path(self, mock_bus, mock_llm, make_task):
        agent = EchoAgent(bus=mock_bus, llm=mock_llm)
        task = make_task(media_path=None)
        result = await agent.process(task)
        assert result.success is False
        assert "no audio" in result.error.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_echo.py -v`
Expected: FAIL

- [ ] **Step 3: Implement EchoAgent**

```python
# src/valentine/agents/echo.py
from __future__ import annotations

import os
import tempfile

import edge_tts

from valentine.agents.base import BaseAgent
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger

log = get_logger("agent.echo")


class EchoAgent(BaseAgent):
    name = AgentName.ECHO

    def __init__(self, *args, groq_client=None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._groq_client = groq_client  # Direct Groq client for Whisper API

    async def process(self, task: AgentTask) -> TaskResult:
        media_path = task.message.media_path
        if not media_path:
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=False,
                error="No audio file provided",
            )

        try:
            transcription = await self._transcribe(media_path)
            return TaskResult(
                task_id=task.task_id,
                agent=self.name,
                success=True,
                content_type=ContentType.TEXT,
                text=transcription,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=False,
                error=f"Transcription failed: {str(e)}",
            )

    async def _transcribe(self, audio_path: str) -> str:
        """Transcribe audio — try Groq Whisper API first, fall back to local."""
        wav_path = await self._convert_to_wav(audio_path)

        if self._groq_client:
            try:
                return await self._groq_client.transcribe(wav_path)
            except Exception as e:
                log.warning("groq_whisper_failed", error=str(e), fallback="local")

        return self._local_transcribe(wav_path)

    def _local_transcribe(self, wav_path: str) -> str:
        """Transcribe using local Whisper tiny model."""
        import whisper
        model = whisper.load_model("tiny")
        result = model.transcribe(wav_path)
        return result["text"].strip()

    async def _convert_to_wav(self, input_path: str) -> str:
        """Convert audio to WAV format using ffmpeg."""
        import asyncio
        output_path = input_path.rsplit(".", 1)[0] + ".wav"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return output_path

    async def _text_to_speech(self, text: str, voice: str = "en-US-GuyNeural") -> str:
        """Convert text to speech using Edge TTS."""
        output_path = os.path.join(tempfile.gettempdir(), f"tts_{hash(text) & 0xFFFFFF}.mp3")
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_path)
        return output_path
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_echo.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/echo.py tests/test_echo.py
git commit -m "feat: Echo agent with Groq Whisper STT and Edge TTS"
```

---

## Task 11: Cortex — Memory Agent

**Files:**
- Create: `src/valentine/agents/cortex.py`
- Create: `tests/test_cortex.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cortex.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from valentine.agents.cortex import CortexAgent
from valentine.models import (
    AgentTask, AgentName, RoutingDecision, IncomingMessage,
    ContentType, TaskResult,
)
from valentine.llm.provider import LLMResponse


@pytest.fixture
def mock_bus():
    return AsyncMock()


@pytest.fixture
def mock_llm():
    llm = AsyncMock()
    llm.chat.return_value = LLMResponse(
        text="Key facts: user is a developer working on Valentine AI project",
        model="qwen-3-32b", provider="cerebras", usage={},
    )
    return llm


@pytest.fixture
def mock_mem0():
    mem0 = MagicMock()
    mem0.add = MagicMock(return_value={"results": [{"id": "m1"}]})
    mem0.search = MagicMock(return_value={"results": [{"memory": "user is a Python dev", "score": 0.9}]})
    mem0.get_all = MagicMock(return_value={"results": [{"memory": "user likes concise code"}]})
    return mem0


class TestCortexAgent:
    @pytest.mark.asyncio
    async def test_fetch_context(self, mock_bus, mock_llm, mock_mem0):
        agent = CortexAgent(bus=mock_bus, llm=mock_llm)
        agent._mem0 = mock_mem0
        memories = await agent.fetch_context("user123", "help me with python")
        assert len(memories) > 0
        mock_mem0.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_memory(self, mock_bus, mock_llm, mock_mem0):
        agent = CortexAgent(bus=mock_bus, llm=mock_llm)
        agent._mem0 = mock_mem0
        await agent.store_memory("user123", "I prefer dark mode")
        mock_mem0.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_explicit_remember(self, mock_bus, mock_llm, mock_mem0):
        agent = CortexAgent(bus=mock_bus, llm=mock_llm)
        agent._mem0 = mock_mem0
        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT,
            text="remember that I like Python more than JavaScript",
        )
        routing = RoutingDecision(intent="memory_store", agent=AgentName.CORTEX)
        task = AgentTask(task_id="t1", agent=AgentName.CORTEX, routing=routing, message=msg)
        result = await agent.process(task)
        assert result.success is True
        mock_mem0.add.assert_called()

    @pytest.mark.asyncio
    async def test_process_memory_recall(self, mock_bus, mock_llm, mock_mem0):
        agent = CortexAgent(bus=mock_bus, llm=mock_llm)
        agent._mem0 = mock_mem0
        msg = IncomingMessage(
            message_id="2", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT,
            text="what do you know about me?",
        )
        routing = RoutingDecision(intent="memory_recall", agent=AgentName.CORTEX)
        task = AgentTask(task_id="t2", agent=AgentName.CORTEX, routing=routing, message=msg)
        result = await agent.process(task)
        assert result.success is True
        assert result.text is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cortex.py -v`
Expected: FAIL

- [ ] **Step 3: Implement CortexAgent**

```python
# src/valentine/agents/cortex.py
from __future__ import annotations

from mem0 import Memory

from valentine.agents.base import BaseAgent
from valentine.config import settings
from valentine.models import AgentName, AgentTask, TaskResult, ContentType
from valentine.utils import get_logger

log = get_logger("agent.cortex")

MEMORY_EXTRACTION_PROMPT = """Analyze this conversation and extract key facts worth remembering about the user. Focus on:
- Personal preferences and habits
- Technical skills and tools they use
- Ongoing projects and goals
- Communication style preferences

Conversation:
{conversation}

List only the important facts, one per line. If nothing worth remembering, respond with "NONE"."""


class CortexAgent(BaseAgent):
    name = AgentName.CORTEX

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mem0: Memory | None = None

    def _get_mem0(self) -> Memory:
        if self._mem0 is None:
            config = {
                "vector_store": {
                    "provider": "qdrant",
                    "config": {
                        "host": settings.qdrant_host,
                        "port": settings.qdrant_port,
                        "collection_name": "valentine_memories",
                    },
                },
            }
            self._mem0 = Memory.from_config(config)
        return self._mem0

    async def process(self, task: AgentTask) -> TaskResult:
        intent = task.routing.intent
        text = task.message.text or ""
        user_id = task.message.user_id

        if intent in ("memory_store", "remember"):
            return await self._handle_store(task, user_id, text)
        elif intent in ("memory_recall", "what_do_you_know"):
            return await self._handle_recall(task, user_id, text)
        else:
            return await self._handle_recall(task, user_id, text)

    async def _handle_store(self, task: AgentTask, user_id: str, text: str) -> TaskResult:
        """Store a memory explicitly requested by the user."""
        try:
            mem0 = self._get_mem0()
            mem0.add(text, user_id=user_id)
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=True,
                text="Got it, I'll remember that.",
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=False,
                error=f"Failed to store memory: {str(e)}",
            )

    async def _handle_recall(self, task: AgentTask, user_id: str, text: str) -> TaskResult:
        """Recall memories related to user's query."""
        try:
            mem0 = self._get_mem0()
            results = mem0.get_all(user_id=user_id)
            memories = [r["memory"] for r in results.get("results", [])]
            if not memories:
                return TaskResult(
                    task_id=task.task_id, agent=self.name, success=True,
                    text="I don't have any memories stored about you yet.",
                )
            memory_list = "\n".join(f"- {m}" for m in memories)
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=True,
                text=f"Here's what I know about you:\n{memory_list}",
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id, agent=self.name, success=False,
                error=f"Failed to recall memories: {str(e)}",
            )

    async def fetch_context(self, user_id: str, query: str | None = None) -> list[str]:
        """Sync context fetch — fast vector lookup, no LLM call. Called by ZeroClaw on every request."""
        try:
            mem0 = self._get_mem0()
            if query:
                results = mem0.search(query, user_id=user_id)
            else:
                results = mem0.get_all(user_id=user_id)
            return [r["memory"] for r in results.get("results", [])[:5]]
        except Exception as e:
            log.warning("context_fetch_failed", error=str(e))
            return []

    async def extract_and_store(self, user_id: str, user_text: str, agent_response: str) -> None:
        """Async memory extraction — runs after response delivery."""
        try:
            conversation = f"User: {user_text}\nAssistant: {agent_response}"
            prompt = MEMORY_EXTRACTION_PROMPT.format(conversation=conversation)
            response = await self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model="qwen-3-32b",
                temperature=0.0,
            )
            if response.text.strip().upper() != "NONE":
                mem0 = self._get_mem0()
                mem0.add(response.text, user_id=user_id)
                log.info("memory_extracted", user_id=user_id)
        except Exception as e:
            log.warning("memory_extraction_failed", error=str(e))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_cortex.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/agents/cortex.py tests/test_cortex.py
git commit -m "feat: Cortex agent with Mem0 memory, sync fetch, and async extraction"
```

---

## Task 12: Nexus — Telegram Platform Adapter

**Files:**
- Create: `src/valentine/nexus/adapter.py`
- Create: `src/valentine/nexus/telegram.py`
- Create: `tests/test_nexus.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_nexus.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from valentine.nexus.telegram import TelegramAdapter
from valentine.models import ContentType


@pytest.fixture
def mock_bus():
    bus = AsyncMock()
    bus.publish_to_stream = AsyncMock()
    bus.subscribe = AsyncMock()
    return bus


class TestTelegramAdapter:
    def test_creates_incoming_message_from_text(self):
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        update = MagicMock()
        update.effective_message.message_id = 42
        update.effective_chat.id = 123
        update.effective_user.id = 456
        update.effective_message.text = "hello"
        update.effective_message.photo = None
        update.effective_message.voice = None
        update.effective_message.document = None

        msg = adapter._to_incoming_message(update)
        assert msg.message_id == "42"
        assert msg.chat_id == "123"
        assert msg.content_type == ContentType.TEXT
        assert msg.text == "hello"

    def test_creates_incoming_message_from_photo(self):
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        update = MagicMock()
        update.effective_message.message_id = 43
        update.effective_chat.id = 123
        update.effective_user.id = 456
        update.effective_message.text = None
        update.effective_message.caption = "check this out"
        update.effective_message.photo = [MagicMock()]
        update.effective_message.voice = None
        update.effective_message.document = None

        msg = adapter._to_incoming_message(update)
        assert msg.content_type == ContentType.PHOTO
        assert msg.text == "check this out"

    def test_creates_incoming_message_from_voice(self):
        adapter = TelegramAdapter.__new__(TelegramAdapter)
        update = MagicMock()
        update.effective_message.message_id = 44
        update.effective_chat.id = 123
        update.effective_user.id = 456
        update.effective_message.text = None
        update.effective_message.photo = None
        update.effective_message.voice = MagicMock()
        update.effective_message.document = None

        msg = adapter._to_incoming_message(update)
        assert msg.content_type == ContentType.VOICE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_nexus.py -v`
Expected: FAIL

- [ ] **Step 3: Implement PlatformAdapter ABC**

```python
# src/valentine/nexus/adapter.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from valentine.models import IncomingMessage


class PlatformAdapter(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Start listening for messages."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the adapter."""
        ...

    @abstractmethod
    async def send_text(self, chat_id: str, text: str) -> None:
        ...

    @abstractmethod
    async def send_image(self, chat_id: str, image_path: str, caption: str | None = None) -> None:
        ...

    @abstractmethod
    async def send_voice(self, chat_id: str, audio_path: str) -> None:
        ...

    @abstractmethod
    async def send_document(self, chat_id: str, doc_path: str, filename: str | None = None) -> None:
        ...
```

- [ ] **Step 4: Implement TelegramAdapter**

```python
# src/valentine/nexus/telegram.py
from __future__ import annotations

import os
import tempfile

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

from valentine.bus.redis_bus import RedisBus
from valentine.models import IncomingMessage, ContentType, TaskResult
from valentine.nexus.adapter import PlatformAdapter
from valentine.utils import get_logger

log = get_logger("nexus.telegram")


class TelegramAdapter(PlatformAdapter):
    def __init__(self, token: str, bus: RedisBus) -> None:
        self._token = token
        self._bus = bus
        self._app: Application | None = None

    def _to_incoming_message(self, update: Update) -> IncomingMessage:
        """Convert a Telegram Update to an IncomingMessage."""
        msg = update.effective_message

        if msg.voice:
            content_type = ContentType.VOICE
            text = None
        elif msg.photo:
            content_type = ContentType.PHOTO
            text = msg.caption
        elif msg.document:
            content_type = ContentType.DOCUMENT
            text = msg.caption
        elif msg.video:
            content_type = ContentType.VIDEO
            text = msg.caption
        else:
            content_type = ContentType.TEXT
            text = msg.text

        return IncomingMessage(
            message_id=str(msg.message_id),
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
            platform="telegram",
            content_type=content_type,
            text=text,
        )

    async def _download_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
        """Download media from Telegram and return local path."""
        msg = update.effective_message
        file = None

        if msg.voice:
            file = await context.bot.get_file(msg.voice.file_id)
            ext = ".ogg"
        elif msg.photo:
            file = await context.bot.get_file(msg.photo[-1].file_id)  # highest res
            ext = ".jpg"
        elif msg.document:
            file = await context.bot.get_file(msg.document.file_id)
            ext = os.path.splitext(msg.document.file_name or ".bin")[1]
        elif msg.video:
            file = await context.bot.get_file(msg.video.file_id)
            ext = ".mp4"

        if file:
            path = os.path.join(tempfile.gettempdir(), f"tg_{msg.message_id}{ext}")
            await file.download_to_drive(path)
            return path
        return None

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all incoming messages."""
        incoming = self._to_incoming_message(update)

        # Download media if present
        if incoming.content_type in (ContentType.VOICE, ContentType.PHOTO, ContentType.DOCUMENT, ContentType.VIDEO):
            media_path = await self._download_media(update, context)
            incoming.media_path = media_path

        # Send typing indicator
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

        # Publish to ZeroClaw route stream
        await self._bus.publish_to_stream("stream:zeroclaw.route", incoming.to_dict())
        log.info("message_forwarded", chat_id=incoming.chat_id, type=incoming.content_type.value)

    async def _handle_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Valentine v2 online. ZeroClaw routing active. How can I help?")

    async def send_text(self, chat_id: str, text: str) -> None:
        if self._app and self._app.bot:
            # Split long messages (Telegram limit is 4096 chars)
            for i in range(0, len(text), 4096):
                await self._app.bot.send_message(
                    chat_id=int(chat_id), text=text[i:i + 4096], parse_mode="Markdown",
                )

    async def send_image(self, chat_id: str, image_path: str, caption: str | None = None) -> None:
        if self._app and self._app.bot:
            with open(image_path, "rb") as f:
                await self._app.bot.send_photo(chat_id=int(chat_id), photo=f, caption=caption)

    async def send_voice(self, chat_id: str, audio_path: str) -> None:
        if self._app and self._app.bot:
            with open(audio_path, "rb") as f:
                await self._app.bot.send_voice(chat_id=int(chat_id), voice=f)

    async def send_document(self, chat_id: str, doc_path: str, filename: str | None = None) -> None:
        if self._app and self._app.bot:
            with open(doc_path, "rb") as f:
                await self._app.bot.send_document(
                    chat_id=int(chat_id), document=f, filename=filename,
                )

    async def handle_response(self, chat_id: str, result: TaskResult) -> None:
        """Send a TaskResult back to the user on Telegram."""
        if result.content_type == ContentType.TEXT and result.text:
            await self.send_text(chat_id, result.text)
        elif result.content_type == ContentType.PHOTO and result.media_path:
            await self.send_image(chat_id, result.media_path, caption=result.text)
        elif result.content_type == ContentType.VOICE and result.media_path:
            await self.send_voice(chat_id, result.media_path)
        elif result.content_type == ContentType.DOCUMENT and result.media_path:
            await self.send_document(chat_id, result.media_path)
        elif result.error:
            await self.send_text(chat_id, f"Error: {result.error}")
        else:
            await self.send_text(chat_id, result.text or "Done.")

    async def start(self) -> None:
        self._app = Application.builder().token(self._token).build()
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(MessageHandler(
            filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL | filters.VIDEO,
            self._handle_message,
        ))

        # Start response listener in background
        import asyncio
        asyncio.create_task(self._listen_for_responses())

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("telegram_adapter_started")

    async def _listen_for_responses(self) -> None:
        """Listen for responses from ZeroClaw via pub/sub and send to users."""
        async for data in self._bus.subscribe("pubsub:nexus.respond"):
            chat_id = data.get("chat_id")
            result_data = data.get("result")
            if chat_id and result_data:
                result = TaskResult.from_dict(result_data)
                await self.handle_response(chat_id, result)

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_nexus.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/valentine/nexus/ tests/test_nexus.py
git commit -m "feat: Nexus Telegram adapter with media handling and response routing"
```

---

## Task 13: Main Entry Point & Process Supervisor

**Files:**
- Create: `src/valentine/main.py`
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from valentine.models import IncomingMessage, ContentType, AgentName


class TestIntegrationRouting:
    """Test that the full routing pipeline works end-to-end with mocks."""

    @pytest.mark.asyncio
    async def test_text_message_routes_to_oracle(self):
        from valentine.orchestrator.zeroclaw import ZeroClaw
        from valentine.llm.provider import LLMResponse
        import json

        bus = AsyncMock()
        llm = AsyncMock()
        llm.chat.return_value = LLMResponse(
            text=json.dumps({"intent": "chat", "agent": "oracle", "priority": "normal", "chain": None, "params": {}}),
            model="m", provider="groq", usage={},
        )
        cortex_fetch = AsyncMock(return_value=[])
        zc = ZeroClaw(bus=bus, llm=llm, cortex_fetch=cortex_fetch)

        msg = IncomingMessage(
            message_id="1", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.TEXT,
            text="hello there",
        )
        await zc.route(msg)

        bus.publish_to_stream.assert_called_once()
        call_args = bus.publish_to_stream.call_args[0]
        assert "oracle" in call_args[0]

    @pytest.mark.asyncio
    async def test_voice_routes_to_echo_without_llm(self):
        from valentine.orchestrator.zeroclaw import ZeroClaw

        bus = AsyncMock()
        llm = AsyncMock()
        cortex_fetch = AsyncMock(return_value=[])
        zc = ZeroClaw(bus=bus, llm=llm, cortex_fetch=cortex_fetch)

        msg = IncomingMessage(
            message_id="2", chat_id="c", user_id="u",
            platform="telegram", content_type=ContentType.VOICE,
            media_path="/tmp/voice.ogg",
        )
        await zc.route(msg)

        # Voice routes directly — no LLM call needed
        llm.chat.assert_not_called()
        bus.publish_to_stream.assert_called_once()
        call_args = bus.publish_to_stream.call_args[0]
        assert "echo" in call_args[0]
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Implement main.py entry point**

```python
# src/valentine/main.py
from __future__ import annotations

import asyncio
import multiprocessing
import signal
import sys

from valentine.bus.redis_bus import RedisBus
from valentine.config import settings
from valentine.llm.provider import FallbackChain
from valentine.llm.groq import GroqClient
from valentine.llm.cerebras import CerebrasClient
from valentine.llm.sambanova import SambaNovaClient
from valentine.utils import setup_logging, get_logger

log = get_logger("main")


def create_providers() -> dict[str, object]:
    """Create LLM provider clients."""
    return {
        "groq": GroqClient(api_key=settings.groq_api_key),
        "cerebras": CerebrasClient(api_key=settings.cerebras_api_key),
        "sambanova": SambaNovaClient(api_key=settings.sambanova_api_key),
    }


def create_fallback_chain(providers: dict, order: list[str]) -> FallbackChain:
    return FallbackChain([providers[name] for name in order if name in providers])


async def run_zeroclaw(bus: RedisBus, providers: dict) -> None:
    from valentine.orchestrator.zeroclaw import ZeroClaw
    from valentine.agents.cortex import CortexAgent

    llm = create_fallback_chain(providers, ["groq", "cerebras", "sambanova"])
    cortex_llm = create_fallback_chain(providers, ["cerebras", "groq", "sambanova"])
    cortex = CortexAgent(bus=bus, llm=cortex_llm)
    zc = ZeroClaw(bus=bus, llm=llm, cortex_fetch=cortex.fetch_context)
    await zc.start()


async def run_agent(agent_name: str, bus: RedisBus, providers: dict) -> None:
    from valentine.agents.oracle import OracleAgent
    from valentine.agents.codesmith import CodeSmithAgent
    from valentine.agents.iris import IrisAgent
    from valentine.agents.echo import EchoAgent
    from valentine.agents.cortex import CortexAgent

    agent_map = {
        "oracle": (OracleAgent, ["cerebras", "groq", "sambanova"]),
        "codesmith": (CodeSmithAgent, ["groq", "cerebras", "sambanova"]),
        "iris": (IrisAgent, ["sambanova", "groq", "cerebras"]),
        "echo": (EchoAgent, ["groq", "cerebras", "sambanova"]),
        "cortex": (CortexAgent, ["cerebras", "groq", "sambanova"]),
    }

    cls, order = agent_map[agent_name]
    llm = create_fallback_chain(providers, order)

    if agent_name == "echo":
        agent = cls(bus=bus, llm=llm, groq_client=providers.get("groq"))
    else:
        agent = cls(bus=bus, llm=llm)

    await agent.start()


async def run_nexus(bus: RedisBus) -> None:
    from valentine.nexus.telegram import TelegramAdapter
    adapter = TelegramAdapter(token=settings.telegram_bot_token, bus=bus)
    await adapter.start()
    # Keep running
    while True:
        await asyncio.sleep(1)


def agent_process(agent_name: str) -> None:
    """Entry point for each agent subprocess."""
    setup_logging()
    log = get_logger(f"process.{agent_name}")
    log.info("starting", agent=agent_name)

    async def _run():
        bus = RedisBus(url=settings.redis_url)
        await bus.connect()
        providers = create_providers()
        try:
            if agent_name == "zeroclaw":
                await run_zeroclaw(bus, providers)
            elif agent_name == "nexus":
                await run_nexus(bus)
            else:
                await run_agent(agent_name, bus, providers)
        finally:
            for p in providers.values():
                if hasattr(p, "close"):
                    await p.close()
            await bus.close()

    asyncio.run(_run())


def main() -> None:
    setup_logging()
    multiprocessing.set_start_method("spawn")

    components = ["nexus", "zeroclaw", "oracle", "codesmith", "iris", "echo", "cortex"]
    processes: dict[str, multiprocessing.Process] = {}

    def start_component(name: str) -> multiprocessing.Process:
        p = multiprocessing.Process(target=agent_process, args=(name,), name=f"valentine-{name}")
        p.daemon = True
        p.start()
        log.info("process_started", component=name, pid=p.pid)
        return p

    # Start all components
    for name in components:
        processes[name] = start_component(name)

    # Supervisor loop — restart crashed processes
    def handle_signal(sig, frame):
        log.info("shutdown_signal_received", signal=sig)
        for name, proc in processes.items():
            if proc.is_alive():
                proc.terminate()
        for proc in processes.values():
            proc.join(timeout=30)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log.info("valentine_v2_started", components=components)

    try:
        while True:
            for name, proc in list(processes.items()):
                if not proc.is_alive():
                    log.warning("process_crashed", component=name, restarting=True)
                    processes[name] = start_component(name)
            import time
            time.sleep(5)
    except KeyboardInterrupt:
        handle_signal(signal.SIGINT, None)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run all tests**

Run: `pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/valentine/main.py tests/test_integration.py
git commit -m "feat: main entry point with process supervisor and graceful shutdown"
```

---

## Task 14: Docker Compose & Deployment Config

**Files:**
- Create: `docker-compose.yml`
- Create: `scripts/setup.sh`
- Create: `scripts/health_check.sh`
- Create: `.gitignore`

- [ ] **Step 1: Create docker-compose.yml**

```yaml
# docker-compose.yml
version: "3.8"

services:
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 512M

volumes:
  qdrant_data:
```

- [ ] **Step 2: Create setup script**

```bash
#!/usr/bin/env bash
# scripts/setup.sh — Full setup for Valentine v2
set -euo pipefail

echo "=== Valentine v2 Setup ==="

# Check Python version
python3 --version | grep -q "3.1[1-9]" || { echo "ERROR: Python 3.11+ required"; exit 1; }

# Install Redis
if ! command -v redis-server &>/dev/null; then
    echo "Installing Redis..."
    sudo apt install -y redis-server
    sudo systemctl enable redis-server
    sudo systemctl start redis-server
fi

# Install Docker (for Qdrant)
if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
fi

# Install ffmpeg (for voice processing)
if ! command -v ffmpeg &>/dev/null; then
    echo "Installing ffmpeg..."
    sudo apt install -y ffmpeg
fi

# Start Qdrant
echo "Starting Qdrant..."
docker compose up -d qdrant

# Create virtualenv and install
echo "Setting up Python environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Create workspace
mkdir -p workspace

# Setup swap (2GB)
if ! swapon --show | grep -q "/swapfile"; then
    echo "Setting up 2GB swap..."
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

# Verify
echo ""
echo "=== Verification ==="
redis-cli ping
docker compose ps
python3 -c "from valentine.config import settings; print('Config OK')"
echo ""
echo "=== Setup Complete ==="
echo "Run with: python -m valentine.main"
```

- [ ] **Step 3: Create health check script**

```bash
#!/usr/bin/env bash
# scripts/health_check.sh
set -euo pipefail

echo "=== Valentine Health Check ==="

# Redis
echo -n "Redis: "
redis-cli ping 2>/dev/null || echo "DOWN"

# Qdrant
echo -n "Qdrant: "
curl -sf http://localhost:6333/healthz && echo " UP" || echo "DOWN"

# Valentine process
echo -n "Valentine: "
pgrep -f "valentine.main" >/dev/null && echo "UP" || echo "DOWN"

# Memory usage
echo ""
echo "=== Memory ==="
free -h | head -2
```

- [ ] **Step 4: Create .gitignore**

```
# .gitignore
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/
build/
.env
*.key
*.key.pub
workspace/
*.ogg
*.wav
*.mp3
.ruff_cache/
.pytest_cache/
```

- [ ] **Step 5: Make scripts executable and commit**

```bash
chmod +x scripts/setup.sh scripts/health_check.sh
touch workspace/.gitkeep
git add docker-compose.yml scripts/ .gitignore workspace/.gitkeep
git commit -m "feat: Docker Compose, setup/health scripts, and gitignore"
```

---

## Task 15: Final Wiring & Full Test Run

**Files:**
- Modify: `src/valentine/llm/__init__.py`
- Modify: `src/valentine/agents/__init__.py`

- [ ] **Step 1: Add convenience exports to __init__ files**

```python
# src/valentine/llm/__init__.py
from valentine.llm.provider import LLMProvider, LLMResponse, FallbackChain
from valentine.llm.groq import GroqClient
from valentine.llm.cerebras import CerebrasClient
from valentine.llm.sambanova import SambaNovaClient
from valentine.llm.quota import QuotaTracker

__all__ = ["LLMProvider", "LLMResponse", "FallbackChain", "GroqClient", "CerebrasClient", "SambaNovaClient", "QuotaTracker"]
```

```python
# src/valentine/agents/__init__.py
from valentine.agents.base import BaseAgent
from valentine.agents.oracle import OracleAgent
from valentine.agents.codesmith import CodeSmithAgent
from valentine.agents.iris import IrisAgent
from valentine.agents.echo import EchoAgent
from valentine.agents.cortex import CortexAgent

__all__ = ["BaseAgent", "OracleAgent", "CodeSmithAgent", "IrisAgent", "EchoAgent", "CortexAgent"]
```

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 3: Run ruff linter**

Run: `ruff check src/ tests/`
Expected: No errors (or only minor style warnings)

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: final wiring, exports, and Valentine v2 complete"
```

- [ ] **Step 5: Verify the full project structure**

Run: `find src/valentine -type f -name "*.py" | sort`
Expected output:
```
src/valentine/__init__.py
src/valentine/agents/__init__.py
src/valentine/agents/base.py
src/valentine/agents/codesmith.py
src/valentine/agents/cortex.py
src/valentine/agents/echo.py
src/valentine/agents/iris.py
src/valentine/agents/oracle.py
src/valentine/bus/__init__.py
src/valentine/bus/redis_bus.py
src/valentine/config.py
src/valentine/llm/__init__.py
src/valentine/llm/cerebras.py
src/valentine/llm/groq.py
src/valentine/llm/provider.py
src/valentine/llm/quota.py
src/valentine/llm/sambanova.py
src/valentine/main.py
src/valentine/models.py
src/valentine/nexus/__init__.py
src/valentine/nexus/adapter.py
src/valentine/nexus/telegram.py
src/valentine/orchestrator/__init__.py
src/valentine/orchestrator/zeroclaw.py
src/valentine/utils.py
```
