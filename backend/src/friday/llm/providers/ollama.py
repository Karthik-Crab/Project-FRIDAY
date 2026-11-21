"""Ollama adapter (local, offline-capable). Uses the native /api/chat endpoint."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace

import httpx

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest, Message, Role
from friday.llm.base import ProviderInfo
from friday.llm.providers._http import DEFAULT_TIMEOUT, iter_ndjson, raise_for_status


class OllamaProvider:
    def __init__(
        self,
        name: str,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        client: httpx.AsyncClient | None = None,
        keep_alive: str = "30m",
        think: bool | None = None,
        num_ctx: int | None = None,
    ) -> None:
        # How long Ollama keeps the model in memory after a request. Its own
        # default (5 minutes) means a cold, multi-second start after any pause.
        self._keep_alive = keep_alive
        # False: a model that can think first answers straight away instead.
        self._think = think
        # How much of the conversation the model can read at once (tokens).
        # Sent with every request, warm-up included: a different value makes
        # Ollama load the model again.
        self._num_ctx = num_ctx
        self._info = ProviderInfo(name=name, kind="ollama", model=model, local=True)
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT)

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        payload = {
            "model": self._info.model,
            "stream": True,
            "keep_alive": self._keep_alive,
            "messages": [{"role": "system", "content": request.system}]
            + [_message(m) for m in request.messages],
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
                **self._load_options(),
            },
        }
        if self._think is not None:
            payload["think"] = self._think
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

    def _load_options(self) -> dict[str, int]:
        return {"num_ctx": self._num_ctx} if self._num_ctx else {}

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def warm_up(self, request: ChatRequest | None = None) -> None:
        """Load the model into memory now instead of on the first question.

        With ``request`` the model also reads the system prompt now. Ollama
        keeps what it has read, so the first real turn only has to read the
        user's words; on a CPU, reading a long prompt takes several seconds.
        The request goes down the same path as a real turn, with the same
        options, and is dropped at the first word of the answer: anything
        that differs from a real turn risks Ollama starting again from nothing.
        """
        try:
            if request is None:
                payload: dict[str, object] = {
                    "model": self._info.model,
                    "messages": [],
                    "keep_alive": self._keep_alive,
                }
                if options := self._load_options():
                    payload["options"] = options
                await self._client.post("/api/chat", json=payload)
                return
            primer = replace(request, messages=(Message(Role.USER, "Hi"),))
            stream = self.stream(primer)
            try:
                async for _ in stream:
                    break
            finally:
                await stream.aclose()
        except (httpx.HTTPError, ProviderError):
            pass  # best effort; a real request will report the problem

    async def unload(self) -> None:
        """Take the model out of memory now (what ``ollama stop`` does). Best effort."""
        try:
            await self._client.post(
                "/api/chat", json={"model": self._info.model, "messages": [], "keep_alive": 0}
            )
        except httpx.HTTPError:
            pass

    async def aclose(self) -> None:
        await self._client.aclose()


def _message(message: Message) -> dict[str, object]:
    data: dict[str, object] = {"role": message.role.value, "content": message.content}
    if message.images:
        data["images"] = list(message.images)
    return data
