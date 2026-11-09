"""Local speech recognition with Faster-Whisper (CTranslate2).

Status: written against the faster-whisper 1.x API; NOT yet verified on the
target Windows hardware. See docs/ROADMAP.md, milestone M1.3.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np

from friday.core.errors import VoiceError
from friday.voice.base import Transcript


def pcm16_to_float32(pcm: bytes) -> np.ndarray[Any, np.dtype[np.float32]]:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class FasterWhisperSTT:
    def __init__(
        self,
        model_size: str = "small.en",
        device: str = "auto",
        compute_type: str = "default",
        language: str | None = "en",
        beam_size: int = 1,
        model: Any = None,  # injectable for tests
    ) -> None:
        if model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:  # pragma: no cover - depends on extras
                raise VoiceError(
                    "faster-whisper is not installed: pip install friday[voice]"
                ) from exc
            model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._model = model
        self._language = language
        self._beam = beam_size

    async def transcribe(self, pcm: bytes, sample_rate: int) -> Transcript:
        if sample_rate != 16_000:
            raise VoiceError(f"Whisper expects 16 kHz audio, got {sample_rate}")
        audio = pcm16_to_float32(pcm)
        try:
            return await asyncio.to_thread(self._run, audio)
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"transcription failed: {type(exc).__name__}: {exc}") from exc

    def _run(self, audio: np.ndarray[Any, np.dtype[np.float32]]) -> Transcript:
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam,
            vad_filter=False,  # we already segmented the utterance
            condition_on_previous_text=False,
        )
        # ``segments`` is a lazy generator: iterating it does the actual work.
        text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(text=text, language=getattr(info, "language", None))
