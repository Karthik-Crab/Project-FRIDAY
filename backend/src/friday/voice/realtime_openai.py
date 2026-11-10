"""Speech-to-speech voice session on the OpenAI Realtime API (optional).

This is the "premium" voice path from ADR-0002: one model hears the audio and
speaks the reply directly, so it keeps tone and answers faster than the
STT -> LLM -> TTS cascade. It needs internet and an OpenAI API key, so the
cascaded ``VoiceSession`` remains the default and the offline fallback.

It emits the same events as ``VoiceSession`` so the CLI and (later) the UI do
not care which one is running. FRIDAY's persona is sent as the session
instructions; mode commands are recognised from the transcript of what the
user said and applied with a session update.

Status: written against the GA Realtime WebSocket protocol and tested against
a local stand-in server. NOT yet verified against the live API.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from friday.core.errors import VoiceError
from friday.persona.commands import parse_command
from friday.persona.persona import Channel, Mode, Persona
from friday.voice.base import AudioSource
from friday.voice.session import (
    AssistantSaid,
    Interrupted,
    StateChanged,
    TurnFinished,
    TurnMetrics,
    UserSaid,
    VoiceErrorEvent,
    VoiceEvent,
    VoiceState,
)

log = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # the API's PCM rate, both directions
DEFAULT_URL = "wss://api.openai.com/v1/realtime"

# Event names changed between the beta and GA protocol; accept both.
_AUDIO_DELTA = {"response.output_audio.delta", "response.audio.delta"}
_TEXT_DELTA = {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}
_TEXT_DONE = {"response.output_audio_transcript.done", "response.audio_transcript.done"}


class StreamingSink(Protocol):
    def write(self, pcm: bytes, sample_rate: int) -> None: ...

    def flush(self) -> int:
        """Drop unplayed audio; return the number of bytes dropped."""
        ...

    @property
    def pending_bytes(self) -> int: ...


class OpenAIRealtimeSession:
    def __init__(
        self,
        source: AudioSource,
        sink: StreamingSink,
        api_key: str,
        *,
        model: str,
        voice: str,
        persona: Persona | None = None,
        mode: Mode = Mode.COMPANION,
        transcription_model: str = "gpt-4o-mini-transcribe",
        turn_detection: str = "semantic_vad",
        url: str = DEFAULT_URL,
        on_event: Callable[[VoiceEvent], None] | None = None,
    ) -> None:
        self.source = source
        self.sink = sink
        self.persona = persona or Persona()
        self.mode = mode
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._transcription_model = transcription_model
        self._turn_detection = turn_detection
        self._url = url
        self._on_event = on_event
        self.state = VoiceState.LISTENING
        # per-reply bookkeeping
        self._item_id: str | None = None
        self._item_bytes = 0
        self._item_text = ""
        self._cut_items: set[str] = set()
        self._speech_stopped_at: float | None = None
        self._metrics: TurnMetrics | None = None

    # ---- protocol payloads ---------------------------------------------

    def _session_update(self) -> dict[str, Any]:
        fmt = {"type": "audio/pcm", "rate": SAMPLE_RATE}
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.persona.system_prompt(self.mode, Channel.VOICE),
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": fmt,
                        "transcription": {"model": self._transcription_model},
                        "turn_detection": {"type": self._turn_detection},
                    },
                    "output": {"format": fmt, "voice": self._voice},
                },
            },
        }

    # ---- run -------------------------------------------------------------

    async def run(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise VoiceError("websockets is not installed: pip install friday[realtime]") from exc

        url = f"{self._url}?model={self._model}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            connection = connect(url, additional_headers=headers, max_size=None)
            ws = await connection.__aenter__()
        except Exception as exc:
            # Never include the exception's request details: they carry the key.
            raise VoiceError(
                f"could not connect to the realtime API: {type(exc).__name__}"
            ) from None
        try:
            await ws.send(json.dumps(self._session_update()))
            tasks = {
                asyncio.create_task(self._send_audio(ws), name="friday-rt-mic"),
                asyncio.create_task(self._receive(ws), name="friday-rt-events"),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                task.result()
        finally:
            self.sink.flush()
            await connection.__aexit__(None, None, None)

    async def _send_audio(self, ws: Any) -> None:
        async for frame in self.source.frames():
            if frame.sample_rate != SAMPLE_RATE:
                raise VoiceError(f"realtime needs {SAMPLE_RATE} Hz audio, got {frame.sample_rate}")
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(frame.pcm).decode("ascii"),
                    }
                )
            )

    async def _receive(self, ws: Any) -> None:
        async for raw in ws:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            await self._handle(ws, event)

    # ---- event handling ------------------------------------------------

    async def _handle(self, ws: Any, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        loop = asyncio.get_running_loop()

        if etype == "input_audio_buffer.speech_started":
            await self._barge_in(ws)
            self._set_state(VoiceState.USER_SPEAKING)

        elif etype == "input_audio_buffer.speech_stopped":
            self._speech_stopped_at = loop.time()
            self._metrics = TurnMetrics(endpoint_ms=0.0)
            self._set_state(VoiceState.THINKING)

        elif etype == "conversation.item.input_audio_transcription.completed":
            text = str(event.get("transcript", "")).strip()
            if text:
                self._emit(UserSaid(text))
                command = parse_command(text)
                if command is not None and command.mode is not self.mode:
                    self.mode = command.mode
                    await ws.send(json.dumps(self._session_update()))

        elif etype in _AUDIO_DELTA:
            item_id = str(event.get("item_id", ""))
            if item_id in self._cut_items:
                return  # late audio for a reply the user already interrupted
            if item_id != self._item_id:
                self._item_id, self._item_bytes, self._item_text = item_id, 0, ""
            pcm = base64.b64decode(event.get("delta", ""))
            if self._metrics is not None and self._metrics.first_audio_ms is None:
                started = self._speech_stopped_at or loop.time()
                self._metrics.first_audio_ms = (loop.time() - started) * 1000
            self._item_bytes += len(pcm)
            self.sink.write(pcm, SAMPLE_RATE)
            self._set_state(VoiceState.SPEAKING)

        elif etype in _TEXT_DELTA:
            if str(event.get("item_id", "")) == self._item_id:
                self._item_text += str(event.get("delta", ""))

        elif etype in _TEXT_DONE:
            item_id = str(event.get("item_id", ""))
            text = str(event.get("transcript", "")).strip()
            if text and item_id not in self._cut_items:
                self._emit(AssistantSaid(text))

        elif etype == "response.done":
            metrics = self._metrics or TurnMetrics(endpoint_ms=0.0)
            metrics.provider = f"openai-realtime:{self._model}"
            status = (event.get("response") or {}).get("status")
            self._emit(TurnFinished(metrics, interrupted=status == "cancelled"))
            self._metrics = None
            if self.state is not VoiceState.USER_SPEAKING:
                self._set_state(VoiceState.LISTENING)

        elif etype == "error":
            err = event.get("error") or {}
            message = str(err.get("message", err))
            log.error("realtime API error: %s", message)
            self._emit(VoiceErrorEvent(message))

    async def _barge_in(self, ws: Any) -> None:
        """The user started talking: silence FRIDAY and tell the server how much
        of the reply was actually heard, so its memory matches the user's."""
        dropped = self.sink.flush()
        if self._item_id is None or dropped == 0:
            return
        played = max(0, self._item_bytes - dropped)
        played_ms = played * 1000 // (2 * SAMPLE_RATE)
        words = self._item_text.split()
        fraction = played / self._item_bytes if self._item_bytes else 0.0
        heard = " ".join(words[: int(len(words) * fraction)])
        self._cut_items.add(self._item_id)
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.truncate",
                    "item_id": self._item_id,
                    "content_index": 0,
                    "audio_end_ms": played_ms,
                }
            )
        )
        self._emit(Interrupted(reason="barge_in", heard=heard))
        self._item_id = None

    # ---- events ---------------------------------------------------------

    def _set_state(self, state: VoiceState) -> None:
        if state is not self.state:
            self.state = state
            self._emit(StateChanged(state))

    def _emit(self, event: VoiceEvent) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                log.exception("voice event listener failed")
