# src/valentine/llm/fallback.py
from __future__ import annotations

import logging
from typing import AsyncGenerator, Dict, Any, List
from .provider import LLMProvider

logger = logging.getLogger(__name__)

class FallbackChain(LLMProvider):
    def __init__(self, providers: List[LLMProvider]):
        self.providers = providers

    @property
    def provider_name(self) -> str:
        return "fallback_chain"
        
    @property
    def default_model(self) -> str:
        if self.providers:
            return self.providers[0].default_model
        return "unknown"

    async def _try_provider(
        self,
        provider: LLMProvider,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> str:
        try:
            return await provider.chat_completion(
                messages, 
                model=model if provider == self.providers[0] else None, 
                temperature=temperature, 
                max_tokens=max_tokens, 
                **kwargs
            )
        except Exception as e:
            logger.warning(f"Provider {provider.provider_name} failed: {e}")
            raise e

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> str:
        last_exception = None
        for provider in self.providers:
            try:
                return await self._try_provider(
                    provider, messages, model, temperature, max_tokens, **kwargs
                )
            except Exception as e:
                last_exception = e
                continue
        raise Exception(f"All providers in fallback chain failed. Last error: {last_exception}")

    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        last_exception = None
        for provider in self.providers:
            try:
                # To catch connection errors on stream init, we must yield from it
                stream = provider.stream_chat_completion(
                    messages, 
                    model=model if provider == self.providers[0] else None, 
                    temperature=temperature, 
                    max_tokens=max_tokens, 
                    **kwargs
                )
                async for chunk in stream:
                    yield chunk
                return  # Yielding complete, stop fallback
            except Exception as e:
                logger.warning(f"Provider {provider.provider_name} stream failed: {e}")
                last_exception = e
                continue
        raise Exception(f"All providers in fallback chain failed. Last error: {last_exception}")
