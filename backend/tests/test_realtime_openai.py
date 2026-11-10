"""Protocol tests for the speech-to-speech session against a local stand-in server.

These check that FRIDAY speaks the Realtime WebSocket protocol as we understand
it. They do NOT prove the live API accepts it.
"""

import asyncio
import base64
import json

import pytest

pytest.importorskip("websockets")
from websockets.asyncio.server import serve

from friday.core.errors import VoiceError
from friday.persona.persona import Mode
from friday.voice import session as vs
from friday.voice.base import AudioFrame
from friday.voice.realtime_openai import SAMPLE_RATE, OpenAIRealtimeSession

SECOND = b"\x01\x00" * SAMPLE_RATE  # one second of audio


class FakeSink:
    def __init__(self) -> None:
        self.written = 0
        self.pending = 0
        self.flushes = 0

    def write(self, pcm: bytes, sample_rate: int) -> None:
        assert sample_rate == SAMPLE_RATE
        self.written += len(pcm)
        self.pending += len(pcm)

    def drain(self, nbytes: int) -> None:  # pretend the speaker played this much
        self.pending -= nbytes

    def flush(self) -> int:
        self.flushes += 1
        dropped, self.pending = self.pending, 0
        return dropped

    @property
    def pending_bytes(self) -> int:
        return self.pending


class Mic:
    def __init__(self, rate: int = SAMPLE_RATE) -> None:
        self.rate = rate
        self.finish = asyncio.Event()

    async def frames(self):  # type: ignore[no-untyped-def]
        for _ in range(3):
            yield AudioFrame(b"\x05\x00" * 480, self.rate)
        await self.finish.wait()


def audio_delta(item: str, pcm: bytes) -> dict[str, object]:
    return {
        "type": "response.output_audio.delta",
        "item_id": item,
        "delta": base64.b64encode(pcm).decode(),
    }


async def run_against(script, **kwargs):  # type: ignore[no-untyped-def]
    """Run a session against ``script(ws, received, sink)``; return what happened."""
    received: list[dict[str, object]] = []
    seen: dict[str, object] = {}
    events: list[object] = []
    sink = FakeSink()
    mic = Mic()

    async def handler(ws) -> None:  # type: ignore[no-untyped-def]
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["path"] = ws.request.path

        async def reader() -> None:
            async for raw in ws:
                received.append(json.loads(raw))

        task = asyncio.create_task(reader())
        await script(ws, received, sink)
        await asyncio.sleep(0.05)
        mic.finish.set()
        task.cancel()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        session = OpenAIRealtimeSession(
            mic,
            sink,
            "sk-test-key-123456",
            model="rt-model",
            voice="v1",
            url=f"ws://127.0.0.1:{port}/v1/realtime",
            on_event=events.append,
            **kwargs,
        )
        await asyncio.wait_for(session.run(), 10)
    return session, received, seen, events, sink


async def wait_for(predicate) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + 3.0
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.005)


async def test_handshake_audio_upload_and_a_full_turn() -> None:
    async def script(ws, received, sink) -> None:  # type: ignore[no-untyped-def]
        await wait_for(lambda: sum(m["type"] == "input_audio_buffer.append" for m in received) == 3)
        for event in (
            {"type": "input_audio_buffer.speech_started"},
            {"type": "input_audio_buffer.speech_stopped"},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "Hi Friday",
            },
            audio_delta("it1", SECOND),
            {
                "type": "response.output_audio_transcript.done",
                "item_id": "it1",
                "transcript": "Hey! Good to hear you.",
            },
            {"type": "response.done", "response": {"status": "completed"}},
        ):
            await ws.send(json.dumps(event))

    session, received, seen, events, sink = await run_against(script)
    assert seen["auth"] == "Bearer sk-test-key-123456"
    assert seen["path"] == "/v1/realtime?model=rt-model"
    update = received[0]
    assert update["type"] == "session.update"
    cfg = update["session"]
    assert "You are FRIDAY" in cfg["instructions"] and "COMPANION" in cfg["instructions"]
    assert cfg["audio"]["output"]["voice"] == "v1"
    assert cfg["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": SAMPLE_RATE}
    appended = [m for m in received if m["type"] == "input_audio_buffer.append"]
    assert base64.b64decode(appended[0]["audio"]) == b"\x05\x00" * 480
    assert sink.written == len(SECOND)
    assert [e.text for e in events if isinstance(e, vs.UserSaid)] == ["Hi Friday"]
    assert [e.text for e in events if isinstance(e, vs.AssistantSaid)] == ["Hey! Good to hear you."]
    finished = [e for e in events if isinstance(e, vs.TurnFinished)]
    assert len(finished) == 1 and not finished[0].interrupted
    assert finished[0].metrics.first_audio_ms is not None


async def test_barge_in_flushes_audio_and_truncates_to_what_was_heard() -> None:
    async def script(ws, received, sink) -> None:  # type: ignore[no-untyped-def]
        await ws.send(json.dumps(audio_delta("it1", SECOND + SECOND)))
        await ws.send(
            json.dumps(
                {
                    "type": "response.output_audio_transcript.delta",
                    "item_id": "it1",
                    "delta": "one two three four",
                }
            )
        )
        await wait_for(lambda: sink.written == 2 * len(SECOND))
        sink.drain(len(SECOND))  # the user heard the first of two seconds
        await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
        await wait_for(lambda: any(m["type"] == "conversation.item.truncate" for m in received))
        await ws.send(json.dumps(audio_delta("it1", SECOND)))  # late audio: must be dropped

    session, received, seen, events, sink = await run_against(script)
    truncate = next(m for m in received if m["type"] == "conversation.item.truncate")
    assert truncate["item_id"] == "it1" and truncate["audio_end_ms"] == 1000
    assert sink.pending == 0 and sink.written == 2 * len(SECOND)
    interrupted = [e for e in events if isinstance(e, vs.Interrupted)]
    assert [e.heard for e in interrupted] == ["one two"]


async def test_spoken_mode_command_updates_instructions() -> None:
    async def script(ws, received, sink) -> None:  # type: ignore[no-untyped-def]
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "Friday, work mode.",
                }
            )
        )
        await wait_for(lambda: sum(m["type"] == "session.update" for m in received) == 2)

    session, received, seen, events, sink = await run_against(script)
    updates = [m for m in received if m["type"] == "session.update"]
    assert session.mode is Mode.WORK
    assert "Current mode: WORK" in updates[1]["session"]["instructions"]


async def test_api_error_is_reported_not_fatal() -> None:
    async def script(ws, received, sink) -> None:  # type: ignore[no-untyped-def]
        await ws.send(json.dumps({"type": "error", "error": {"message": "bad voice"}}))

    session, received, seen, events, sink = await run_against(script)
    assert [e.message for e in events if isinstance(e, vs.VoiceErrorEvent)] == ["bad voice"]


async def test_connection_failure_is_clean_and_hides_the_key() -> None:
    session = OpenAIRealtimeSession(
        Mic(), FakeSink(), "sk-test-key-123456", model="m", voice="v", url="ws://127.0.0.1:9/x"
    )
    with pytest.raises(VoiceError) as info:
        await asyncio.wait_for(session.run(), 10)
    assert "sk-test" not in str(info.value)
