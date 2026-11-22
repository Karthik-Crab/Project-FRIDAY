"""Microphone and speaker of the browser that switched voice mode on.

The page captures the microphone with the browser's own echo cancellation,
noise suppression and level control (the same processing video calls use),
turns it into 16 kHz 16-bit mono and sends 20 ms frames over the app's live
connection as binary messages. FRIDAY's speech goes back the same way and the
page plays it. So the phone, laptop or PC in your hand is the one that listens
and speaks, and FRIDAY no longer hears herself through the speakers.

Messages to the page (binary, first byte says what):
* ``A`` + sample rate (4 bytes, big-endian) + 16-bit PCM: play this next.
* ``S``: stop playing now (FRIDAY was interrupted).
* ``X``: this page no longer has voice mode (switched off or taken over).

Playback progress is tracked here on the clock rather than reported by the
page: on a home network the delay is a few milliseconds, and it keeps the page
simple.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from friday.core.errors import VoiceError
from friday.voice.base import AudioChunk, AudioFrame

RATE = 16_000
_LEAD = 0.03  # the page starts each sound this far ahead, to avoid clicks


@dataclass(slots=True)
class _Playing:
    start: float
    end: float
    done: asyncio.Future[float]


class BrowserAudio:
    """``AudioSource`` and ``AudioSink`` for one browser page."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]], owner: object = None) -> None:
        self._send = send
        self.owner = owner  # the live connection this belongs to
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=250)
        self._listening = False
        self._closed = False
        self._end = 0.0
        self._playing: list[_Playing] = []
        self._lock = asyncio.Lock()
        self._sends: set[asyncio.Task[None]] = set()

    # Microphone -------------------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        """A frame from the page. Ignored until the session listens, so audio
        from the seconds voice mode takes to start is not answered later."""
        if not self._listening or self._closed or not pcm or len(pcm) % 2:
            return
        if self._queue.full():  # the session is stalled: keep the newest audio
            self._queue.get_nowait()
        self._queue.put_nowait(pcm)

    async def frames(self) -> AsyncIterator[AudioFrame]:
        self._listening = True
        while True:
            pcm = await self._queue.get()
            if pcm is None:
                raise VoiceError("the device that had voice mode went away")
            yield AudioFrame(pcm, RATE)

    # Speaker ----------------------------------------------------------------

    async def play(self, chunk: AudioChunk) -> float:
        if self._closed:
            return 0.0
        loop = asyncio.get_running_loop()
        now = loop.time()
        start = max(now + _LEAD, self._end)
        end = start + chunk.duration_s
        self._end = end
        item = _Playing(start, end, loop.create_future())
        self._playing.append(item)
        timer = loop.call_at(end, _finish, item.done)
        try:
            await self._transmit(b"A" + struct.pack(">I", chunk.sample_rate) + chunk.pcm)
            return await item.done
        except asyncio.CancelledError:
            self.stop()
            raise
        finally:
            timer.cancel()
            with contextlib.suppress(ValueError):
                self._playing.remove(item)

    def stop(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        for item in self._playing:
            if not item.done.done():
                span = item.end - item.start
                heard = (now - item.start) / span if span > 0 else 1.0
                item.done.set_result(min(1.0, max(0.0, heard)))
        self._end = 0.0
        if not self._closed:
            self._later(b"S")

    # The page ---------------------------------------------------------------

    def close(self, tell_page: bool = True) -> None:
        """Voice mode ended, or the page went away (``tell_page=False``)."""
        if self._closed:
            return
        if tell_page:
            with contextlib.suppress(RuntimeError):
                self._later(b"X", force=True)
        self._closed = True
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        for item in self._playing:
            if not item.done.done():
                item.done.set_result(0.0)

    @property
    def closed(self) -> bool:
        return self._closed

    def _later(self, data: bytes, force: bool = False) -> None:
        task = asyncio.get_running_loop().create_task(self._transmit(data, force))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)

    async def _transmit(self, data: bytes, force: bool = False) -> None:
        if self._closed and not force:
            return
        async with self._lock:
            with contextlib.suppress(Exception):  # a closed page is noticed elsewhere
                await self._send(data)


def _finish(done: asyncio.Future[float]) -> None:
    if not done.done():
        done.set_result(1.0)
