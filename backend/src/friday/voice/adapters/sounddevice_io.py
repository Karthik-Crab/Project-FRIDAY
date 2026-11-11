"""Microphone capture and speaker playback via PortAudio (``sounddevice``).

Status: NOT yet verified on the target Windows hardware. See docs/ROADMAP.md,
milestone M1.3. Known gap: no acoustic echo cancellation -- use a headset
until AEC lands (docs/RISKS.md, R1).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from friday.core.errors import VoiceError
from friday.voice.base import AudioChunk, AudioFrame
from friday.voice.playback import PlaybackBuffer


def _sd() -> Any:
    try:
        import sounddevice
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise VoiceError("sounddevice is not installed: pip install friday[voice]") from exc
    return sounddevice


def describe_device(device: int | str | None, kind: str) -> str:
    """Name of the device that will be used, for the start-up line. A Bluetooth
    headset used as the microphone shows up here as "Hands-Free": telephone
    quality, which speech recognition copes with badly."""
    try:
        info = _sd().query_devices(device, kind)
        return f"{info['name']} ({info['default_samplerate']:.0f} Hz)"
    except Exception as exc:  # a missing device is reported when it is opened
        return f"unknown ({type(exc).__name__})"


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
    """Gapless playback on one long-lived output stream.

    The stream stays open for the whole session and pulls from a
    ``PlaybackBuffer``, so sentences join without the click/pause of opening a
    device per sentence, and ``stop`` takes effect on the next audio callback
    (a few milliseconds) because it just empties the buffer.

    Besides the chunk-at-a-time ``play`` used by the cascaded pipeline, it has
    ``write``/``flush`` for backends that stream audio in small pieces
    (speech-to-speech).
    """

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._buffer = PlaybackBuffer()
        self._stream: Any = None
        self._rate = 0

    def _ensure_stream(self, sample_rate: int) -> None:
        if self._stream is not None and self._rate == sample_rate:
            return
        self.close()
        sd = _sd()

        def callback(outdata: Any, frames: int, time: Any, status: Any) -> None:
            outdata[:] = self._buffer.read(len(outdata))

        self._stream = sd.RawOutputStream(
            samplerate=sample_rate,
            device=self._device,
            channels=1,
            dtype="int16",
            latency="low",
            callback=callback,
        )
        self._rate = sample_rate
        self._stream.start()

    async def play(self, chunk: AudioChunk) -> float:
        self._ensure_stream(chunk.sample_rate)
        loop = asyncio.get_running_loop()
        done: asyncio.Future[float] = loop.create_future()

        def finish(fraction: float) -> None:
            if not done.done():
                done.set_result(fraction)

        def on_done(fraction: float) -> None:  # runs on the audio thread
            loop.call_soon_threadsafe(finish, fraction)

        self._buffer.write(chunk.pcm, on_done=on_done)
        try:
            return await done
        except asyncio.CancelledError:
            self._buffer.clear()
            raise

    def write(self, pcm: bytes, sample_rate: int) -> None:
        """Queue audio without waiting (streaming backends)."""
        self._ensure_stream(sample_rate)
        self._buffer.write(pcm)

    def flush(self) -> int:
        """Drop everything not yet played; returns the number of bytes dropped."""
        return self._buffer.clear()

    @property
    def pending_bytes(self) -> int:
        return self._buffer.pending

    def stop(self) -> None:
        self._buffer.clear()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.abort()
            self._stream.close()
            self._stream = None
