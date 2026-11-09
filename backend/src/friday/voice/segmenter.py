"""Turn detection: turns a stream of speech/non-speech frames into utterances.

Simple, explicit state machine with hysteresis:

* Speech must persist ``start_ms`` before an utterance starts (ignores clicks).
* ``preroll_ms`` of audio before the start is kept so the first syllable is
  not clipped.
* The utterance ends after ``end_silence_ms`` of continuous non-speech. This is
  the main knob trading responsiveness against cutting the user off mid-pause.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from friday.voice.base import AudioFrame


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    start_ms: float = 120.0
    end_silence_ms: float = 700.0
    preroll_ms: float = 300.0
    min_utterance_ms: float = 250.0
    max_utterance_ms: float = 30_000.0


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    pass


@dataclass(frozen=True, slots=True)
class UtteranceEnded:
    pcm: bytes
    sample_rate: int
    speech_ms: float


@dataclass(frozen=True, slots=True)
class UtteranceDiscarded:
    """Speech started but was too short to be worth transcribing."""

    speech_ms: float


SegmentEvent = SpeechStarted | UtteranceEnded | UtteranceDiscarded


class UtteranceSegmenter:
    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self.config = config or SegmenterConfig()
        self._preroll: deque[AudioFrame] = deque()
        self._preroll_ms = 0.0
        self._pending: list[AudioFrame] = []  # candidate speech before start
        self._pending_speech_ms = 0.0
        self._buffer: list[AudioFrame] = []
        self.in_speech = False
        self.speech_ms = 0.0  # voiced audio in the current utterance
        self._silence_ms = 0.0
        self._total_ms = 0.0

    def reset(self) -> None:
        self.__init__(self.config)  # type: ignore[misc]

    def process(self, frame: AudioFrame, is_speech: bool) -> list[SegmentEvent]:
        dur = frame.duration_ms
        if not self.in_speech:
            return self._process_idle(frame, is_speech, dur)

        self._buffer.append(frame)
        self._total_ms += dur
        if is_speech:
            self.speech_ms += dur
            self._silence_ms = 0.0
        else:
            self._silence_ms += dur
        if (
            self._silence_ms >= self.config.end_silence_ms
            or self._total_ms >= self.config.max_utterance_ms
        ):
            return [self._finish()]
        return []

    def flush(self) -> list[SegmentEvent]:
        """End any utterance in progress (e.g. the audio source closed)."""
        return [self._finish()] if self.in_speech else []

    def _process_idle(self, frame: AudioFrame, is_speech: bool, dur: float) -> list[SegmentEvent]:
        if is_speech:
            self._pending.append(frame)
            self._pending_speech_ms += dur
            if self._pending_speech_ms >= self.config.start_ms:
                self.in_speech = True
                self._buffer = list(self._preroll) + self._pending
                self._total_ms = sum(f.duration_ms for f in self._buffer)
                self.speech_ms = self._pending_speech_ms
                self._silence_ms = 0.0
                self._pending, self._pending_speech_ms = [], 0.0
                self._preroll.clear()
                self._preroll_ms = 0.0
                return [SpeechStarted()]
            return []
        # A non-speech frame breaks a candidate start; move it to pre-roll.
        for f in [*self._pending, frame]:
            self._preroll.append(f)
            self._preroll_ms += f.duration_ms
        self._pending, self._pending_speech_ms = [], 0.0
        while self._preroll and self._preroll_ms > self.config.preroll_ms:
            self._preroll_ms -= self._preroll.popleft().duration_ms
        return []

    def _finish(self) -> SegmentEvent:
        frames, speech_ms = self._buffer, self.speech_ms
        self._buffer = []
        self.in_speech = False
        self.speech_ms = 0.0
        self._silence_ms = 0.0
        self._total_ms = 0.0
        if speech_ms < self.config.min_utterance_ms:
            return UtteranceDiscarded(speech_ms=speech_ms)
        return UtteranceEnded(
            pcm=b"".join(f.pcm for f in frames),
            sample_rate=frames[0].sample_rate,
            speech_ms=speech_ms,
        )
