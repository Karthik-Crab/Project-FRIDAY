"""Microphone capture and speaker playback via PortAudio (``sounddevice``).

Status: NOT yet verified on the target Windows hardware. See docs/ROADMAP.md,
milestone M1.3. Known gap: no acoustic echo cancellation -- use a headset
until AEC lands (docs/RISKS.md, R1).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any

from friday.core.errors import VoiceError
from friday.voice.base import AudioChunk, AudioFrame


def _sd() -> Any:
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise VoiceError("sounddevice is not installed: pip install friday[voice]") from exc
    return sounddevice


class MicrophoneSource:
    def __init__(
        self, device: int | str | None = None, sample_rate: int = 16_000, frame_ms: int = 20
    ) -> None:
        self._device = device
        self._rate = sample_rate
        self._blocksize = sample_rate * frame_ms // 1000

    async def frames(self) -> AsyncIterator[AudioFrame]:
        sd = _sd()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)

        def callback(indata: Any, frames: int, time: Any, status: Any) -> None:
            data = bytes(indata)

            def put() -> None:
                if queue.full():  # consumer is stalled: drop oldest, keep latency bounded
                    queue.get_nowait()
                queue.put_nowait(data)

            loop.call_soon_threadsafe(put)

        with sd.RawInputStream(
            samplerate=self._rate,
            blocksize=self._blocksize,
            device=self._device,
            channels=1,
            dtype="int16",
            callback=callback,
        ):
            while True:
                yield AudioFrame(await queue.get(), self._rate)


class SpeakerSink:
    """Plays one chunk at a time on a PortAudio output stream; ``stop`` is immediate."""

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._stop = threading.Event()

    async def play(self, chunk: AudioChunk) -> float:
        sd = _sd()
        self._stop.clear()
        loop = asyncio.get_running_loop()
        done: asyncio.Future[float] = loop.create_future()
        data = chunk.pcm
        total = len(data)
        pos = 0

        def finish(fraction: float) -> None:
            if not done.done():
                done.set_result(fraction)

        def callback(outdata: Any, frames: int, time: Any, status: Any) -> None:
            nonlocal pos
            n = frames * 2
            if self._stop.is_set():
                outdata[:] = b"\x00" * len(outdata)
                raise sd.CallbackStop
            piece = data[pos : pos + n]
            outdata[: len(piece)] = piece
            if len(piece) < n:
                outdata[len(piece) :] = b"\x00" * (n - len(piece))
                pos = total
                raise sd.CallbackStop
            pos += n

        stream = sd.RawOutputStream(
            samplerate=chunk.sample_rate,
            device=self._device,
            channels=1,
            dtype="int16",
            callback=callback,
            finished_callback=lambda: loop.call_soon_threadsafe(
                finish, min(1.0, pos / total) if total else 1.0
            ),
        )
        with stream:
            try:
                return await done
            finally:
                self._stop.set()

    def stop(self) -> None:
        self._stop.set()
