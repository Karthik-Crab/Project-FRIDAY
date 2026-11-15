"""An expressive voice served by a separate local process, with a fallback.

The speech model (Chatterbox, see ``backend/servers/chatterbox_server.py``)
runs in its own environment on the graphics card. When that service is not
running, or the card is busy with something else, FRIDAY falls back to the
built-in voice instead of going silent.

Status: covered by tests against a stand-in server. Never run against the real
model.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from friday.core.errors import VoiceError
from friday.voice.base import AudioChunk, TextToSpeech
from friday.voice.sanitize import strip_expression_tags

log = logging.getLogger(__name__)

# Sounds the expressive voice can make when the text contains the mark.
EXPRESSION_TAGS: tuple[str, ...] = ("laugh", "chuckle", "sigh", "gasp")


class RemoteTTS:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 20.0,
        tags: tuple[str, ...] = EXPRESSION_TAGS,
        client: httpx.AsyncClient | None = None,  # injectable for tests
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self.expression_tags = tags
        self._rate = 24_000

    @property
    def sample_rate(self) -> int:
        return self._rate

    async def health(self) -> dict[str, object]:
        try:
            response = await self._client.get("/health", timeout=3.0)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VoiceError(f"voice service not reachable: {type(exc).__name__}") from exc
        if not isinstance(body, dict):
            raise VoiceError("voice service gave an unexpected answer")
        self._rate = int(body.get("sample_rate", self._rate))
        return body

    async def synthesize(self, text: str) -> AudioChunk:
        try:
            response = await self._client.post("/speak", json={"text": text})
            response.raise_for_status()
            rate = int(response.headers["X-Sample-Rate"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise VoiceError(f"voice service failed: {type(exc).__name__}") from exc
        pcm = response.content
        if not pcm or len(pcm) % 2:
            raise VoiceError("voice service returned no usable audio")
        self._rate = rate
        return AudioChunk(pcm, rate)

    async def warm_up(self) -> None:
        await self.health()
        await self.synthesize("Ready.")


class FallbackTTS:
    """Use ``primary``; when it fails, use ``fallback`` and try again later."""

    def __init__(
        self,
        primary: TextToSpeech,
        fallback: TextToSpeech,
        retry_after_s: float = 30.0,
        on_change: Callable[[bool], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._retry_after_s = retry_after_s
        self._on_change = on_change
        self._clock = clock
        self._down_until = 0.0
        self.on_primary = True
        self.expression_tags: tuple[str, ...] = tuple(getattr(primary, "expression_tags", ()))

    @property
    def sample_rate(self) -> int:
        return (self._primary if self.on_primary else self._fallback).sample_rate

    def _switch(self, on_primary: bool) -> None:
        if on_primary != self.on_primary:
            self.on_primary = on_primary
            if self._on_change:
                self._on_change(on_primary)

    async def synthesize(self, text: str) -> AudioChunk:
        if self._clock() >= self._down_until:
            try:
                audio = await self._primary.synthesize(text)
            except VoiceError as exc:
                log.warning("expressive voice unavailable, using the built-in voice: %s", exc)
                self._down_until = self._clock() + self._retry_after_s
                self._switch(False)
            else:
                self._switch(True)
                return audio
        # The built-in voice would read the marks out as words.
        plain = strip_expression_tags(text, self.expression_tags)
        return await self._fallback.synthesize(plain or text)

    async def warm_up(self) -> None:
        for voice in (self._fallback, self._primary):
            warm = getattr(voice, "warm_up", None)
            if warm is None:
                continue
            try:
                await warm()
            except VoiceError as exc:
                if voice is self._fallback:
                    raise
                log.warning("expressive voice unavailable at start-up: %s", exc)
                self._down_until = self._clock() + self._retry_after_s
                self._switch(False)
