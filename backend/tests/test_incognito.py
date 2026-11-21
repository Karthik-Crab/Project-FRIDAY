"""Incognito chats: kept out of FRIDAY's memory, and gone without a trace when deleted."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from fastapi.testclient import TestClient
from test_attachments import HOST, ORIGIN, _until, _upload
from test_studio import FakeComfy

from friday.app.server import create_app
from friday.app.services import AppServices
from friday.app.store import ConversationStore
from friday.conversation.engine import (
    INCOGNITO_NO_NOTES,
    INCOGNITO_NO_TASKS,
    ConversationEngine,
)
from friday.conversation.notes import NoteBook
from friday.conversation.studio import StudioDelegate, Workflow
from friday.integrations.workflows import qwen_image_edit
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel


def _services(tmp_path: Path, helpers: tuple[Path, ...] = ()) -> tuple[AppServices, NoteBook]:
    talk = FakeProvider("gpu", reply="Sure.")
    eyes = FakeProvider("vision", reply="A fox.")
    router = ProviderRouter(
        {"gpu": talk, "vision": eyes}, {"default": ["gpu"], "vision": ["vision"]}
    )
    notes = NoteBook(tmp_path / "memory")
    services = AppServices(
        ConversationStore(tmp_path / "chats.db"),
        lambda _: ConversationEngine(router, channel=Channel.TEXT, notes=notes),
        uploads_dir=tmp_path / "uploads",
        audio_dir=tmp_path / "Music",
        can_see=True,
        helper_folders=helpers,
    )
    return services, notes


def _client(services: AppServices) -> TestClient:
    return TestClient(create_app(services, port=8765), base_url="http://127.0.0.1:8765")


def _new(client: TestClient, incognito: bool) -> str:
    response = client.post("/api/conversations", json={"incognito": incognito}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["incognito"] is incognito
    return str(response.json()["id"])


def test_an_old_chat_database_gets_the_incognito_column(tmp_path: Path) -> None:
    db = tmp_path / "chats.db"
    old = sqlite3.connect(db)
    old.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
        " created REAL NOT NULL, updated REAL NOT NULL);"
        "INSERT INTO conversations VALUES ('a', 'Old chat', 1, 1);"
    )
    old.commit()
    old.close()
    store = ConversationStore(db)
    assert store.conversations()[0].incognito is False
    assert store.create(incognito=True).incognito is True


def test_nothing_goes_into_memory_in_an_incognito_chat(tmp_path: Path) -> None:
    services, notes = _services(tmp_path)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        secret = _new(client, incognito=True)
        ws.send_json(
            {"type": "chat", "conversation_id": secret, "text": "remember that my PIN is 4321"}
        )
        said = _until(ws, "stream_end")
        assert said["message"]["content"] == INCOGNITO_NO_NOTES
        ws.send_json(
            {"type": "chat", "conversation_id": secret, "text": "ask claude code to fix the tests"}
        )
        said = _until(ws, "stream_end")
        assert said["message"]["content"] == INCOGNITO_NO_TASKS
        assert notes.facts() == []
        # A normal chat still remembers.
        normal = _new(client, incognito=False)
        ws.send_json(
            {"type": "chat", "conversation_id": normal, "text": "remember that I like tea"}
        )
        _until(ws, "stream_end")
        assert any("tea" in f for f in notes.facts())


def test_deleting_an_incognito_chat_removes_every_file(tmp_path: Path) -> None:
    comfy_out = tmp_path / "ComfyUI" / "output"
    comfy_out.mkdir(parents=True)
    old_picture = comfy_out / "before.png"  # made before the job: must stay
    old_picture.write_bytes(b"old")
    past = time.time() - 3600
    os.utime(old_picture, (past, past))
    services, _ = _services(tmp_path, helpers=(comfy_out,))
    fake = FakeComfy()
    real_download = fake.handler

    def handler(request):  # type: ignore[no-untyped-def]
        if request.url.path == "/prompt":  # ComfyUI keeps its own copy of the result
            (comfy_out / "FRIDAY-edit_00001_.png").write_bytes(b"copy")
        return real_download(request)

    fake.handler = handler  # type: ignore[method-assign]
    services.studio = StudioDelegate(
        fake.client(),
        {},
        tmp_path / "Pictures",
        open_results=False,
        edit_workflow=Workflow(
            qwen_image_edit(),
            {"unet": "e.gguf", "text_encoder": "t.gguf", "vae": "v.safetensors", "steps": 4},
        ),
    )
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        secret = _new(client, incognito=True)
        pic = _upload(client, "me.png", b"\x89PNG secret")
        ws.send_json(
            {
                "type": "chat",
                "conversation_id": secret,
                "text": "make the sky red",
                "files": [pic["id"]],
            }
        )
        done = _until(
            ws,
            "message_update",
            lambda e: e["message"]["meta"].get("status") in {"done", "failed"},
        )["message"]
        assert done["meta"]["status"] == "done", done["meta"]
        result = Path(services.store.message(done["id"]).file or "")
        # Kept in the private folder, not in Pictures; ComfyUI's copy is gone already.
        assert result.parent == tmp_path / "incognito" / secret
        assert list((tmp_path / "Pictures").glob("*")) == []
        assert not (comfy_out / "FRIDAY-edit_00001_.png").exists()
        assert old_picture.exists()
        assert (tmp_path / "uploads" / pic["id"]).is_dir()

        assert client.delete(f"/api/conversations/{secret}", headers=ORIGIN).status_code == 200
        assert services.store.get(secret) is None
        assert not result.exists()
        assert not (tmp_path / "incognito" / secret).exists()
        assert not (tmp_path / "uploads" / pic["id"]).exists()
    raw = (tmp_path / "chats.db").read_bytes()
    assert b"make the sky red" not in raw  # overwritten, not just marked free


def test_a_normal_chat_keeps_its_files_when_deleted(tmp_path: Path) -> None:
    services, _ = _services(tmp_path)
    with _client(services) as client, client.websocket_connect("/ws", headers=HOST) as ws:
        normal = _new(client, incognito=False)
        pic = _upload(client, "fox.png", b"\x89PNG fake")
        ws.send_json({"type": "chat", "conversation_id": normal, "text": "", "files": [pic["id"]]})
        _until(ws, "stream_end")
        client.delete(f"/api/conversations/{normal}", headers=ORIGIN)
        assert (tmp_path / "uploads" / pic["id"]).is_dir()
