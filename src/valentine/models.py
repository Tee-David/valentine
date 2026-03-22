# src/valentine/models.py
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class MessageSource(str, Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    API = "api"


class ContentType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    DOCUMENT = "document"
    VIDEO = "video"


class AgentName(str, Enum):
    ZEROCLAW = "zeroclaw"
    CODESMITH = "codesmith"
    ORACLE = "oracle"
    IRIS = "iris"
    ECHO = "echo"
    CORTEX = "cortex"
    NEXUS = "nexus"
    BROWSER = "browser"


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
    user_name: str | None = None
    reply_to_text: str | None = None
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
            "user_name": self.user_name,
            "reply_to_text": self.reply_to_text,
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
            user_name=data.get("user_name"),
            reply_to_text=data.get("reply_to_text"),
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
    file_name: str | None = None  # human-readable filename for downloads
    error: str | None = None
    chat_id: str | None = None
    processing_time_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "agent": self.agent.value,
            "success": self.success,
            "content_type": self.content_type.value,
            "text": self.text,
            "media_path": self.media_path,
            "file_name": self.file_name,
            "error": self.error,
            "chat_id": self.chat_id,
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
            file_name=data.get("file_name"),
            error=data.get("error"),
            chat_id=data.get("chat_id"),
            processing_time_ms=data.get("processing_time_ms", 0),
        )
