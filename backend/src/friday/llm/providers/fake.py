"""Deterministic provider for tests and offline demos."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest
from friday.llm.base import ProviderInfo


class FakeProvider:
    def __init__(
        self,
        name: str = "fake",
        reply: str | Callable[[ChatRequest], str] = "Hello from the fake provider.",
        *,
        delay: float = 0.0,
        fail_with: ProviderError | None = None,
        fail_after_chunks: int | None = None,
        local: bool = True,
    ) -> None:
        self._info = ProviderInfo(name=name, kind="fake", model="fake-1", local=local)
        self._reply = reply
        self._delay = delay
        self._fail_with = fail_with
        self._fail_after = fail_after_chunks
        self.requests: list[ChatRequest] = []
        self.cancelled = False
        self.healthy = True

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        self.requests.append(request)
        if self._fail_with is not None and self._fail_after is None:
            raise self._fail_with
        text = self._reply(request) if callable(self._reply) else self._reply
        words = text.split(" ")
        try:
            for i, word in enumerate(words):
                if self._fail_after is not None and i >= self._fail_after:
                    raise self._fail_with or ProviderError(self._info.name, "mid-stream failure")
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield word if i == 0 else " " + word
        except (asyncio.CancelledError, GeneratorExit):
            self.cancelled = True
            raise

    async def health_check(self) -> bool:
        return self.healthy

    async def aclose(self) -> None:
        return None
