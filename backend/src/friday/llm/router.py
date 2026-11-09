"""Provider selection with fallback.

Routes map a *purpose* (``conversation``, ``reasoning``, ``offline``...) to an
ordered list of provider instance names. The conversation engine asks for a
purpose; it never names a vendor.

Fallback rules:
* A failure *before the first token* falls through to the next provider.
* A failure *after* text has been emitted is surfaced: we cannot splice two
  models' output into one sentence without it being audible.
* A failed provider is put on cooldown so the next turn does not pay the
  timeout again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass

from friday.core.errors import ProviderError, ProviderUnavailableError
from friday.core.messages import ChatRequest
from friday.llm.base import LLMProvider, ProviderInfo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamResult:
    """Populated while streaming; tells the caller who actually answered."""

    provider: ProviderInfo | None = None


class ProviderRouter:
    def __init__(
        self,
        providers: dict[str, LLMProvider],
        routes: dict[str, list[str]],
        *,
        cooldown_seconds: float = 30.0,
        offline: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for purpose, names in routes.items():
            missing = [n for n in names if n not in providers]
            if missing:
                raise ValueError(f"route '{purpose}' references unknown providers {missing}")
        self._providers = providers
        self._routes = routes
        self._cooldown = cooldown_seconds
        self._down_until: dict[str, float] = {}
        self._clock = clock
        self.offline = offline
        self.override: str | None = None

    @property
    def providers(self) -> dict[str, LLMProvider]:
        return dict(self._providers)

    def set_override(self, name: str | None) -> None:
        if name is not None and name not in self._providers:
            raise ValueError(f"unknown provider '{name}'")
        self.override = name

    def candidates(self, purpose: str) -> list[LLMProvider]:
        if self.override:
            names = [self.override]
        else:
            names = list(self._routes.get(purpose) or self._routes.get("default") or [])
            # Always end with the offline route so FRIDAY never goes silent
            # just because the internet is down.
            for n in self._routes.get("offline", []):
                if n not in names:
                    names.append(n)
        now = self._clock()
        result = []
        for name in names:
            provider = self._providers[name]
            if self.offline and not provider.info.local:
                continue
            if self._down_until.get(name, 0.0) > now and not self.override:
                continue
            result.append(provider)
        return result

    async def stream(
        self, purpose: str, request: ChatRequest, result: StreamResult | None = None
    ) -> AsyncGenerator[str, None]:
        candidates = self.candidates(purpose)
        if not candidates:
            raise ProviderUnavailableError("router", f"no provider available for '{purpose}'")
        errors: list[str] = []
        for provider in candidates:
            name = provider.info.name
            started = False
            gen = provider.stream(request)
            try:
                async for delta in gen:
                    if not started:
                        started = True
                        if result is not None:
                            result.provider = provider.info
                    yield delta
                self._down_until.pop(name, None)
                return
            except ProviderError as exc:
                self._down_until[name] = self._clock() + self._cooldown
                log.warning("provider %s failed: %s", name, exc)
                if started:
                    raise
                errors.append(str(exc))
            finally:
                await gen.aclose()
        raise ProviderUnavailableError("router", "all providers failed: " + "; ".join(errors))
