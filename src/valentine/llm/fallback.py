# src/valentine/llm/fallback.py
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncGenerator, Dict, Any, List
from .provider import LLMProvider, MultimodalProvider

logger = logging.getLogger(__name__)

# Circuit breaker: skip a provider for this many seconds after a failure
_CIRCUIT_OPEN_DURATION = 15  # seconds
_PER_PROVIDER_TIMEOUT = 25  # seconds — max time to wait for a single provider


class FallbackChain(MultimodalProvider):
    """Wraps multiple LLM providers with automatic failover and circuit breakers.
    Also implements MultimodalProvider by delegating to the first capable provider.
    """
    def __init__(self, providers: List[LLMProvider]):
        self.providers = providers
        # Track when each provider last failed: provider_name → timestamp
        self._circuit_open_until: Dict[str, float] = {}

    @property
    def provider_name(self) -> str:
        return "fallback_chain"

    @property
    def default_model(self) -> str:
        if self.providers:
            return self.providers[0].default_model
        return "unknown"

    def _is_circuit_open(self, provider: LLMProvider) -> bool:
        """Check if a provider's circuit breaker is tripped (skip it)."""
        deadline = self._circuit_open_until.get(provider.provider_name, 0)
        return time.monotonic() < deadline

    def _trip_circuit(self, provider: LLMProvider) -> None:
        """Trip the circuit breaker for a provider after a failure."""
        self._circuit_open_until[provider.provider_name] = (
            time.monotonic() + _CIRCUIT_OPEN_DURATION
        )
        logger.warning(
            f"Circuit breaker tripped for {provider.provider_name} — "
            f"skipping for {_CIRCUIT_OPEN_DURATION}s"
        )

    def _close_circuit(self, provider: LLMProvider) -> None:
        """Reset the circuit breaker after a successful call."""
        self._circuit_open_until.pop(provider.provider_name, None)

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
            result = await asyncio.wait_for(
                provider.chat_completion(
                    messages,
                    model=model if provider == self.providers[0] else None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs
                ),
                timeout=_PER_PROVIDER_TIMEOUT,
            )
            self._close_circuit(provider)
            return result
        except asyncio.TimeoutError:
            self._trip_circuit(provider)
            logger.warning(f"Provider {provider.provider_name} timed out after {_PER_PROVIDER_TIMEOUT}s")
            raise
        except Exception as e:
            self._trip_circuit(provider)
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
            if self._is_circuit_open(provider):
                logger.info(f"Skipping {provider.provider_name} (circuit open)")
                continue
            try:
                return await self._try_provider(
                    provider, messages, model, temperature, max_tokens, **kwargs
                )
            except Exception as e:
                last_exception = e
                continue
        raise Exception(
            "I'm having trouble connecting to my AI providers right now. "
            "Please try again in a moment!"
        )

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
            if self._is_circuit_open(provider):
                logger.info(f"Skipping {provider.provider_name} (circuit open)")
                continue
            try:
                stream = provider.stream_chat_completion(
                    messages,
                    model=model if provider == self.providers[0] else None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs
                )
                async for chunk in stream:
                    yield chunk
                self._close_circuit(provider)
                return
            except Exception as e:
                self._trip_circuit(provider)
                logger.warning(f"Provider {provider.provider_name} stream failed: {e}")
                last_exception = e
                continue
        raise Exception(
            "I'm having trouble connecting to my AI providers right now. "
            "Please try again in a moment!"
        )

    async def image_completion(
        self,
        prompt: str,
        image_url_or_base64: str,
        model: str | None = None,
        **kwargs: Any
    ) -> str:
        """Delegate vision/image analysis to the first MultimodalProvider in the chain."""
        last_exception = None
        for provider in self.providers:
            if not isinstance(provider, MultimodalProvider):
                continue
            if self._is_circuit_open(provider):
                logger.info(f"Skipping {provider.provider_name} for vision (circuit open)")
                continue
            try:
                result = await asyncio.wait_for(
                    provider.image_completion(prompt, image_url_or_base64, model=model, **kwargs),
                    timeout=_PER_PROVIDER_TIMEOUT,
                )
                self._close_circuit(provider)
                return result
            except asyncio.TimeoutError:
                self._trip_circuit(provider)
                logger.warning(f"Vision provider {provider.provider_name} timed out")
                last_exception = TimeoutError(f"{provider.provider_name} vision timed out")
            except Exception as e:
                self._trip_circuit(provider)
                logger.warning(f"Vision provider {provider.provider_name} failed: {e}")
                last_exception = e
        raise Exception(
            "I can't analyze images right now — all vision providers are unavailable. "
            "Please try again in a moment!"
        )

