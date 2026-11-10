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

Barge-in is two-stage, so noise and "mm-hmm" do not kill an answer:

1. *Hold*: when the user has spoken for ``barge_in_min_ms`` while a turn is
   active, playback is silenced within one frame and the turn is paused.
2. *Decide*, once the utterance is transcribed: real speech cancels the turn
   (history is rewritten to what the user actually heard) and starts a new
   one; "stop" cancels without a reply; an empty transcript or a backchannel
   resumes the answer from the sentence that was cut.

If the user speaks again *before the reply has produced any audio*, they were
not interrupting, they were still mid-thought (the pause was just longer than
the endpoint). The pending turn is held like any other, and once the transcript
is known: real words are merged with the pending text into one new turn; noise,
"okay" or "hmm" let the pending turn carry on untouched. Merging is capped
(``max_merges``), so a room full of other voices cannot postpone the reply for
ever.

Acknowledgements: people say "Hmm" or "Okay" the moment they start thinking.
When clips are configured, one is played as soon as a turn starts, which fills
the silence while the model and the voice produce the real first sentence.

Stopping never relies on task cancellation alone: an explicit flag is checked
after every chunk, because ``asyncio.wait_for`` before Python 3.12 (and any
library that catches ``CancelledError``) can swallow a cancellation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from enum import StrEnum

from friday.conversation.engine import ConversationEngine
from friday.core.errors import FridayError
from friday.persona.persona import Mode
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
from friday.voice.control import ControlIntent, classify, is_filler
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
    # Slightly higher while FRIDAY is talking. Kept modest: on real hardware a
    # high value (0.85) stopped a laptop microphone from ever registering an
    # interruption. Speakers without echo cancellation need it raised.
    speech_threshold_while_speaking: float = 0.6
    barge_in_min_ms: float = 300.0
    tts_queue_size: int = 2
    # How many times a pending reply may be restarted because the user (or a
    # voice in the background) spoke again before it produced any audio.
    max_merges: int = 2
    # Short sounds played the moment a turn starts, in companion mode, for
    # utterances of at least ``ack_min_words`` words. Empty: none.
    acknowledgements: tuple[str, ...] = ()
    ack_min_words: int = 4
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
    reason: str  # "barge_in" | "stop" | "merge" (user was still mid-thought)
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
    ack_ms: float | None = None  # when the acknowledgement sound started
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
        self._turn_text = ""  # what the active turn is answering
        self._turn_audible = False  # the active turn has started playing
        self._merges = 0  # restarts of the pending turn so far
        self._acks: list[tuple[str, AudioChunk]] = []
        # Things FRIDAY wants to say unprompted (a background task finished).
        self._notices: asyncio.Queue[Callable[[], AsyncGenerator[str, None]]] = asyncio.Queue()
        self._turn_is_notice = False
        self._transcribing = False
        self._ack_index = 0
        self._turn: asyncio.Task[None] | None = None
        self._interrupt_reason: str | None = None
        self._stop_requested = False  # explicit, cancellation-independent stop
        self._resume = asyncio.Event()  # cleared while a turn is on hold
        self._resume.set()
        self._hold_count = 0  # bumped on every hold; detects a cut chunk
        self._held_at: float | None = None
        self.state = VoiceState.LISTENING
        self.speaking = False

    # ---- public ---------------------------------------------------------

    async def run(self) -> None:
        await self.prepare()
        worker = asyncio.create_task(self._utterance_worker(), name="friday-utterances")
        announcer = asyncio.create_task(self._announcer(), name="friday-notices")
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
            announcer.cancel()
            await self._cancel_turn("shutdown")
            for task in (worker, announcer):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def announce(self, stream: Callable[[], AsyncGenerator[str, None]]) -> None:
        """Say something unprompted, as soon as nobody is talking."""
        self._notices.put_nowait(stream)

    def _idle(self) -> bool:
        return not (
            self._turn_active()
            or self.segmenter.in_speech
            or self._transcribing
            or not self._utterances.empty()
        )

    async def _announcer(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            stream = await self._notices.get()
            while True:  # several conditions, none worth an Event each: poll
                if self._idle():
                    break
                await asyncio.sleep(0.1)
            self._interrupt_reason = None
            self._stop_requested = False
            self._turn_text = ""
            self._turn_audible = False
            self._turn_is_notice = True
            self._merges = 0
            metrics = TurnMetrics(endpoint_ms=0.0)
            self._turn = asyncio.create_task(
                self._run_turn("", loop.time(), metrics, stream()), name="friday-notice"
            )

    async def prepare(self) -> None:
        """Synthesise the acknowledgement clips once, so they play instantly."""
        if self._acks or not self.config.acknowledgements:
            return
        try:
            for text in self.config.acknowledgements:
                self._acks.append((text, await self.tts.synthesize(text)))
        except FridayError as exc:  # a nicety, never a reason not to start
            log.warning("acknowledgement sounds unavailable: %s", exc)
            self._acks = []

    def interrupt(self, reason: str = "barge_in") -> None:
        """Stop the current turn now (also usable from a UI button)."""
        if self._turn is not None and not self._turn.done():
            self._interrupt_reason = reason
            self._stop_requested = True
            self.sink.stop()
            self._turn.cancel()

    @property
    def on_hold(self) -> bool:
        return self._turn_active() and not self._resume.is_set()

    def _hold(self) -> None:
        """Silence playback but keep the turn, until we know what the user said."""
        if self._turn_active() and self._resume.is_set():
            self._resume.clear()
            self._hold_count += 1
            self._held_at = asyncio.get_running_loop().time()
            self.sink.stop()
            self._set_state(VoiceState.USER_SPEAKING)

    def _release(self) -> None:
        """False alarm (noise or a backchannel): carry on speaking."""
        if self.on_hold:
            self._held_at = None
            self._resume.set()
            if not self._turn_audible:
                self._set_state(VoiceState.THINKING)

    def _pre_audio(self) -> bool:
        """The active turn has not made a sound yet (and is not a command)."""
        return (
            not self._turn_audible
            and not self._turn_is_notice
            and not self.engine.last_turn.was_command
        )

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
            and not self.on_hold
        ):
            if self._pre_audio() and self._merges >= self.config.max_merges:
                pass  # restarted often enough: answer what we have
            else:
                log.debug(
                    "holding after %.0f ms of speech (p=%.2f, threshold=%.2f)",
                    self.segmenter.speech_ms,
                    prob,
                    threshold,
                )
                self._hold()

    def _handle_segment_event(self, event: object) -> None:
        if isinstance(event, SpeechStarted):
            if not self._turn_active():
                self._set_state(VoiceState.USER_SPEAKING)
        elif isinstance(event, UtteranceEnded):
            self._utterances.put_nowait((event, asyncio.get_running_loop().time()))
        elif not self._turn_active():
            self._set_state(VoiceState.LISTENING)
        elif self._utterances.empty():
            self._release()  # too short to mean anything

    # ---- utterance worker ----------------------------------------------

    async def _utterance_worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            utterance, ended_at = await self._utterances.get()
            self._transcribing = True
            try:
                transcript = await self.stt.transcribe(utterance.pcm, utterance.sample_rate)
                text = transcript.text.strip()
                stt_ms = (loop.time() - ended_at) * 1000
                intent = classify(text) if text else None
                if text:
                    self._emit(UserSaid(text))
                if not text:
                    self._release()  # noise, not speech
                    if not self._turn_active():
                        self._set_state(VoiceState.LISTENING)
                    continue
                if self._turn_active():
                    answer = self.engine.awaiting_answer and not is_filler(text)
                    if (intent is ControlIntent.BACKCHANNEL and not answer) or is_filler(text):
                        self._release()
                        continue
                    if intent is ControlIntent.STOP:
                        await self._cancel_turn("stop")
                    elif not self._pre_audio():
                        await self._cancel_turn("barge_in")
                        self._merges = 0
                    elif self._merges >= self.config.max_merges:
                        log.debug("merge limit reached, not restarting for: %r", text)
                        self._release()
                        continue
                    else:
                        # The user went on talking before we answered: answer
                        # the whole thought as one turn.
                        text = f"{self._turn_text} {text}"
                        await self._cancel_turn("merge")
                        self._merges += 1
                else:
                    self._merges = 0
                    if is_filler(text):
                        # "Hmm." into silence is not a question; do not answer it.
                        self._set_state(VoiceState.LISTENING)
                        continue
                if intent is ControlIntent.STOP:
                    self.engine.dismiss_pending()
                    self._set_state(VoiceState.LISTENING)
                    continue
                metrics = TurnMetrics(
                    endpoint_ms=self.config.segmenter.end_silence_ms, stt_ms=stt_ms
                )
                self._interrupt_reason = None
                self._stop_requested = False
                self._turn_text = text
                self._turn_audible = False
                self._turn_is_notice = False
                self._turn = asyncio.create_task(
                    self._run_turn(text, ended_at, metrics), name="friday-turn"
                )
            except FridayError as exc:
                log.error("speech recognition failed: %s", exc)
                self._emit(VoiceErrorEvent(f"speech recognition failed: {exc}"))
                self._release()
                if not self._turn_active():
                    self._set_state(VoiceState.LISTENING)
            finally:
                self._transcribing = False
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

    async def _run_turn(
        self,
        text: str,
        ended_at: float,
        metrics: TurnMetrics,
        notice: AsyncGenerator[str, None] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self.config.tts_queue_size)
        ack = self._pick_ack(text)
        producer = asyncio.create_task(
            self._produce(text, queue, ended_at, metrics, ack[0] if ack else None, notice)
        )
        heard: list[str] = []
        playing: tuple[str, float, float] | None = None  # text, start, duration
        self._set_state(VoiceState.THINKING)

        def check_stop() -> None:
            if self._stop_requested:
                raise asyncio.CancelledError

        try:
            if ack is not None:
                await self._resume.wait()
                check_stop()
                metrics.ack_ms = (loop.time() - ended_at) * 1000
                self.speaking = True
                await self.sink.play(ack[1])
                check_stop()
            while True:
                item = await queue.get()
                check_stop()
                if item is _END:
                    break
                if isinstance(item, BaseException):
                    raise item
                assert isinstance(item, tuple)
                chunk_text, audio = item
                assert isinstance(audio, AudioChunk)
                while True:  # replays the chunk if a hold cut it short
                    await self._resume.wait()
                    check_stop()
                    if metrics.first_audio_ms is None:
                        metrics.first_audio_ms = (loop.time() - ended_at) * 1000
                    self.speaking = True
                    self._turn_audible = True
                    self._set_state(VoiceState.SPEAKING)
                    holds = self._hold_count
                    playing = (chunk_text, loop.time(), audio.duration_s)
                    await self.sink.play(audio)
                    check_stop()
                    if self._hold_count == holds:
                        break
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
                stopped = self._held_at if self._held_at is not None else loop.time()
                fraction = (stopped - started) / duration if duration > 0 else 1.0
                heard.append(_truncate_words(chunk_text, fraction))
            heard_text = " ".join(h for h in heard if h)
            if self._interrupt_reason == "merge":
                self.engine.discard_turn()  # it is re-asked as part of a longer turn
            else:
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
            self._held_at = None
            self._resume.set()
            self._set_state(
                VoiceState.USER_SPEAKING if self.segmenter.in_speech else VoiceState.LISTENING
            )

    def _pick_ack(self, text: str) -> tuple[str, AudioChunk] | None:
        if (
            not self._acks
            or self.engine.mode is not Mode.COMPANION
            or len(text.split()) < self.config.ack_min_words
            or self.engine.is_control(text)
        ):
            return None
        ack = self._acks[self._ack_index % len(self._acks)]
        self._ack_index += 1
        return ack

    async def _produce(
        self,
        text: str,
        queue: asyncio.Queue[object],
        ended_at: float,
        metrics: TurnMetrics,
        ack_text: str | None = None,
        notice: AsyncGenerator[str, None] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        chunker = SentenceChunker()
        sanitizer = SpeechSanitizer()
        first = True

        async def speak(piece: str) -> None:
            nonlocal first
            cleaned = sanitizer.clean(piece)
            if first and ack_text:
                cleaned = _drop_echo(cleaned, ack_text)  # not "Hmm. Hmm, ..."
            first = False
            if cleaned:
                # While the user is talking over us, do not compete with speech
                # recognition for the CPU; synthesise again once that is settled.
                await self._resume.wait()
                audio = await self.tts.synthesize(cleaned)
                await queue.put((cleaned, audio))

        try:
            async with aclosing(notice or self.engine.respond(text)) as stream:
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


_LEADING_WORD = re.compile(r"\W*(\w+)\W*")


def _drop_echo(piece: str, ack: str) -> str:
    """Remove a first word that repeats the acknowledgement just played."""
    said = _LEADING_WORD.match(ack)
    lead = _LEADING_WORD.match(piece)
    if said is None or lead is None or lead.group(1).lower() != said.group(1).lower():
        return piece
    rest = piece[lead.end() :]
    return rest[:1].upper() + rest[1:]
