"""The chat app: saved chats, offers with buttons, jobs, voice switch, local-only."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_studio import CardModel, FakeComfy, _studio

from friday.app.server import create_app
from friday.app.services import AppServices
from friday.app.store import NEW_TITLE, ConversationStore
from friday.app.voice import VoiceMode
from friday.conversation.engine import ConversationEngine
from friday.core.messages import Role
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel
from friday.voice import session as vs

HOST = {"host": "127.0.0.1:8765"}


def _services(
    tmp_path: Path,
    *,
    reply: str = "Hello there.",
    comfy: FakeComfy | None = None,
    voice: Any = None,
) -> tuple[AppServices, FakeProvider]:
    model = FakeProvider("local", reply=reply)
    router = ProviderRouter({"local": model}, {"default": ["local"]})
    studio = _studio(comfy, tmp_path)[0] if comfy else None

    def make_engine(_: str) -> ConversationEngine:
        return ConversationEngine(router, channel=Channel.TEXT, studio=studio)

    store = ConversationStore(tmp_path / "chats.db")
    return AppServices(store, make_engine, studio=studio, voice=voice), model


def _client(services: AppServices) -> TestClient:
    return TestClient(create_app(services, port=8765), base_url="http://127.0.0.1:8765")


_seen: dict[int, list[dict[str, Any]]] = {}


def _until(ws: Any, kind: str, check: Callable[[dict[str, Any]], bool] = lambda e: True) -> Any:
    """The next event of this kind. Others are kept: they may arrive in any order."""
    kept = _seen.setdefault(id(ws), [])
    for i, event in enumerate(kept):
        if event["type"] == kind and check(event):
            return kept.pop(i)
    for _ in range(500):
        event = ws.receive_json()
        if event["type"] == kind and check(event):
            return event
        kept.append(event)
    raise AssertionError(f"no {kind} event")


# Store


def test_store_keeps_conversations_and_messages(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "c.db")
    conv = store.create()
    first = store.add(conv.id, "user", "text", "hi")
    store.add(conv.id, "assistant", "job", "a fox", meta={"kind": "image", "status": "running"})
    store.close()

    again = ConversationStore(tmp_path / "c.db")  # after a restart
    assert [c.id for c in again.conversations()] == [conv.id]
    messages = again.messages(conv.id)
    assert [m.content for m in messages] == ["hi", "a fox"]
    assert messages[0].id == first.id
    assert [m.content for m in again.unfinished_jobs()] == ["a fox"]
    done = again.update(messages[1].id, file="/x/fox.png", meta={"status": "done"})
    assert done.meta == {"kind": "image", "status": "done"}
    assert done.to_json()["file_name"] == "fox.png" and "file" not in done.to_json()
    again.delete(conv.id)
    assert again.messages(conv.id) == []


def test_unfinished_jobs_are_marked_failed_on_start(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "c.db")
    conv = store.create()
    job = store.add(conv.id, "assistant", "job", "x", meta={"kind": "image", "status": "running"})
    store.close()
    services, _ = _services(tmp_path.parent / tmp_path.name)  # same file name below
    services.store.close()
    services = AppServices(ConversationStore(tmp_path / "c.db"), lambda _: None)  # type: ignore[arg-type,return-value]
    assert services.store.message(job.id).meta["status"] == "failed"  # type: ignore[union-attr]


# Chat


def test_chat_streams_saves_and_titles(tmp_path: Path) -> None:
    services, model = _services(tmp_path, reply="Paris is the capital of France.")
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        assert ws.receive_json()["type"] == "status"
        ws.send_json({"type": "chat", "text": "What is the capital of France?", "request_id": 1})
        conv = _until(ws, "conversation")["conversation"]
        start = _until(ws, "stream_start")
        end = _until(ws, "stream_end")
        assert end["draft"] == start["draft"]
        assert end["message"]["content"] == "Paris is the capital of France."
        assert end["message"]["meta"]["provider"] == "local"
        reply = _until(ws, "reply")
        assert reply["conversation_id"] == conv["id"]

        saved = client.get(f"/api/conversations/{conv['id']}").json()
        assert saved["conversation"]["title"] == "What is the capital of France?"
        assert [(m["role"], m["kind"]) for m in saved["messages"]] == [
            ("user", "text"),
            ("assistant", "text"),
        ]
        assert client.get("/api/conversations").json()[0]["id"] == conv["id"]
    assert len(model.requests) == 1


def test_history_is_rebuilt_from_saved_chats(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    conv = services.store.create()
    services.store.add(conv.id, "user", "text", "My dog is called Bruno.")
    services.store.add(conv.id, "assistant", "text", "Lovely name!")
    services.store.add(
        conv.id, "assistant", "job", "a fox", meta={"kind": "image", "status": "done"}
    )
    engine = services.engine(conv.id)
    assert [(m.role, m.content) for m in engine.window.messages] == [
        (Role.USER, "My dog is called Bruno."),
        (Role.ASSISTANT, "Lovely name!"),
        (Role.USER, "(no message)"),
        (Role.ASSISTANT, "I made the picture: a fox."),
    ]


def test_rename_and_delete(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    with _client(services) as client:
        conv = client.post("/api/conversations", headers={"origin": "http://127.0.0.1:8765"}).json()
        assert conv["title"] == NEW_TITLE
        client.patch(f"/api/conversations/{conv['id']}", json={"title": "  Dogs  "})
        assert (
            client.get(f"/api/conversations/{conv['id']}").json()["conversation"]["title"] == "Dogs"
        )
        client.delete(f"/api/conversations/{conv['id']}")
        assert client.get(f"/api/conversations/{conv['id']}").status_code == 404


# Pictures and music


def test_picture_request_is_offered_then_made(tmp_path: Path) -> None:
    comfy = FakeComfy()
    services, model = _services(tmp_path, comfy=comfy)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "chat", "text": "Hi! Can you make a picture of a fox in the snow?"})
        offer = _until(ws, "message", lambda e: e["message"]["kind"] == "proposal")["message"]
        assert offer["content"] == "a fox in the snow"
        assert offer["meta"] == {"kind": "image", "status": "pending"}
        assert model.requests == [] and comfy.queued == []  # nothing until the button

        ws.send_json(
            {
                "type": "make",
                "conversation_id": offer["conversation_id"],
                "kind": "image",
                "prompt": "a red fox in deep snow",
                "proposal_id": offer["id"],
                "request_id": 7,
            }
        )
        accepted = _until(ws, "message_update", lambda e: e["message"]["id"] == offer["id"])
        assert accepted["message"]["meta"]["status"] == "accepted"
        job = _until(ws, "message", lambda e: e["message"]["kind"] == "job")["message"]
        done = _until(
            ws,
            "message_update",
            lambda e: e["message"]["id"] == job["id"] and e["message"]["meta"]["status"] == "done",
        )["message"]
        assert done["file_name"]
        assert comfy.queued[0]["6"]["inputs"]["text"] == "a red fox in deep snow"
        picture = client.get(f"/files/{job['id']}")
        assert picture.status_code == 200 and picture.content.startswith(b"\x89PNG")
        assert client.get("/files/99999").status_code == 404

        # The same offer cannot be used twice.
        ws.send_json(
            {
                "type": "make",
                "conversation_id": offer["conversation_id"],
                "kind": "image",
                "prompt": "again",
                "proposal_id": offer["id"],
                "request_id": 8,
            }
        )
        assert "already answered" in _until(ws, "error")["message"]


def test_picture_mode_makes_straight_away_and_can_be_cancelled(tmp_path: Path) -> None:
    comfy = FakeComfy(polls=10_000)
    services, _ = _services(tmp_path, comfy=comfy)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "make", "kind": "image", "prompt": "a lighthouse", "request_id": 1})
        user = _until(ws, "message", lambda e: e["message"]["role"] == "user")["message"]
        assert user["meta"] == {"make": "image"}
        job = _until(ws, "message", lambda e: e["message"]["kind"] == "job")["message"]
        assert job["meta"]["status"] == "running"
        _until(ws, "reply")
        for _ in range(100):
            if comfy.queued:
                break
            time.sleep(0.01)
        ws.send_json({"type": "cancel_job", "message_id": job["id"]})
        cancelled = _until(
            ws, "message_update", lambda e: e["message"]["meta"]["status"] == "cancelled"
        )
        assert cancelled["message"]["id"] == job["id"]
        assert client.get("/api/status").json()["can_make"] == ["image"]


def test_music_not_set_up_is_refused(tmp_path: Path) -> None:
    services, _ = _services(tmp_path, comfy=FakeComfy())
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "make", "kind": "music", "prompt": "piano", "request_id": 3})
        error = _until(ws, "error")
        assert error["request_id"] == 3 and "not set up" in error["message"]
        assert client.get("/api/conversations").json() == []


# Only this machine


def test_other_sites_are_refused(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    with _client(services) as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 403
        bad = client.post("/api/conversations", headers={"origin": "https://evil.example"})
        assert bad.status_code == 403
        assert client.get("/api/conversations").json() == []
        with pytest.raises(Exception):  # noqa: B017 (closed during the handshake)
            with client.websocket_connect(
                "/ws", headers={**HOST, "origin": "https://evil.example"}
            ) as ws:
                ws.receive_json()


# Voice


class FakeVoice:
    """Stands in for VoiceMode (same methods)."""

    def __init__(self) -> None:
        self.state = "off"
        self.conversation_id: str | None = None
        self.on_event: Callable[[object], None] | None = None
        self.engine: ConversationEngine | None = None
        self.said: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def start(
        self, engine: ConversationEngine, conversation_id: str, on_event: Callable[[object], None]
    ) -> None:
        self.state, self.conversation_id, self.on_event, self.engine = (
            "on",
            conversation_id,
            on_event,
            engine,
        )

    async def stop(self) -> None:
        self.state, self.conversation_id = "off", None

    def announce(self, engine: ConversationEngine, text: str, context: str) -> None:
        self.said.append(text)


def test_voice_turns_are_saved_in_the_chat(tmp_path: Path) -> None:
    voice = FakeVoice()
    services, _ = _services(tmp_path, voice=voice)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "voice", "on": True, "request_id": 1})
        on = _until(ws, "status", lambda e: e["voice"] == "on")
        cid = on["voice_conversation"]
        assert _until(ws, "reply")["conversation_id"] == cid
        assert voice.on_event is not None
        metrics = vs.TurnMetrics(endpoint_ms=1.0)
        metrics.provider = "local"
        for event in (
            vs.UserSaid("Tell me a joke"),
            vs.AssistantSaid("Why did the cat sit on the computer?"),
            vs.AssistantSaid("To keep an eye on the mouse."),
            vs.TurnFinished(metrics, interrupted=False),
        ):
            voice.on_event(event)
        _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        saved = client.get(f"/api/conversations/{cid}").json()["messages"]
        assert [(m["role"], m["content"]) for m in saved] == [
            ("user", "Tell me a joke"),
            (
                "assistant",
                "Why did the cat sit on the computer? To keep an eye on the mouse.",
            ),
        ]
        assert saved[0]["meta"] == {"voice": True}

        ws.send_json({"type": "voice", "on": False, "request_id": 2})
        assert _until(ws, "status", lambda e: e["voice"] == "off")
        assert voice.state == "off"


def test_voice_merge_removes_the_half_sentence(tmp_path: Path) -> None:
    voice = FakeVoice()
    services, _ = _services(tmp_path, voice=voice)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "voice", "on": True, "request_id": 1})
        cid = _until(ws, "reply")["conversation_id"]
        assert voice.on_event is not None
        voice.on_event(vs.UserSaid("Tell me"))
        first = _until(ws, "message")["message"]
        voice.on_event(vs.Interrupted(reason="merge", heard=""))
        assert _until(ws, "message_deleted")["id"] == first["id"]
        assert client.get(f"/api/conversations/{cid}").json()["messages"] == []


# VoiceMode: the voice service only while voice is on


class FakeLauncher:
    """Same methods as VoiceServiceLauncher."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.up = False

    @property
    def started_here(self) -> bool:
        return self.up

    async def is_up(self) -> bool:
        return self.up

    async def start(self) -> bool:
        self.log.append("voice service started")
        self.up = True
        return True

    def stop(self) -> None:
        self.log.append("voice service stopped")
        self.up = False

    async def aclose(self) -> None:
        self.stop()


class LoggedCard(CardModel):
    def __init__(self, log: list[str]) -> None:
        super().__init__("gpu")
        self.log = log

    async def unload(self) -> None:
        self.log.append("model unloaded")

    async def warm_up(self, request: Any = None) -> None:
        self.log.append("model loaded")


class FakeSession:
    def __init__(self) -> None:
        self.running = asyncio.Event()

    async def run(self) -> None:
        self.running.set()
        await asyncio.Event().wait()

    def announce(self, stream: Any) -> None:
        pass


async def test_voice_mode_makes_room_on_the_card() -> None:
    log: list[str] = []
    card = LoggedCard(log)
    router = ProviderRouter({"gpu": card}, {"default": ["gpu"]})
    session = FakeSession()

    async def build(engine: ConversationEngine, on_event: Callable[[object], None]) -> FakeSession:
        log.append("microphone on")
        return session

    mode = VoiceMode(
        build,
        launcher=FakeLauncher(log),  # type: ignore[arg-type]
        router=router,
        gpu_providers=["gpu"],
    )
    engine = ConversationEngine(router, channel=Channel.TEXT)
    await mode.start(engine, "c1", lambda e: None)
    await session.running.wait()
    assert mode.state == "on" and engine.channel is Channel.VOICE
    assert log == ["model unloaded", "voice service started", "model loaded", "microphone on"]

    log.clear()
    await mode.stop()
    assert mode.state == "off" and engine.channel is Channel.TEXT
    assert log == ["voice service stopped", "model unloaded", "model loaded"]


async def test_voice_mode_reports_a_broken_microphone() -> None:
    stopped: list[str | None] = []

    class Broken(FakeSession):
        async def run(self) -> None:
            raise OSError("PortAudio library not found")

    async def build(engine: ConversationEngine, on_event: Callable[[object], None]) -> Broken:
        return Broken()

    mode = VoiceMode(build, on_stopped=stopped.append)
    engine = ConversationEngine(ProviderRouter({"x": FakeProvider("x")}, {"default": ["x"]}))
    await mode.start(engine, "c1", lambda e: None)
    for _ in range(100):
        if stopped:
            break
        await asyncio.sleep(0.01)
    assert mode.state == "off"
    assert stopped and "PortAudio" in (stopped[0] or "")
