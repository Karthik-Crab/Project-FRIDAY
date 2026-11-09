"""Local neural TTS with kokoro-onnx (Apache-2.0 model weights).

Status: written against the kokoro-onnx 0.4 API; NOT yet verified on the
target Windows hardware. See docs/ROADMAP.md, milestone M1.3.

Model files (``kokoro-v1.0.onnx``, ``voices-v1.0.bin``) are downloaded
separately; paths come from config.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from friday.core.errors import VoiceError
from friday.voice.base import AudioChunk


def float_to_pcm16(samples: Any) -> bytes:
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


class KokoroTTS:
    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        lang: str = "en-us",
        engine: Any = None,  # injectable for tests
    ) -> None:
        if engine is None:
            try:
                from kokoro_onnx import Kokoro
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise VoiceError("kokoro-onnx is not installed: pip install friday[voice]") from exc
            engine = Kokoro(model_path, voices_path)
        self._engine = engine
        self.voice = voice
        self.speed = speed
        self._lang = lang
        self._rate = 24_000

    @property
    def sample_rate(self) -> int:
        return self._rate

    async def synthesize(self, text: str) -> AudioChunk:
        try:
            samples, rate = await asyncio.to_thread(
                self._engine.create, text, voice=self.voice, speed=self.speed, lang=self._lang
            )
        except Exception as exc:
            raise VoiceError(f"speech synthesis failed: {type(exc).__name__}: {exc}") from exc
        self._rate = int(rate)
        return AudioChunk(float_to_pcm16(samples), int(rate))
