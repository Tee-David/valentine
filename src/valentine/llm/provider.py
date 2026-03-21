# src/valentine/llm/provider.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncGenerator, Dict, Any, List


class LLMProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass
        
    @property
    @abstractmethod
    def default_model(self) -> str:
        pass

    @abstractmethod
    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> str:
        """Standard chat completion"""
        pass

    @abstractmethod
    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        """Streaming chat completion"""
        pass


class MultimodalProvider(LLMProvider):
    @abstractmethod
    async def image_completion(
        self,
        prompt: str,
        image_url_or_base64: str,
        model: str | None = None,
        **kwargs: Any
    ) -> str:
        """Image to Text / Visual Question Answering"""
        pass


class AudioProvider(ABC):
    @abstractmethod
    async def transcribe_audio(
        self,
        audio_path: str,
        model: str | None = None,
        **kwargs: Any
    ) -> str:
        """Audio to Text transcription"""
        pass
