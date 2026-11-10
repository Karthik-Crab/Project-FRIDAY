"""Local speech recognition with Faster-Whisper (CTranslate2).

Status: runs on the target Windows laptop (first hardware run, 2026-11-10).
On that CPU the cost is dominated by the model size, not the clip length:
``small.en`` took about 2 s per utterance, hence ``base.en`` as the default.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import numpy as np

from friday.core.errors import VoiceError
from friday.voice.base import Transcript

log = logging.getLogger(__name__)


def default_cpu_threads() -> int:
    """Physical cores (assuming SMT), capped: more threads stop helping."""
    return max(1, min(8, (os.cpu_count() or 4) // 2))


def _load_model(model_size: str, device: str, compute_type: str, cpu_threads: int) -> Any:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise VoiceError("faster-whisper is not installed: pip install friday[voice]") from exc
    kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
        "cpu_threads": cpu_threads or default_cpu_threads(),
    }
    try:
        # Use the cached copy without touching the network, so startup works
        # offline; only download when the model is not on disk yet.
        return WhisperModel(model_size, local_files_only=True, **kwargs)
    except Exception:  # pragma: no cover - depends on the local cache
        log.info("speech model %s not cached yet; downloading", model_size)
        return WhisperModel(model_size, **kwargs)


def pcm16_to_float32(pcm: bytes) -> np.ndarray[Any, np.dtype[np.float32]]:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


class FasterWhisperSTT:
    def __init__(
        self,
        model_size: str = "base.en",
        device: str = "auto",
        compute_type: str = "default",
        language: str | None = "en",
        beam_size: int = 1,
        max_no_speech_prob: float = 0.6,
        min_avg_logprob: float = -1.0,
        cpu_threads: int = 0,  # 0 = one per physical core, up to 8
        vocabulary: tuple[str, ...] = (),
        model: Any = None,  # injectable for tests
    ) -> None:
        if model is None:
            model = _load_model(model_size, device, compute_type, cpu_threads)
        self._model = model
        # Names and jargon Whisper should expect ("Friday", not "Freddy").
        self._prompt = ", ".join(vocabulary) + "." if vocabulary else None
        self._language = language
        self._beam = beam_size
        self._max_no_speech = max_no_speech_prob
        self._min_logprob = min_avg_logprob

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

    async def warm_up(self) -> None:
        """Run once on silence so the first real utterance is not the slow one."""
        await self.transcribe(b"\x00\x00" * 8000, 16_000)

    def _run(self, audio: np.ndarray[Any, np.dtype[np.float32]]) -> Transcript:
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=self._beam,
            vad_filter=False,  # we already segmented the utterance
            condition_on_previous_text=False,
            initial_prompt=self._prompt,
        )
        # ``segments`` is a lazy generator: iterating it does the actual work.
        # Whisper invents plausible text ("Thank you.") on noise. Its own scores
        # say when: a segment that is probably not speech AND low-confidence is
        # dropped. Segments without scores (other backends, stubs) are kept.
        kept = []
        for seg in segments:
            no_speech = getattr(seg, "no_speech_prob", 0.0)
            logprob = getattr(seg, "avg_logprob", 0.0)
            if no_speech > self._max_no_speech and logprob < self._min_logprob:
                continue
            kept.append(seg.text.strip())
        text = " ".join(t for t in kept if t).strip()
        return Transcript(text=text, language=getattr(info, "language", None))
