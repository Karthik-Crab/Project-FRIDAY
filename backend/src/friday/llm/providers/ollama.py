"""Ollama adapter (local, offline-capable). Uses the native /api/chat endpoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest
from friday.llm.base import ProviderInfo
from friday.llm.providers._http import DEFAULT_TIMEOUT, iter_ndjson, raise_for_status


class OllamaProvider:
    def __init__(
        self,
        name: str,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._info = ProviderInfo(name=name, kind="ollama", model=model, local=True)
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT)

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        payload = {
            "model": self._info.model,
            "stream": True,
            "messages": [{"role": "system", "content": request.system}]
            + [{"role": m.role.value, "content": m.content} for m in request.messages],
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    raise_for_status(self._info.name, response, await response.aread())
                async for event in iter_ndjson(response):
                    if "error" in event:
                        raise ProviderError(self._info.name, str(event["error"]))
                    message = event.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str) and content:
                            yield content
                    if event.get("done"):
                        return
        except httpx.HTTPError as exc:
            raise ProviderError(self._info.name, f"connection error: {type(exc).__name__}") from exc

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
