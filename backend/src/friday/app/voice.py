"""Voice mode for the chat app: on only while the owner switches it on.

Switching on: the expressive voice moves onto the graphics card (the
``GraphicsCard`` makes room for it first, and the talking model loads again
beside it), then the microphone opens and the conversation continues by voice.

Switching off closes the microphone; the voice leaves the card (it waits in
RAM for next time) and the talking model has the whole card again.

The speech recogniser (Whisper) runs on the processor and stays loaded in RAM
between switches, so switching on again is quick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from friday.conversation.engine import ConversationEngine
from friday.integrations.card import GraphicsCard
from friday.integrations.voice_service import VoiceServiceLauncher
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel

log = logging.getLogger(__name__)


class Session(Protocol):
    async def run(self) -> None: ...

    def announce(self, stream: Any) -> None: ...


# Builds a voice session for an engine; events go to the callback.
SessionBuilder = Callable[[ConversationEngine, Callable[[object], None]], Awaitable[Session]]


class VoiceMode:
    def __init__(
        self,
        build_session: SessionBuilder,
        *,
        launcher: VoiceServiceLauncher | None = None,
        router: ProviderRouter | None = None,
        gpu_providers: Sequence[str] = (),
        on_stopped: Callable[[str | None], None] | None = None,
        card: GraphicsCard | None = None,
    ) -> None:
        self._build = build_session
        self.card = card  # when set, it decides who has the graphics card
        self.launcher = launcher
        self.router = router
        self.gpu_providers = tuple(gpu_providers)
        self.on_stopped = on_stopped  # called with an error message, or None
        self.state = "off"
        self.conversation_id: str | None = None
        self._engine: ConversationEngine | None = None
        self._session: Session | None = None
        self._task: asyncio.Task[None] | None = None
        self._switch = asyncio.Lock()

    @property
    def available(self) -> bool:
        return True

    async def start(
        self,
        engine: ConversationEngine,
        conversation_id: str,
        on_event: Callable[[object], None],
        audio: Any = None,
    ) -> None:
        """``audio``: a browser page's microphone and speaker (``BrowserAudio``);
        None uses this PC's own."""
        async with self._switch:
            if self.state == "on":
                return
            self.state = "starting"
            try:
                if self.card is not None:
                    await self.card.voice_on()
                elif self.launcher is not None and not await self.launcher.is_up():
                    await self._unload_card_models()
                    try:
                        await self.launcher.start()
                    finally:
                        await self._load_card_models()
                if audio is not None:
                    session = await self._build(engine, on_event, audio)  # type: ignore[call-arg]
                else:
                    session = await self._build(engine, on_event)
            except BaseException:
                self.state = "off"
                if self.card is not None:
                    with contextlib.suppress(Exception):
                        await self.card.voice_off()
                elif self.launcher is not None:
                    self.launcher.stop()
                raise
            engine.channel = Channel.VOICE
            self._engine = engine
            self._session = session
            self.conversation_id = conversation_id
            self._task = asyncio.get_running_loop().create_task(self._run(session))
            self.state = "on"

    async def _run(self, session: Session) -> None:
        error: str | None = None
        try:
            await session.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the microphone went away, a missing library, ...
            log.exception("voice mode stopped")
            error = f"Voice mode stopped: {exc}"
        if self.state == "on":  # stopped by itself, not switched off
            await self.stop()
            if self.on_stopped is not None:
                self.on_stopped(error)

    async def stop(self) -> None:
        async with self._switch:
            if self.state == "off":
                return
            self.state = "stopping"
            task, self._task = self._task, None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if self._engine is not None:
                self._engine.channel = Channel.TEXT
            self._engine = None
            self._session = None
            self.conversation_id = None
            if self.card is not None:
                await self.card.voice_off()
            elif self.launcher is not None and self.launcher.started_here:
                self.launcher.stop()
                # Give the talking model the whole card again.
                await self._unload_card_models()
                await self._load_card_models()
            self.state = "off"

    def announce(self, engine: ConversationEngine, text: str, context: str) -> None:
        if self._session is not None:
            self._session.announce(lambda: engine.say(text, context))

    async def _unload_card_models(self) -> None:
        for provider in self._card_providers():
            unload = getattr(provider, "unload", None)
            if unload is not None:
                with contextlib.suppress(Exception):
                    await unload()

    async def _load_card_models(self) -> None:
        for provider in self._card_providers():
            warm_up = getattr(provider, "warm_up", None)
            if warm_up is not None:
                with contextlib.suppress(Exception):
                    await warm_up()

    def _card_providers(self) -> list[Any]:
        if self.router is None:
            return []
        return [self.router.providers[n] for n in self.gpu_providers if n in self.router.providers]

    async def aclose(self) -> None:
        await self.stop()
        if self.card is not None:
            self.card.stop_voice()
        if self.launcher is not None:
            await self.launcher.aclose()


def real_session_builder(config: Any, router: ProviderRouter) -> SessionBuilder:
    """Microphone, Whisper, the expressive voice (Kokoro if it is down), speaker.

    Whisper and Kokoro are loaded on first use and then kept in RAM.
    """
    from dataclasses import replace

    from friday.voice.segmenter import SegmenterConfig
    from friday.voice.session import VoiceConfig, VoiceSession
    from friday.voice.vad import EnergyVAD

    v = config.voice
    loaded: dict[str, Any] = {}

    async def build(
        engine: ConversationEngine, on_event: Callable[[object], None], audio: Any = None
    ) -> Session:
        from friday.voice.adapters.faster_whisper_stt import FasterWhisperSTT
        from friday.voice.adapters.kokoro_tts import KokoroTTS
        from friday.voice.adapters.remote_tts import FallbackTTS, RemoteTTS
        from friday.voice.adapters.sounddevice_io import MicrophoneSource, SpeakerSink

        if "stt" not in loaded:
            stt = FasterWhisperSTT(
                v.stt_model,
                device=v.stt_device,
                compute_type=v.stt_compute_type,
                language=v.language,
                beam_size=v.stt_beam_size,
                max_no_speech_prob=v.stt_max_no_speech_prob,
                min_avg_logprob=v.stt_min_avg_logprob,
                cpu_threads=v.stt_cpu_threads,
                vocabulary=("Friday", config.user_name, "Claude Code", *v.stt_vocabulary),
            )
            await stt.warm_up()
            loaded["stt"] = stt
        if "kokoro" not in loaded:
            loaded["kokoro"] = KokoroTTS(
                v.tts_model_path, v.tts_voices_path, voice=v.tts_voice, speed=v.tts_speed
            )
        tts: Any = loaded["kokoro"]
        if v.tts_server_url:
            tts = FallbackTTS(RemoteTTS(v.tts_server_url, v.tts_server_timeout_s), tts)
        await tts.warm_up()
        if isinstance(tts, FallbackTTS):
            engine.persona = replace(engine.persona, expression_tags=tts.expression_tags)
        vcfg = VoiceConfig(
            speech_threshold=v.speech_threshold,
            # The browser removes FRIDAY's own voice from the microphone, so she
            # needs no extra deafness while she speaks.
            speech_threshold_while_speaking=(
                v.speech_threshold if audio is not None else v.speech_threshold_while_speaking
            ),
            barge_in_min_ms=v.barge_in_min_ms,
            max_merges=v.max_merges,
            acknowledgements=tuple(v.acknowledgements),
            sentence_pause_ms=v.sentence_pause_ms,
            max_chunk_chars=v.max_chunk_chars,
            segmenter=SegmenterConfig(end_silence_ms=v.end_silence_ms),
        )
        return VoiceSession(
            engine,
            audio if audio is not None else MicrophoneSource(v.input_device),
            EnergyVAD(min_speech_db=v.min_speech_db),
            loaded["stt"],
            tts,
            audio if audio is not None else SpeakerSink(v.output_device),
            vcfg,
            on_event=on_event,
        )

    return build
