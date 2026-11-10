"""Anthropic Messages API adapter (streaming)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import httpx

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest, Role
from friday.llm.base import ProviderInfo
from friday.llm.providers._http import DEFAULT_TIMEOUT, iter_sse_data, raise_for_status

API_VERSION = "2023-06-01"


class AnthropicProvider:
    def __init__(
        self,
        name: str,
        model: str,
        api_key: str | None,
        base_url: str = "https://api.anthropic.com",
        send_temperature: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._info = ProviderInfo(name=name, kind="anthropic", model=model, local=False)
        self._send_temperature = send_temperature
        headers = {"anthropic-version": API_VERSION}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = client or httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=DEFAULT_TIMEOUT
        )

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        # The Messages API requires the first message to be from the user and
        # roles to alternate; the conversation engine guarantees that.
        payload: dict[str, object] = {
            "model": self._info.model,
            "stream": True,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [
                {"role": m.role.value, "content": m.content}
                for m in request.messages
                if m.role is not Role.SYSTEM
            ],
        }
        if self._send_temperature:
            payload["temperature"] = request.temperature
        try:
            async with self._client.stream("POST", "/v1/messages", json=payload) as response:
                if response.status_code >= 400:
                    raise_for_status(self._info.name, response, await response.aread())
                async for data in iter_sse_data(response):
                    event = json.loads(data)
                    etype = event.get("type")
                    if etype == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield delta["text"]
                    elif etype == "error":
                        err = event.get("error", {})
                        retryable = err.get("type") in ("overloaded_error", "api_error")
                        raise ProviderError(
                            self._info.name, str(err.get("message", err)), retryable=retryable
                        )
                    elif etype == "message_stop":
                        return
        except httpx.HTTPError as exc:
            raise ProviderError(self._info.name, f"connection error: {type(exc).__name__}") from exc

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("/v1/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()
