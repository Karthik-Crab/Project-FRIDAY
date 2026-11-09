"""OpenAI Chat Completions adapter.

Also covers OpenAI-compatible servers (LM Studio, vLLM, llama.cpp server,
OpenRouter...) by changing ``base_url``. ``local`` must be declared by config
because the adapter cannot know whether ``base_url`` is on this machine.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import httpx

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest
from friday.llm.base import ProviderInfo
from friday.llm.providers._http import DEFAULT_TIMEOUT, iter_sse_data, raise_for_status


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        model: str,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        local: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._info = ProviderInfo(name=name, kind="openai", model=model, local=local)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=DEFAULT_TIMEOUT
        )

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        payload = {
            "model": self._info.model,
            "stream": True,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": [{"role": "system", "content": request.system}]
            + [{"role": m.role.value, "content": m.content} for m in request.messages],
        }
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    raise_for_status(self._info.name, response, await response.aread())
                async for data in iter_sse_data(response):
                    if data == "[DONE]":
                        return
                    event = json.loads(data)
                    if "error" in event:
                        raise ProviderError(self._info.name, str(event["error"]))
                    for choice in event.get("choices", []):
                        content = (choice.get("delta") or {}).get("content")
                        if content:
                            yield content
        except httpx.HTTPError as exc:
            raise ProviderError(self._info.name, f"connection error: {type(exc).__name__}") from exc

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
