"""Attached files in the chat app: pictures, documents, and songs (more bass)."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import wave
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_studio import CardModel, FakeComfy, _finish

from friday.app.server import create_app
from friday.app.services import AppServices, _safe_name
from friday.app.store import ConversationStore
from friday.cli import _model_list
from friday.config import FridayConfig
from friday.conversation.engine import PICTURES_NOT_SET_UP, ConversationEngine
from friday.conversation.studio import Kind, Request, StudioDelegate, Workflow
from friday.core.messages import ChatRequest, Message, Role
from friday.documents import DocumentError, category, extract_text, for_model
from friday.integrations.audio_tools import (
    AudioEdit,
    ffmpeg_args,
    find_ffmpeg,
    output_name,
    parse_audio_edit,
    wants_new_part,
)
from friday.integrations.workflows import sdxl_text_to_image
from friday.llm.providers.fake import FakeProvider
from friday.llm.providers.ollama import OllamaProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel

HOST = {"host": "127.0.0.1:8765"}
ORIGIN = {"origin": "http://127.0.0.1:8765"}
FFMPEG = find_ffmpeg()


# What the owner asks for


@pytest.mark.parametrize(
    ("text", "gain"),
    [
        ("add some bass", 6.0),
        ("Add some bass to this song please", 6.0),
        ("more bass", 6.0),
        ("boost the bass a lot", 10.0),
        ("just a little more bass", 3.0),
        ("add 8 dB of bass", 8.0),
        ("less bass", -6.0),
        ("too much bass, tone it down", -6.0),
    ],
)
def test_bass_requests(text: str, gain: float) -> None:
    assert parse_audio_edit(text) == AudioEdit(gain)


@pytest.mark.parametrize(
    "text", ["make it sound nice", "add a bass line", "who plays bass here?", "what song is this"]
)
def test_not_a_bass_change(text: str) -> None:
    assert parse_audio_edit(text) is None


def test_a_new_instrument_part_is_told_apart() -> None:
    assert wants_new_part("add a bass line to this")
    assert wants_new_part("can you add drums")
    assert not wants_new_part("add some bass")


def test_ffmpeg_command(tmp_path: Path) -> None:
    song = tmp_path / "My Song!.m4a"
    target = output_name(song, AudioEdit(6.0), tmp_path / "out")
    assert target.name == "My Song (bass 1).mp3"
    args = ffmpeg_args("ffmpeg", song, target, AudioEdit(6.0))
    assert args[args.index("-af") + 1] == "bass=g=6:f=100:w=0.6,alimiter=limit=0.95:level=disabled"
    target.touch()
    assert output_name(song, AudioEdit(6.0), tmp_path / "out").name == "My Song (bass 2).mp3"
    softer = ffmpeg_args("ffmpeg", song, target, AudioEdit(-3.0))
    assert softer[softer.index("-af") + 1] == "bass=g=-3:f=100:w=0.6"  # no limiter needed


# Documents


def test_file_kinds_and_safe_names() -> None:
    assert category("cat.JPG") == "image"
    assert category("song.flac") == "audio"
    assert category("plan.docx") == "document"
    assert category("setup.exe") is None
    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("..\\..\\boot.ini") == "boot.ini"
    assert _safe_name("a<b>:c?.txt") == "a_b_c_.txt"


def test_plain_text_and_cutting(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("# Shopping\n- milk   \n- eggs\n", encoding="utf-8")
    assert extract_text(doc) == "# Shopping\n- milk\n- eggs"
    text, cut = for_model("notes.md", "x" * 50, 20)
    assert cut and text.startswith('[Attached document "notes.md"]\n' + "x" * 20)
    assert "first 20 of 50 characters" in text
    (tmp_path / "empty.txt").write_text("  \n")
    with pytest.raises(DocumentError, match="no text"):
        extract_text(tmp_path / "empty.txt")


def test_word_and_excel(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    openpyxl = pytest.importorskip("openpyxl")
    document = docx.Document()
    document.add_paragraph("Meeting on Friday at 10.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Name", "Role"
    document.save(tmp_path / "plan.docx")
    assert extract_text(tmp_path / "plan.docx") == "Meeting on Friday at 10.\nName | Role"

    book = openpyxl.Workbook()
    book.active.title = "Budget"
    book.active.append(["Rent", 1200])
    book.active.append(["Food", 300])
    book.save(tmp_path / "budget.xlsx")
    assert extract_text(tmp_path / "budget.xlsx") == "[sheet Budget]\nRent,1200\nFood,300"


def _pdf_with_text(text: str) -> bytes:
    """A one-page PDF, written by hand (no extra library)."""
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_pdf(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    (tmp_path / "letter.pdf").write_bytes(_pdf_with_text("Your order has shipped"))
    assert extract_text(tmp_path / "letter.pdf") == "[page 1]\nYour order has shipped"


# Pictures: the engine and the Ollama adapter


async def test_a_picture_goes_to_the_model_that_can_see() -> None:
    talk, eyes = FakeProvider("gpu", reply="Hi."), FakeProvider("vision", reply="A red fox.")
    router = ProviderRouter(
        {"gpu": talk, "vision": eyes}, {"default": ["gpu"], "vision": ["vision"]}
    )
    engine = ConversationEngine(router, channel=Channel.TEXT)
    reply = "".join([d async for d in engine.respond("What is this? [picture: fox.png]", ["QUJD"])])
    assert reply == "A red fox." and talk.requests == []
    assert eyes.requests[0].messages[-1].images == ("QUJD",)
    # History keeps the words only; the next question goes to the talking model.
    assert all(not m.images for m in engine.window.messages)
    "".join([d async for d in engine.respond("Thanks!")])
    assert len(talk.requests) == 1


async def test_pictures_not_set_up_is_said_plainly() -> None:
    model = FakeProvider("gpu")
    engine = ConversationEngine(ProviderRouter({"gpu": model}, {"default": ["gpu"]}))
    reply = "".join([d async for d in engine.respond("what's this?", ["QUJD"])])
    assert reply == PICTURES_NOT_SET_UP and model.requests == []


async def test_ollama_sends_pictures_context_and_thinking() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    provider = OllamaProvider("vision", "gemma3:4b", client=client, num_ctx=8192, think=False)
    request = ChatRequest(system="s", messages=(Message(Role.USER, "what?", ("QUJD",)),))
    assert "".join([d async for d in provider.stream(request)]) == "ok"
    await provider.warm_up()
    sent = seen[0]
    assert sent["messages"][1] == {"role": "user", "content": "what?", "images": ["QUJD"]}
    assert sent["options"]["num_ctx"] == 8192 and sent["think"] is False
    assert seen[1]["options"] == {"num_ctx": 8192}  # warm-up loads it the same way


async def test_the_picture_model_steps_aside_during_a_job(tmp_path: Path) -> None:
    talk, eyes = CardModel("gpu"), CardModel("vision")
    cpu = FakeProvider("local")
    router = ProviderRouter(
        {"gpu": talk, "vision": eyes, "local": cpu},
        {"default": ["gpu", "local"], "vision": ["vision", "local"]},
    )
    fake = FakeComfy()
    fake.release.clear()
    studio = StudioDelegate(
        fake.client(),
        {
            Kind.IMAGE: Workflow(
                sdxl_text_to_image(),
                {
                    "checkpoint": "j",
                    "width": 64,
                    "height": 64,
                    "steps": 1,
                    "cfg": 1,
                    "negative": "",
                },
            )
        },
        tmp_path,
        router=router,
        gpu_providers=["gpu"],
        gpu_occasional=["vision"],
        open_results=False,
    )
    job = asyncio.create_task(studio.make(Request(Kind.IMAGE, "a fox")))
    for _ in range(200):
        if fake.queued:
            break
        await asyncio.sleep(0.005)
    try:
        assert fake.queued, "the job did not start"
        assert [p.info.name for p in router.candidates("vision")] == ["local"]
    finally:
        fake.release.set()
    await job
    await _finish(studio)
    assert (talk.unloaded, talk.warmed) == (1, 1)
    assert (eyes.unloaded, eyes.warmed) == (1, 0)  # not loaded again: rarely used
    assert router.held == frozenset()


# The chat app


def _app(
    tmp_path: Path,
    *,
    reply: str | Callable[[ChatRequest], str] = "Got it.",
    see: bool = True,
    ffmpeg: str | None = None,
) -> tuple[AppServices, FakeProvider, FakeProvider]:
    talk = FakeProvider("gpu", reply=reply)
    eyes = FakeProvider("vision", reply="A red fox in the snow.")
    routes = {"default": ["gpu"], **({"vision": ["vision"]} if see else {})}
    router = ProviderRouter({"gpu": talk, "vision": eyes}, routes)
    services = AppServices(
        ConversationStore(tmp_path / "chats.db"),
        lambda _: ConversationEngine(router, channel=Channel.TEXT),
        uploads_dir=tmp_path / "uploads",
        audio_dir=tmp_path / "Music",
        ffmpeg=ffmpeg,
        can_see=see,
        document_chars=60,
    )
    return services, talk, eyes


def _client(services: AppServices) -> TestClient:
    return TestClient(create_app(services, port=8765), base_url="http://127.0.0.1:8765")


def _upload(client: TestClient, name: str, data: bytes) -> dict[str, Any]:
    response = client.post(f"/api/upload?name={name}", content=data, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return dict(response.json())


_seen: dict[int, list[dict[str, Any]]] = {}


@pytest.fixture(autouse=True)
def _forget_events() -> None:
    _seen.clear()  # a new connection may get an old one's id


def _until(ws: Any, kind: str, check: Callable[[dict[str, Any]], bool] = lambda e: True) -> Any:
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


def test_uploads_are_checked(tmp_path: Path) -> None:
    services, _, _ = _app(tmp_path)
    with _client(services) as client:
        bad = client.post("/api/upload?name=setup.exe", content=b"MZ", headers=ORIGIN)
        assert bad.status_code == 400 and "can't use .exe" in bad.json()["detail"]
        assert client.post("/api/upload?name=a.txt", content=b"", headers=ORIGIN).status_code == 400
        away = client.post(
            "/api/upload?name=a.txt", content=b"x", headers={"origin": "https://evil.example"}
        )
        assert away.status_code == 403
        saved = _upload(client, "../../notes.txt", b"hello")
        assert saved["name"] == "notes.txt" and saved["category"] == "document"
        assert (tmp_path / "uploads" / saved["id"] / "notes.txt").read_bytes() == b"hello"
        page = client.get(f"/uploads/{saved['id']}")
        assert page.headers["content-disposition"].startswith("attachment")
        assert client.get("/uploads/not-an-id").status_code == 404
        assert client.get("/uploads/" + "0" * 32).status_code == 404


def test_a_document_is_read_into_the_question(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path, reply="It says the order shipped.")
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        doc = _upload(client, "order.txt", b"Your order has shipped. " * 5)
        ws.send_json({"type": "chat", "text": "", "files": [doc["id"]], "request_id": 1})
        user = _until(ws, "message", lambda e: e["message"]["role"] == "user")["message"]
        assert user["content"] == "Summarise this document."
        assert user["meta"]["files"] == [doc]
        end = _until(ws, "stream_end")
        assert end["message"]["content"] == "It says the order shipped."
        asked = talk.requests[0].messages[-1].content
        assert asked.startswith('Summarise this document.\n\n[Attached document "order.txt"]')
        assert "Your order has shipped." in asked and "first 60 of 119" in asked

        # Follow-up questions still see it, also after the app restarts.
        services._engines.clear()
        ws.send_json({"type": "chat", "text": "When?", "conversation_id": user["conversation_id"]})
        _until(ws, "stream_end", lambda e: e["message"]["id"] != end["message"]["id"])
        history = [m.content for m in talk.requests[1].messages]
        assert any("Your order has shipped." in m for m in history)


def test_a_document_that_cannot_be_read(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        doc = _upload(client, "blank.txt", b"   \n  ")
        ws.send_json({"type": "chat", "text": "what's in it?", "files": [doc["id"]]})
        reply = _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        assert "no text" in reply["message"]["content"] and talk.requests == []


def test_a_picture_is_looked_at(tmp_path: Path) -> None:
    services, talk, eyes = _app(tmp_path)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        pic = _upload(client, "fox.png", b"\x89PNG fake")
        ws.send_json({"type": "chat", "text": "", "files": [pic["id"]]})
        end = _until(ws, "stream_end")
        assert end["message"]["content"] == "A red fox in the snow."
        assert end["message"]["meta"]["provider"] == "vision"
        assert eyes.requests[0].messages[-1].images == ("iVBORyBmYWtl",)
        assert "[picture attached: fox.png]" in eyes.requests[0].messages[-1].content
        assert talk.requests == []
        assert client.get(f"/uploads/{pic['id']}").content == b"\x89PNG fake"


def test_a_picture_without_a_model_that_can_see(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path, see=False)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        assert client.get("/api/status").json()["can_read"]["image"] is False
        pic = _upload(client, "fox.png", b"\x89PNG fake")
        ws.send_json({"type": "chat", "text": "what's this?", "files": [pic["id"]]})
        assert _until(ws, "stream_end")["message"]["content"] == PICTURES_NOT_SET_UP
        assert talk.requests == []


def _wav(path: Path, seconds: float = 0.5) -> bytes:
    rate = 22050
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        frames = (
            int(8000 * math.sin(2 * math.pi * f * n / rate))
            for n in range(int(rate * seconds))
            for f in (60,)
        )
        out.writeframes(b"".join(struct.pack("<h", v) for v in frames))
    return path.read_bytes()


def test_a_song_without_a_change_asked_for(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path, ffmpeg="ffmpeg")
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        song = _upload(client, "song.wav", _wav(tmp_path / "s.wav"))
        ws.send_json({"type": "chat", "text": "", "files": [song["id"]]})
        said = _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        assert "add some bass" in said["message"]["content"]
        ws.send_json({"type": "chat", "text": "add a bass line", "files": [song["id"]]})
        said = _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        assert "isn't set up" in said["message"]["content"]
        assert talk.requests == []


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
def test_more_bass_for_real(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path, ffmpeg=FFMPEG)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        song = _upload(client, "My Song.wav", _wav(tmp_path / "s.wav"))
        ws.send_json({"type": "chat", "text": "add some bass", "files": [song["id"]]})
        job = _until(ws, "message", lambda e: e["message"]["kind"] == "job")["message"]
        assert job["content"] == "My Song.wav: more bass (+6 dB)"
        done = _until(
            ws,
            "message_update",
            lambda e: (
                e["message"]["id"] == job["id"] and e["message"]["meta"]["status"] != "running"
            ),
        )["message"]
        assert done["meta"]["status"] == "done", done["meta"]
        assert done["file_name"] == "My Song (bass 1).wav"
        changed = tmp_path / "Music" / "My Song (bass 1).wav"
        with wave.open(str(changed)) as w:
            assert w.getnframes() > 0
        assert client.get(f"/files/{job['id']}").status_code == 200
        assert talk.requests == []


# Thinking on or off, and the list of models


async def test_thinking_can_be_switched_per_question() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, text=json.dumps({"message": {"content": "ok"}, "done": True}))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")
    qwen = OllamaProvider("gpu", "qwen3", client=client, think=False)
    gemma = OllamaProvider("local", "gemma3", client=client)  # can't think: never told to
    ask = ChatRequest(system="s", messages=(Message(Role.USER, "hi"),), max_tokens=100)
    for provider, think in [(qwen, None), (qwen, True), (gemma, True)]:
        "".join([d async for d in provider.stream(replace(ask, think=think))])
    assert seen[0]["think"] is False and seen[0]["options"]["num_predict"] == 100
    assert seen[1]["think"] is True and seen[1]["options"]["num_predict"] == 4196
    assert "think" not in seen[2] and seen[2]["options"]["num_predict"] == 100


@pytest.mark.parametrize("can_think", [True, False])
def test_the_app_passes_the_thinking_switch(tmp_path: Path, can_think: bool) -> None:
    services, talk, eyes = _app(tmp_path)
    services.can_think = can_think
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        assert client.get("/api/status").json()["can_think"] is can_think
        ws.send_json({"type": "chat", "text": "hard question", "think": True})
        _until(ws, "stream_end")
        ws.send_json({"type": "chat", "text": "easy one"})
        _until(ws, "stream_end", lambda e: e["message"]["id"] == 4)
        pic = _upload(client, "fox.png", b"\x89PNG fake")
        ws.send_json({"type": "chat", "text": "", "files": [pic["id"]], "think": True})
        _until(ws, "stream_end", lambda e: e["message"]["id"] == 6)
    assert [r.think for r in talk.requests] == [True if can_think else None, None]
    assert eyes.requests[0].think is None  # the picture model can't think


def test_the_model_list_says_what_each_is_for(tmp_path: Path) -> None:
    config = FridayConfig.model_validate(
        {
            "providers": {
                "local": {"kind": "ollama", "model": "gemma3-cpu"},
                "gpu": {"kind": "ollama", "model": "qwen3", "think": False},
                "vision": {"kind": "ollama", "model": "gemma3:4b"},
            },
            "routes": {
                "conversation": ["gpu", "local"],
                "vision": ["vision", "local"],
                "default": ["gpu"],
                "offline": ["local"],
            },
            "studio": {
                "gpu_providers": ["gpu"],
                "gpu_occasional": ["vision"],
                "image_checkpoint": "Juggernaut-XL_v9.safetensors",
                "music_checkpoint": "ace_step_1.5_turbo_aio.safetensors",
            },
            "voice": {
                "tts_server_url": "http://127.0.0.1:8890",
                "tts_server_command": ["python", "chatterbox_server.py"],
            },
        }
    )
    fakes = {}
    for name, cfg in config.providers.items():
        fakes[name] = FakeProvider(name)
        fakes[name]._info = replace(fakes[name].info, model=cfg.model)
    router = ProviderRouter(fakes, config.routes)
    studio = StudioDelegate(
        FakeComfy().client(),
        {Kind.IMAGE: Workflow({}, {}), Kind.MUSIC: Workflow({}, {})},
        tmp_path,
    )
    rows = [(m["role"], m["label"], m["where"]) for m in _model_list(config, router, studio)]
    assert rows == [
        ("Chat", "qwen3", "graphics card"),
        ("Chat when the card is busy, reads pictures", "gemma3-cpu", "processor"),
        ("Reads pictures", "gemma3:4b", "graphics card"),
        ("Makes pictures", "Juggernaut-XL_v9", "graphics card, ComfyUI"),
        ("Makes music", "ace_step_1.5_turbo_aio", "graphics card, ComfyUI"),
        ("Voice", "Chatterbox", "graphics card, only while on"),
        ("Hearing", "Whisper base.en", "processor"),
    ]


def test_the_stop_button_ends_the_answer_and_keeps_what_was_written(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path, reply=" ".join(f"word{i}" for i in range(400)))
    talk._delay = 0.02  # slow enough to stop halfway
    with _client(services) as http, http.websocket_connect("/ws", headers=HOST) as ws:
        ws.send_json({"type": "chat", "text": "tell me a long story"})
        start = _until(ws, "stream_start", lambda e: True)
        _until(ws, "delta", lambda e: e["draft"] == start["draft"])
        cid = start["conversation_id"]
        ws.send_json({"type": "stop", "conversation_id": cid, "request_id": "s"})
        end = _until(ws, "stream_end", lambda e: e["draft"] == start["draft"])["message"]
        assert end["meta"]["stopped"] is True
        assert end["content"].startswith("word0") and "word399" not in end["content"]
        assert talk.cancelled
        # Nothing left to stop; the next question is answered normally.
        ws.send_json({"type": "stop", "conversation_id": cid, "request_id": "t"})
        assert _until(ws, "reply", lambda e: e["request_id"] == "t")["stopped"] is False
        talk._delay = 0
        ws.send_json({"type": "chat", "conversation_id": cid, "text": "and then?"})
        again = _until(ws, "stream_end", lambda e: True)["message"]
        assert "stopped" not in again["meta"]
    history = services.engine(cid).window
    assert "word0" in str(history.messages)
