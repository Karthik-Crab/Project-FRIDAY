"""The full-duplex voice session.

Data flow (each arrow is a protocol, so every box is replaceable):

    AudioSource -> VAD -> UtteranceSegmenter -> SpeechToText
        -> ConversationEngine (text deltas) -> SentenceChunker -> SpeechSanitizer
        -> TextToSpeech -> AudioSink

Concurrency model (single asyncio loop):

* The *frame loop* (``run``) never blocks: it scores each mic frame and runs
  the segmenter. It keeps listening while FRIDAY talks, which is what makes
  interruption possible.
* An *utterance worker* transcribes finished utterances one at a time and
  decides what they mean (new turn, stop, backchannel).
* At most one *turn task* is active. Inside it, a producer streams the reply,
  chunks and synthesises sentences into a small queue while the consumer plays
  them, so sentence N+1 is synthesised while sentence N is playing.

Barge-in: if the user speaks for ``barge_in_min_ms`` while a turn is active,
the turn is cancelled and playback stops within one frame. The engine's
history is rewritten to what the user actually heard, so FRIDAY never assumes
they heard the rest of the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import StrEnum

from friday.conversation.engine import ConversationEngine
from friday.core.errors import FridayError
from friday.voice.base import (
    AudioChunk,
    AudioFrame,
    AudioSink,
    AudioSource,
    SpeechToText,
    TextToSpeech,
    VoiceActivityDetector,
)
from friday.voice.chunker import SentenceChunker
from friday.voice.control import ControlIntent, classify
from friday.voice.sanitize import SpeechSanitizer
from friday.voice.segmenter import (
    SegmenterConfig,
    SpeechStarted,
    UtteranceEnded,
    UtteranceSegmenter,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    speech_threshold: float = 0.5
    # Higher while FRIDAY is talking, so her own voice leaking from speakers
    # into the mic is less likely to trigger a false interruption.
    speech_threshold_while_speaking: float = 0.85
    barge_in_min_ms: float = 400.0
    tts_queue_size: int = 2
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)


class VoiceState(StrEnum):
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"
    SPEAKING = "speaking"


# ---- events (consumed by CLI now, WebSocket/UI later) ----------------------


@dataclass(frozen=True, slots=True)
class StateChanged:
    state: VoiceState


@dataclass(frozen=True, slots=True)
class UserSaid:
    text: str


@dataclass(frozen=True, slots=True)
class AssistantSaid:
    text: str


@dataclass(frozen=True, slots=True)
class Interrupted:
    reason: str  # "barge_in" | "stop"
    heard: str


@dataclass(slots=True)
class TurnMetrics:
    """Latencies in ms, measured from the moment the utterance was endpointed.

    Perceived latency is roughly ``endpoint_ms + first_audio_ms``.
    """

    endpoint_ms: float
    stt_ms: float = 0.0
    first_token_ms: float | None = None
    first_audio_ms: float | None = None
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class TurnFinished:
    metrics: TurnMetrics
    interrupted: bool


@dataclass(frozen=True, slots=True)
class VoiceErrorEvent:
    message: str


VoiceEvent = StateChanged | UserSaid | AssistantSaid | Interrupted | TurnFinished | VoiceErrorEvent

_END = object()


class VoiceSession:
    def __init__(
        self,
        engine: ConversationEngine,
        source: AudioSource,
        vad: VoiceActivityDetector,
        stt: SpeechToText,
        tts: TextToSpeech,
        sink: AudioSink,
        config: VoiceConfig | None = None,
        on_event: Callable[[VoiceEvent], None] | None = None,
    ) -> None:
        self.engine = engine
        self.source = source
        self.vad = vad
        self.stt = stt
        self.tts = tts
        self.sink = sink
        self.config = config or VoiceConfig()
        self.segmenter = UtteranceSegmenter(self.config.segmenter)
        self._on_event = on_event
        self._utterances: asyncio.Queue[tuple[UtteranceEnded, float]] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None
        self._interrupt_reason: str | None = None
        self.state = VoiceState.LISTENING
        self.speaking = False

    # ---- public ---------------------------------------------------------

    async def run(self) -> None:
        worker = asyncio.create_task(self._utterance_worker(), name="friday-utterances")
        try:
            async for frame in self.source.frames():
                self._on_frame(frame)
                if worker.done():  # surface unexpected worker crashes
                    worker.result()
            for event in self.segmenter.flush():
                self._handle_segment_event(event)
            await self._utterances.join()
            if self._turn is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._turn
        finally:
            worker.cancel()
            await self._cancel_turn("shutdown")
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    def interrupt(self, reason: str = "barge_in") -> None:
        """Stop the current turn now (also usable from a UI button)."""
        if self._turn is not None and not self._turn.done():
            self._interrupt_reason = reason
            self.sink.stop()
            self._turn.cancel()

    # ---- frame loop -----------------------------------------------------

    def _on_frame(self, frame: AudioFrame) -> None:
        prob = self.vad.speech_probability(frame)
        threshold = (
            self.config.speech_threshold_while_speaking
            if self.speaking
            else self.config.speech_threshold
        )
        for event in self.segmenter.process(frame, prob >= threshold):
            self._handle_segment_event(event)
        if (
            self.segmenter.in_speech
            and self._turn_active()
            and self.segmenter.speech_ms >= self.config.barge_in_min_ms
        ):
            log.debug("barge-in after %.0f ms of speech", self.segmenter.speech_ms)
            self.interrupt("barge_in")

    def _handle_segment_event(self, event: object) -> None:
        if isinstance(event, SpeechStarted):
            if not self._turn_active():
                self._set_state(VoiceState.USER_SPEAKING)
        elif isinstance(event, UtteranceEnded):
            self._utterances.put_nowait((event, asyncio.get_running_loop().time()))
        elif not self._turn_active():
            self._set_state(VoiceState.LISTENING)

    # ---- utterance worker ----------------------------------------------

    async def _utterance_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            utterance, ended_at = await self._utterances.get()
            try:
                transcript = await self.stt.transcribe(utterance.pcm, utterance.sample_rate)
                stt_ms = (loop.time() - ended_at) * 1000
                text = transcript.text.strip()
                if not text:
                    if not self._turn_active():
                        self._set_state(VoiceState.LISTENING)
                    continue
                self._emit(UserSaid(text))
                intent = classify(text)
                if self._turn_active():
                    if intent is ControlIntent.BACKCHANNEL:
                        continue
                    await self._cancel_turn("stop" if intent is ControlIntent.STOP else "barge_in")
                if intent is ControlIntent.STOP:
                    self._set_state(VoiceState.LISTENING)
                    continue
                metrics = TurnMetrics(
                    endpoint_ms=self.config.segmenter.end_silence_ms, stt_ms=stt_ms
                )
                self._interrupt_reason = None
                self._turn = asyncio.create_task(
                    self._run_turn(text, ended_at, metrics), name="friday-turn"
                )
            except FridayError as exc:
                log.error("speech recognition failed: %s", exc)
                self._emit(VoiceErrorEvent(f"speech recognition failed: {exc}"))
                self._set_state(VoiceState.LISTENING)
            finally:
                self._utterances.task_done()

    # ---- turn -----------------------------------------------------------

    def _turn_active(self) -> bool:
        return self._turn is not None and not self._turn.done()

    async def _cancel_turn(self, reason: str) -> None:
        if self._turn_active():
            self.interrupt(reason)
        if self._turn is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn

    async def _run_turn(self, text: str, ended_at: float, metrics: TurnMetrics) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self.config.tts_queue_size)
        producer = asyncio.create_task(self._produce(text, queue, ended_at, metrics))
        heard: list[str] = []
        playing: tuple[str, float, float] | None = None  # text, start, duration
        self._set_state(VoiceState.THINKING)
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, tuple)
                chunk_text, audio = item
                assert isinstance(audio, AudioChunk)
                if metrics.first_audio_ms is None:
                    metrics.first_audio_ms = (loop.time() - ended_at) * 1000
                self.speaking = True
                self._set_state(VoiceState.SPEAKING)
                playing = (chunk_text, loop.time(), audio.duration_s)
                await self.sink.play(audio)
                playing = None
                heard.append(chunk_text)
                self._emit(AssistantSaid(chunk_text))
            await producer
            metrics.provider = self._provider_name()
            self._emit(TurnFinished(metrics, interrupted=False))
        except asyncio.CancelledError:
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer
            if playing is not None:
                chunk_text, started, duration = playing
                fraction = (loop.time() - started) / duration if duration > 0 else 1.0
                heard.append(_truncate_words(chunk_text, fraction))
            heard_text = " ".join(h for h in heard if h)
            self.engine.record_interruption(heard_text)
            self._emit(Interrupted(reason=self._interrupt_reason or "cancelled", heard=heard_text))
            metrics.provider = self._provider_name()
            self._emit(TurnFinished(metrics, interrupted=True))
            raise
        except FridayError as exc:
            log.error("turn failed: %s", exc)
            self._emit(VoiceErrorEvent(str(exc)))
        except Exception:
            log.exception("unexpected error in voice turn")
            self._emit(VoiceErrorEvent("internal error during reply"))
        finally:
            self.sink.stop()
            self.speaking = False
            self._set_state(
                VoiceState.USER_SPEAKING if self.segmenter.in_speech else VoiceState.LISTENING
            )

    async def _produce(
        self, text: str, queue: asyncio.Queue[object], ended_at: float, metrics: TurnMetrics
    ) -> None:
        loop = asyncio.get_running_loop()
        chunker = SentenceChunker()
        sanitizer = SpeechSanitizer()

        async def speak(piece: str) -> None:
            cleaned = sanitizer.clean(piece)
            if cleaned:
                audio = await self.tts.synthesize(cleaned)
                await queue.put((cleaned, audio))

        try:
            async with aclosing(self.engine.respond(text)) as stream:
                async for delta in stream:
                    if metrics.first_token_ms is None:
                        metrics.first_token_ms = (loop.time() - ended_at) * 1000
                    for piece in chunker.feed(delta):
                        await speak(piece)
            for piece in chunker.flush():
                await speak(piece)
            await queue.put(_END)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # hand the failure to the consumer
            await queue.put(exc)

    def _provider_name(self) -> str | None:
        info = self.engine.last_turn.provider
        return info.name if info else None

    # ---- events ---------------------------------------------------------

    def _set_state(self, state: VoiceState) -> None:
        if state is not self.state:
            self.state = state
            self._emit(StateChanged(state))

    def _emit(self, event: VoiceEvent) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # a broken UI listener must not kill the voice loop
                log.exception("voice event listener failed")


def _truncate_words(text: str, fraction: float) -> str:
    words = text.split()
    keep = int(len(words) * max(0.0, min(1.0, fraction)))
    return " ".join(words[:keep])
