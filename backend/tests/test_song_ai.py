"""AI changes to attached songs (ACE-Step 1.5): what is understood, the API
calls, and the job in the chat app."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_attachments import HOST, _app, _client, _forget_events, _until, _upload, _wav
from test_studio import CardModel, FakeComfy

from friday.config import FridayConfig
from friday.conversation.studio import StudioDelegate
from friday.integrations.song_ai import (
    AceStepClient,
    SongAIError,
    SongEditor,
    SongTask,
    mix_args,
    output_path,
    parse_song_task,
    part_path,
)
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter

__all__ = ["_forget_events"]  # the fixture clears saved events between tests


@pytest.mark.parametrize(
    ("text", "task"),
    [
        (
            "add a funky slap bass line to this song",
            SongTask("lego", "funky slap bass line", "bass"),
        ),
        ("Add drums please", SongTask("lego", "drums", "drums")),
        ("can you add a piano part", SongTask("lego", "piano part", "keyboard")),
        ("add a saxophone solo", SongTask("lego", "saxophone solo", "woodwinds")),
        ("give me just the vocals", SongTask("extract", "vocals", "vocals")),
        ("isolate the backing vocals", SongTask("extract", "backing vocals", "backing_vocals")),
        ("make it a jazz version", SongTask("cover", "jazz")),
        ("turn this into lo-fi hip hop", SongTask("cover", "lo-fi hip hop")),
        (
            "redo 0:30 to 0:45 with a guitar solo",
            SongTask("repaint", "a guitar solo", start=30, end=45),
        ),
        ("regenerate from 10s to 20s", SongTask("repaint", "same style", start=10, end=20)),
    ],
)
def test_what_is_understood(text: str, task: SongTask) -> None:
    assert parse_song_task(text) == task


@pytest.mark.parametrize("text", ["what song is this", "make it better", "redo it", ""])
def test_not_a_song_change(text: str) -> None:
    assert parse_song_task(text) is None


def test_descriptions_and_file_names(tmp_path: Path) -> None:
    lego = SongTask("lego", "funky bass line", "bass")
    assert lego.describe == "add bass: funky bass line"
    assert SongTask("extract", "vocals", "vocals").describe == "just the vocals"
    assert SongTask("repaint", "solo", start=75, end=90).describe == "redo 1:15 to 1:30: solo"
    first = output_path(tmp_path / "My Song!.wav", lego, tmp_path / "out")
    assert first.name == "My Song (with bass 1).mp3"
    first.touch()
    assert output_path(tmp_path / "My Song!.wav", lego, tmp_path / "out").name == (
        "My Song (with bass 2).mp3"
    )


def test_the_form_sent_to_ace_step() -> None:
    client = AceStepClient(model="acestep-v15-base", steps=50)
    form = client.form(SongTask("lego", "funky bass line", "bass"))
    assert form["task_type"] == "lego" and form["prompt"] == "funky bass line"
    assert form["instruction"] == "Generate the bass track based on the audio context:"
    assert form["model"] == "acestep-v15-base" and form["inference_steps"] == "50"
    assert form["thinking"] == "false" and form["batch_size"] == "1"
    repaint = client.form(SongTask("repaint", "solo", start=30, end=45))
    assert (repaint["repainting_start"], repaint["repainting_end"]) == ("30", "45")
    assert "instruction" not in repaint
    assert client.form(SongTask("cover", "jazz"))["audio_cover_strength"] == "0.5"


def _ace_step(results: list[dict[str, Any]], seen: list[httpx.Request]) -> httpx.AsyncClient:
    """A stand-in ACE-Step server: queued, then the given results in turn."""
    answers = iter(results)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/release_task":
            return httpx.Response(200, json={"code": 200, "data": {"task_id": "t1"}})
        if request.url.path == "/query_result":
            return httpx.Response(200, json={"code": 200, "data": [next(answers)]})
        if request.url.path == "/v1/audio":
            return httpx.Response(200, content=b"ID3 new song")
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")


async def test_a_change_is_sent_waited_for_and_saved(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    done = json.dumps([{"file": "/v1/audio?path=%2Ftmp%2Fa.mp3", "status": 1}])
    http = _ace_step([{"status": 0}, {"status": 1, "result": done}], seen)
    client = AceStepClient("http://127.0.0.1:8001", client=http, poll_s=0)
    song = tmp_path / "song.wav"
    song.write_bytes(b"RIFF")
    target = await client.run(song, SongTask("lego", "drums", "drums"), tmp_path / "out.mp3")
    assert target.read_bytes() == b"ID3 new song"
    upload = seen[0].content
    assert b'name="src_audio"; filename="song.wav"' in upload and b"RIFF" in upload
    assert b'name="task_type"\r\n\r\nlego' in upload
    assert [r.url.path for r in seen] == [
        "/release_task",
        "/query_result",
        "/query_result",
        "/v1/audio",
    ]


async def test_a_failed_change_says_why_and_leaves_no_file(tmp_path: Path) -> None:
    failed = json.dumps([{"status": 2, "error": "out of memory"}])
    client = AceStepClient(client=_ace_step([{"status": 2, "result": failed}], []), poll_s=0)
    song = tmp_path / "song.wav"
    song.write_bytes(b"RIFF")
    with pytest.raises(SongAIError, match="out of memory"):
        await client.run(song, SongTask("cover", "jazz"), tmp_path / "out.mp3")
    assert not (tmp_path / "out.mp3").exists()


class FakeLauncher:
    """As VoiceServiceLauncher: start() and stop()."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> bool:
        self.calls.append("start")
        return True

    def stop(self) -> None:
        self.calls.append("stop")


class FakeClient(AceStepClient):
    def __init__(self, fail: bool = False) -> None:
        super().__init__(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)))
        self.fail = fail
        self.done: list[tuple[str, SongTask]] = []
        self.release = asyncio.Event()
        self.release.set()

    async def run(self, source: Path, task: SongTask, target: Path) -> Path:
        await self.release.wait()
        if self.fail:
            raise SongAIError("ACE-Step couldn't do it: out of memory")
        self.done.append((source.name, task))
        target.write_bytes(b"ID3 changed")  # noqa: ASYNC240 (a test stand-in)
        return target


async def test_the_server_is_started_and_stopped_around_a_change(tmp_path: Path) -> None:
    launcher = FakeLauncher()
    editor = SongEditor(FakeClient(fail=True), launcher)  # type: ignore[arg-type]
    with pytest.raises(SongAIError):
        await editor.run(tmp_path / "a.wav", SongTask("cover", "jazz"), tmp_path / "b.mp3")
    assert launcher.calls == ["start", "stop"]


async def test_the_card_is_cleared_for_ace_step(tmp_path: Path) -> None:
    talk = CardModel("gpu")
    router = ProviderRouter(
        {"gpu": talk, "local": FakeProvider("local")}, {"default": ["gpu", "local"]}
    )
    fake = FakeComfy()
    studio = StudioDelegate(
        fake.client(), {}, tmp_path, router=router, gpu_providers=["gpu"], open_results=False
    )
    seen: list[list[str]] = []

    async def work() -> str:
        seen.append([p.info.name for p in router.candidates("default")])
        return "done"

    assert await studio.exclusive(work) == "done"
    assert seen == [["local"]]  # the talking model stepped aside while it ran
    assert (talk.unloaded, talk.warmed) == (1, 1) and router.held == frozenset()
    assert fake.freed >= 1  # ComfyUI let go of its models first


def test_the_server_must_be_on_this_computer() -> None:
    with pytest.raises(ValueError, match="this machine only"):
        FridayConfig.model_validate({"song_ai": {"url": "http://192.168.1.9:8001"}})


def test_add_a_part_in_the_chat_app(tmp_path: Path) -> None:
    services, talk, _ = _app(tmp_path)
    client = FakeClient()
    services.song_ai = SongEditor(client)
    with _client(services) as http, http.websocket_connect("/ws", headers=HOST) as ws:
        assert http.get("/api/status").json()["can_read"]["audio"] is True
        song = _upload(http, "My Song.wav", _wav(tmp_path / "s.wav"))
        ws.send_json({"type": "chat", "text": "", "files": [song["id"]]})
        said = _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        assert "add a funky bass line" in said["message"]["content"]

        ws.send_json({"type": "chat", "text": "add a funky bass line", "files": [song["id"]]})
        job = _until(ws, "message", lambda e: e["message"]["kind"] == "job")["message"]
        assert job["content"] == "My Song.wav: add bass: funky bass line"
        assert job["meta"]["ai"] == "lego"
        done = _until(
            ws,
            "message_update",
            lambda e: e["message"]["id"] == job["id"] and e["message"]["meta"]["status"] == "done",
        )["message"]
        assert done["file_name"] == "My Song (with bass 1).mp3"
        assert http.get(f"/files/{job['id']}").content == b"ID3 changed"
    assert client.done == [("My Song.wav", SongTask("lego", "funky bass line", "bass"))]
    assert talk.requests == []


def test_a_failed_ai_change_shows_why(tmp_path: Path) -> None:
    services, _, _ = _app(tmp_path)
    services.song_ai = SongEditor(FakeClient(fail=True))
    with _client(services) as http, http.websocket_connect("/ws", headers=HOST) as ws:
        song = _upload(http, "song.wav", _wav(tmp_path / "s.wav"))
        ws.send_json({"type": "chat", "text": "make it a jazz version", "files": [song["id"]]})
        failed = _until(
            ws,
            "message_update",
            lambda e: e["message"]["meta"].get("status") == "failed",
        )["message"]
        assert "out of memory" in failed["meta"]["error"]


def test_without_ace_step_it_says_so(tmp_path: Path) -> None:
    services, _, _ = _app(tmp_path, ffmpeg="ffmpeg")
    with _client(services) as http, http.websocket_connect("/ws", headers=HOST) as ws:
        song = _upload(http, "song.wav", _wav(tmp_path / "s.wav"))
        ws.send_json({"type": "chat", "text": "add drums", "files": [song["id"]]})
        said = _until(ws, "message", lambda e: e["message"]["role"] == "assistant")
        assert "isn't set up" in said["message"]["content"]


def test_the_mix_command() -> None:
    args = mix_args("ffmpeg", Path("song.mp3"), Path("part.mp3"), Path("out.mp3"))
    graph = args[args.index("-filter_complex") + 1]
    assert args[args.index("song.mp3") - 1] == "-i" and args[args.index("part.mp3") - 1] == "-i"
    assert "amix=inputs=2:duration=first:normalize=0" in graph and "alimiter" in graph
    assert part_path(Path("/m/My Song (with bass 1).mp3")).name == (
        "My Song (with bass 1) - part only.mp3"
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_an_added_part_is_mixed_into_the_song(tmp_path: Path) -> None:
    song = tmp_path / "song.wav"
    song.write_bytes(_wav(tmp_path / "s.wav"))
    client = FakeClient()

    async def tone(source: Path, task: SongTask, target: Path) -> Path:
        client.done.append((source.name, task))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=f=80:d=1", str(target),
        )  # fmt: skip
        await proc.wait()
        return target

    client.run = tone  # type: ignore[method-assign]
    editor = SongEditor(client, ffmpeg="ffmpeg")
    target = tmp_path / "song (with bass 1).mp3"
    await editor.run(song, SongTask("lego", "bass", "bass"), target)
    assert target.stat().st_size > 0 and part_path(target).stat().st_size > 0
    # Pulling a part out is not mixed back in.
    alone = tmp_path / "song (only bass 1).mp3"
    await editor.run(song, SongTask("extract", "bass", "bass"), alone)
    assert not part_path(alone).exists()


@pytest.mark.parametrize(
    "text",
    [
        "make a flute instrumental of this song",
        "play this song on flute",
        "make it a flute version",
        "the tune on flute please",
    ],
)
def test_the_whole_song_on_one_instrument(text: str) -> None:
    from friday.integrations.vocals import parse_vocal_task

    task = parse_song_task(text)
    assert task == SongTask("cover", "flute instrumental, solo flute playing the melody, no vocals")
    assert parse_vocal_task(text) is None  # not karaoke


def test_adding_a_flute_is_still_a_new_part() -> None:
    task = parse_song_task("add a flute")
    assert task is not None and task.task == "lego"
