# src/valentine/llm/cerebras.py
import httpx
from typing import AsyncGenerator, Dict, Any, List
from .provider import LLMProvider
from valentine.config import settings

class CerebrasClient(LLMProvider):
    def __init__(self):
        self._api_key = settings.cerebras_api_key
        self._base_url = settings.cerebras_base_url
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=120.0
        )

    @property
    def provider_name(self) -> str:
        return "cerebras"
        
    @property
    def default_model(self) -> str:
        return settings.cerebras_default_model

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> str:
        req_model = model or self.default_model
        payload = {
            "model": req_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            **kwargs,
        }
        response = await self._client.post("/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    async def stream_chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs: Any
    ) -> AsyncGenerator[str, None]:
        req_model = model or self.default_model
        payload = {
            "model": req_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **kwargs,
        }
        async with self._client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    line = line[len("data: "):]
                    if line == "[DONE]":
                        break
                    import json
                    try:
                        chunk = json.loads(line)
                        if "choices" in chunk and len(chunk["choices"]) > 0:
                            delta = chunk["choices"][0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                yield delta["content"]
                    except json.JSONDecodeError:
                        continue

    async def close(self):
        await self._client.aclose()
