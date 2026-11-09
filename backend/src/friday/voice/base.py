"""Voice component contracts.

Every engine (Whisper, Kokoro, Piper, a cloud voice, a microphone...) plugs in
behind one of these protocols. The voice pipeline only talks to protocols, so
replacing Faster-Whisper with another recogniser touches one adapter file.

Audio conventions:
* Capture: 16-bit signed little-endian PCM, mono. ``AudioFrame`` is one short
  frame (typically 20-32 ms) as delivered by the device.
* Playback: ``AudioChunk`` is 16-bit PCM mono at whatever rate the TTS engine
  produces; the sink is responsible for resampling if the device needs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

SAMPLE_WIDTH = 2  # bytes, int16


@dataclass(frozen=True, slots=True)
class AudioFrame:
    pcm: bytes
    sample_rate: int

    @property
    def duration_ms(self) -> float:
        return len(self.pcm) / SAMPLE_WIDTH / self.sample_rate * 1000.0


@dataclass(frozen=True, slots=True)
class AudioChunk:
    pcm: bytes
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / SAMPLE_WIDTH / self.sample_rate


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None


@runtime_checkable
class AudioSource(Protocol):
    def frames(self) -> AsyncIterator[AudioFrame]: ...


@runtime_checkable
class AudioSink(Protocol):
    async def play(self, chunk: AudioChunk) -> float:
        """Play ``chunk`` and return when done or stopped.

        Returns the fraction of the chunk that was actually played (1.0 when
        it finished). The pipeline uses this to know what the user heard.
        """
        ...

    def stop(self) -> None:
        """Stop playback immediately. Safe to call when idle."""
        ...


@runtime_checkable
class VoiceActivityDetector(Protocol):
    def speech_probability(self, frame: AudioFrame) -> float: ...

    def reset(self) -> None: ...


@runtime_checkable
class SpeechToText(Protocol):
    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript: ...


@runtime_checkable
class TextToSpeech(Protocol):
    @property
    def sample_rate(self) -> int: ...

    async def synthesize(self, text: str) -> AudioChunk: ...
