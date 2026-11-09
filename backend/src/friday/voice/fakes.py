"""Deterministic in-process voice components.

Used by the automated tests and by ``friday voice --simulate``. They exercise
the *real* VAD, segmenter, chunker and session logic; only the hardware and
the ML models are replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass

import numpy as np

from friday.voice.base import AudioChunk, AudioFrame, Transcript

SAMPLE_RATE = 16_000
FRAME_MS = 20


def tone(ms: float, amplitude: int = 8000, sample_rate: int = SAMPLE_RATE) -> bytes:
    n = int(sample_rate * ms / 1000)
    t = np.arange(n) / sample_rate
    return (amplitude * np.sin(2 * math.pi * 220 * t)).astype("<i2").tobytes()


def noise(ms: float, amplitude: int = 30, sample_rate: int = SAMPLE_RATE, seed: int = 0) -> bytes:
    n = int(sample_rate * ms / 1000)
    rng = np.random.default_rng(seed)
    return rng.integers(-amplitude, amplitude, n).astype("<i2").tobytes()


@dataclass(frozen=True, slots=True)
class Speak:
    """User speaks for ``ms`` milliseconds."""

    ms: float


@dataclass(frozen=True, slots=True)
class Silence:
    ms: float


@dataclass(frozen=True, slots=True)
class WaitFor:
    """Pause the microphone script until ``predicate()`` is true."""

    predicate: Callable[[], bool]
    timeout_s: float = 5.0


Step = Speak | Silence | WaitFor


class ScriptedAudioSource:
    """Plays a script of speech/silence as 20 ms frames.

    ``realtime`` scales wall-clock pacing: 1.0 is real time, 0.0 yields as
    fast as possible (still yielding to the event loop on every frame).
    """

    def __init__(self, steps: Iterable[Step], realtime: float = 0.0) -> None:
        self._steps = list(steps)
        self._realtime = realtime

    async def frames(self) -> AsyncIterator[AudioFrame]:
        frame_bytes = SAMPLE_RATE * FRAME_MS // 1000 * 2
        for i, step in enumerate(self._steps):
            if isinstance(step, WaitFor):
                deadline = asyncio.get_running_loop().time() + step.timeout_s
                while not step.predicate():
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError(f"WaitFor step {i} timed out")
                    # keep feeding silence while we wait, like a real mic
                    yield AudioFrame(noise(FRAME_MS, seed=i), SAMPLE_RATE)
                    await asyncio.sleep(FRAME_MS / 1000 * self._realtime or 0.001)
                continue
            pcm = tone(step.ms) if isinstance(step, Speak) else noise(step.ms, seed=i)
            for start in range(0, len(pcm), frame_bytes):
                yield AudioFrame(pcm[start : start + frame_bytes], SAMPLE_RATE)
                await asyncio.sleep(FRAME_MS / 1000 * self._realtime)


class ScriptedSTT:
    """Returns the next transcript from a list for each utterance."""

    def __init__(self, transcripts: Iterable[str], delay: float = 0.0) -> None:
        self._queue = list(transcripts)
        self._delay = delay
        self.calls = 0

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return Transcript(self._queue.pop(0) if self._queue else "")


class FakeTTS:
    """Silent audio whose length is proportional to the text (~15 chars/s)."""

    def __init__(self, sample_rate: int = 24_000, chars_per_second: float = 15.0) -> None:
        self._rate = sample_rate
        self._cps = chars_per_second
        self.spoken: list[str] = []

    @property
    def sample_rate(self) -> int:
        return self._rate

    async def synthesize(self, text: str) -> AudioChunk:
        self.spoken.append(text)
        seconds = max(0.05, len(text) / self._cps)
        return AudioChunk(b"\x00\x00" * int(self._rate * seconds), self._rate)


class FakeAudioSink:
    """Pretends to play audio; ``speed`` > 1 plays faster than real time."""

    def __init__(self, speed: float = 1.0) -> None:
        self._speed = speed
        self._stop = asyncio.Event()
        self.played: list[float] = []  # fraction played per chunk
        self.is_playing = False
        self.stop_calls = 0

    async def play(self, chunk: AudioChunk) -> float:
        self._stop.clear()
        duration = chunk.duration_s / self._speed
        loop = asyncio.get_running_loop()
        start = loop.time()
        self.is_playing = True
        fraction = 1.0
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=duration)
        finally:  # also runs when the turn is cancelled mid-chunk
            self.is_playing = False
            fraction = min(1.0, (loop.time() - start) / duration) if duration else 1.0
            self.played.append(fraction)
        return fraction

    def stop(self) -> None:
        self.stop_calls += 1
        self._stop.set()
