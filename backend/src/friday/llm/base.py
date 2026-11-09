"""The language-model provider contract.

A provider does exactly one thing: turn a provider-neutral ``ChatRequest``
into a stream of text deltas. Personality, memory, routing and tool use are
*not* provider concerns -- that is what keeps FRIDAY's identity stable when
the underlying model changes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from friday.core.messages import ChatRequest


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    name: str  # instance name from config, e.g. "claude-main"
    kind: str  # adapter kind, e.g. "anthropic"
    model: str
    local: bool  # True if it works without internet


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...

    def stream(self, request: ChatRequest) -> AsyncGenerator[str, None]:
        """Yield text deltas. Raise ``ProviderError`` on failure.

        Implementations must be cancellation-safe: if the consumer stops
        iterating (barge-in), the underlying HTTP stream must be closed.
        """
        ...

    async def health_check(self) -> bool: ...

    async def aclose(self) -> None: ...
