"""Making pictures and music: request parsing, ComfyUI client, the job, the engine.

The ComfyUI stand-in answers the same routes with the same shapes as ComfyUI
itself (``POST /prompt``, ``GET /history/<id>``, ``GET /view``, ``POST /free``,
``POST /interrupt``, ``GET /system_stats``), and the real ``ComfyClient`` talks
to it, so a wrong request shape fails here rather than on the PC.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from friday.conversation.delegation import Notice
from friday.conversation.engine import ConversationEngine
from friday.conversation.studio import (
    Kind,
    Request,
    StudioDelegate,
    Workflow,
    is_studio_command,
    parse_request,
)
from friday.core.messages import ChatRequest
from friday.integrations.comfyui import (
    ComfyClient,
    ComfyError,
    OutputFile,
    fill_workflow,
    load_workflow,
    placeholders,
)
from friday.integrations.workflows import sdxl_text_to_image
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter

# ---------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("text", "kind", "prompt"),
    [
        ("Friday, make a picture of a fox in the snow.", Kind.IMAGE, "a fox in the snow"),
        ("generate an image of a red car", Kind.IMAGE, "a red car"),
        ("Can you draw me a dragon over a castle?", Kind.IMAGE, "a dragon over a castle"),
        (
            "make a realistic photo of an old man reading",
            Kind.IMAGE,
            "realistic photo of an old man reading",
        ),
        ("create a cyberpunk wallpaper", Kind.IMAGE, "cyberpunk wallpaper"),
        ("show me a picture of a sunset over the sea", Kind.IMAGE, "a sunset over the sea"),
        (
            "could you please generate a picture showing a busy market in Chennai",
            Kind.IMAGE,
            "a busy market in Chennai",
        ),
        ("make a picture", Kind.IMAGE, ""),
        (
            "compose some relaxing piano music for studying",
            Kind.MUSIC,
            "relaxing piano music for studying",
        ),
        ("write a song about my cat", Kind.MUSIC, "song about my cat"),
        ("make some lo-fi beats", Kind.MUSIC, "lo-fi beats"),
        ("Friday, generate music", Kind.MUSIC, ""),
    ],
)
def test_requests_are_recognised(text: str, kind: Kind, prompt: str) -> None:
    assert parse_request(text) == Request(kind, prompt)


@pytest.mark.parametrize(
    "text",
    [
        "Make the picture brighter",
        "write a story about music for kids",
        "what is the best image editor",
        "I made a picture of my dog",
        "tell me about the art of war",
        "draw conclusions from this data",
        "paint the wall blue",
        "make a plan for my week",
        "write an email to my boss",
    ],
)
def test_other_sentences_are_not_requests(text: str) -> None:
    assert parse_request(text) is None
    assert not is_studio_command(text)


def test_cancel_is_a_command() -> None:
    assert is_studio_command("Friday, cancel the picture")
    assert is_studio_command("stop the music")


# ---------------------------------------------------------------- workflows


def test_fill_workflow_types_and_seed() -> None:
    template = sdxl_text_to_image()
    assert placeholders(template) == {
        "checkpoint",
        "width",
        "height",
        "prompt",
        "negative",
        "seed",
        "steps",
        "cfg",
    }
    values = {
        "checkpoint": "juggernaut.safetensors",
        "width": 1024,
        "height": 768,
        "prompt": "a fox",
        "negative": "blurry",
        "steps": 30,
        "cfg": 4.5,
    }
    filled = fill_workflow(template, values, random.Random(1))
    assert filled["5"]["inputs"]["width"] == 1024  # a number, not "1024"
    assert filled["6"]["inputs"]["text"] == "a fox"
    assert isinstance(filled["3"]["inputs"]["seed"], int)
    assert template["6"]["inputs"]["text"] == "{{prompt}}"  # the template is untouched
    again = fill_workflow(template, values, random.Random(2))
    assert again["3"]["inputs"]["seed"] != filled["3"]["inputs"]["seed"]


def test_fill_workflow_inside_text_and_fixed_seeds() -> None:
    template = {
        "1": {"class_type": "T", "inputs": {"tags": "style: {{prompt}}", "seed": 42}},
        "2": {"class_type": "K", "inputs": {"noise_seed": 7, "steps": 8}},
    }
    filled = fill_workflow(template, {"prompt": "jazz"}, random.Random(3))
    assert filled["1"]["inputs"]["tags"] == "style: jazz"
    # A fixed seed in an exported workflow would repeat the same result.
    assert filled["1"]["inputs"]["seed"] != 42
    assert filled["2"]["inputs"]["noise_seed"] != 7
    assert filled["2"]["inputs"]["steps"] == 8


def test_fill_workflow_missing_value() -> None:
    with pytest.raises(ComfyError, match="lyrics"):
        fill_workflow({"1": {"inputs": {"lyrics": "{{lyrics}}"}}}, {"prompt": "x"})


def test_load_workflow_rejects_the_editor_format(tmp_path: Path) -> None:
    path = tmp_path / "wf.json"
    path.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    with pytest.raises(ComfyError, match="Export"):
        load_workflow(path)
    path.write_text(json.dumps(sdxl_text_to_image()), encoding="utf-8")
    assert load_workflow(path)["9"]["class_type"] == "SaveImage"


# ---------------------------------------------------------------- ComfyUI stand-in


class FakeComfy:
    """Answers like ComfyUI. ``polls`` = how many history checks before done."""

    def __init__(self, polls: int = 2, fail: str | None = None, refuse: bool = False) -> None:
        self.polls = polls
        self.fail = fail
        self.refuse = refuse
        self.up = True
        self.queued: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.freed = 0
        self.interrupted = 0
        self.history_calls = 0
        self.uploaded: list[bytes] = []
        self.release = asyncio.Event()
        self.release.set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if not self.up:
            raise httpx.ConnectError("refused", request=request)
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.3.x"}, "devices": []})
        if path == "/prompt" and request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"prompt", "client_id"}
            if self.refuse:
                return httpx.Response(
                    400,
                    json={
                        "error": {"type": "prompt_outputs_failed_validation", "message": "Bad"},
                        "node_errors": {
                            "4": {
                                "errors": [
                                    {
                                        "message": "Value not in list",
                                        "details": "ckpt_name: 'x' not in []",
                                    }
                                ]
                            }
                        },
                    },
                )
            self.queued.append(body["prompt"])
            return httpx.Response(200, json={"prompt_id": "abc", "number": 0, "node_errors": {}})
        if path == "/history/abc":
            self.history_calls += 1
            if self.history_calls < self.polls or not self.release.is_set():
                return httpx.Response(200, json={})
            if self.fail:
                return httpx.Response(
                    200,
                    json={
                        "abc": {
                            "outputs": {},
                            "status": {
                                "status_str": "error",
                                "completed": False,
                                "messages": [
                                    ["execution_start", {"prompt_id": "abc"}],
                                    [
                                        "execution_error",
                                        {
                                            "node_type": "KSampler",
                                            "exception_message": self.fail,
                                        },
                                    ],
                                ],
                            },
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "abc": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "FRIDAY_00001_.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            },
                            "12": {
                                "images": [
                                    {"filename": "preview.png", "subfolder": "", "type": "temp"}
                                ]
                            },
                        },
                        "status": {"status_str": "success", "completed": True, "messages": []},
                    }
                },
            )
        if path == "/view":
            assert request.url.params["type"] == "output"
            return httpx.Response(
                200, content=b"\x89PNG fake " + request.url.params["filename"].encode()
            )
        if path == "/upload/image" and request.method == "POST":
            self.uploaded.append(request.content)
            return httpx.Response(200, json={"name": "friday-pic.png", "subfolder": ""})
        if path == "/free":
            assert json.loads(request.content) == {"unload_models": True, "free_memory": False}
            self.freed += 1
            return httpx.Response(200)
        if path == "/interrupt":
            self.interrupted += 1
            return httpx.Response(200)
        return httpx.Response(404)

    def client(self) -> ComfyClient:
        transport = httpx.MockTransport(self.handler)
        http = httpx.AsyncClient(base_url="http://127.0.0.1:8188", transport=transport)
        return ComfyClient(client=http, poll_s=0.001)


async def test_client_runs_a_job_and_fetches_the_result(tmp_path: Path) -> None:
    fake = FakeComfy(polls=3)
    comfy = fake.client()
    assert await comfy.is_up()
    prompt_id = await comfy.queue({"1": {"class_type": "X", "inputs": {}}})
    files = await comfy.wait(prompt_id, timeout_s=5)
    assert files == [OutputFile("FRIDAY_00001_.png")]  # the temporary preview is left out
    path = await comfy.download(files[0], tmp_path)
    assert path.read_bytes().startswith(b"\x89PNG")
    # A second file of the same name does not overwrite the first.
    assert (await comfy.download(files[0], tmp_path)).name == "FRIDAY_00001_-1.png"


async def test_client_reports_refusal_and_failure() -> None:
    with pytest.raises(ComfyError, match="ckpt_name"):
        await FakeComfy(refuse=True).client().queue({})
    fake = FakeComfy(fail="CUDA out of memory")
    comfy = fake.client()
    with pytest.raises(ComfyError, match="KSampler failed: CUDA out of memory"):
        await comfy.wait(await comfy.queue({}), timeout_s=5)


async def test_client_download_keeps_only_the_file_name(tmp_path: Path) -> None:
    comfy = FakeComfy().client()
    path = await comfy.download(OutputFile("../../evil.png"), tmp_path)
    assert path.parent == tmp_path and path.name == "evil.png"


# ---------------------------------------------------------------- the job


class CardModel(FakeProvider):
    """A talking model on the graphics card, as OllamaProvider: unload, warm_up."""

    def __init__(self, name: str) -> None:
        super().__init__(name, reply="from the card")
        self.unloaded = 0
        self.warmed = 0

    async def unload(self) -> None:
        self.unloaded += 1

    async def warm_up(self, request: ChatRequest | None = None) -> None:
        self.warmed += 1


def _studio(
    fake: FakeComfy, tmp_path: Path, router: ProviderRouter | None = None, music: bool = False
) -> tuple[StudioDelegate, list[Notice], list[Path]]:
    notices: list[Notice] = []
    opened: list[Path] = []
    workflows = {
        Kind.IMAGE: Workflow(
            sdxl_text_to_image(),
            {
                "checkpoint": "j.safetensors",
                "width": 1024,
                "height": 1024,
                "steps": 30,
                "cfg": 4.5,
                "negative": "blurry",
            },
        )
    }
    if music:
        workflows[Kind.MUSIC] = Workflow(
            {
                "1": {
                    "class_type": "SaveAudio",
                    "inputs": {"tags": "{{prompt}}", "len": "{{seconds}}"},
                }
            },
            {"seconds": 30},
        )
    studio = StudioDelegate(
        fake.client(),
        workflows,
        tmp_path / "out",
        router=router,
        gpu_providers=("gpu",) if router else (),
        on_done=notices.append,
        opener=opened.append,
    )
    return studio, notices, opened


async def _finish(studio: StudioDelegate) -> None:
    for _ in range(500):
        if not studio.busy:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("job did not finish")


async def test_read_back_yes_makes_the_picture_and_shares_the_card(tmp_path: Path) -> None:
    card, cpu = CardModel("gpu"), FakeProvider("local", reply="from the cpu")
    router = ProviderRouter({"gpu": card, "local": cpu}, {"default": ["gpu", "local"]})
    fake = FakeComfy(polls=3)
    fake.release.clear()  # hold the job so the middle of it can be checked
    studio, notices, opened = _studio(fake, tmp_path, router)

    assert studio.handle("Friday, make a picture of a fox in the snow") == (
        "Picture: a fox in the snow. Shall I make it?"
    )
    assert fake.queued == []  # nothing happens before "yes"
    assert "making the picture" in (studio.handle("yes") or "")
    for _ in range(200):
        if fake.queued:
            break
        await asyncio.sleep(0.005)
    # While ComfyUI has the card, the card model is unloaded and set aside.
    assert card.unloaded == 1
    assert [p.info.name for p in router.candidates("default")] == ["local"]
    assert fake.queued[0]["6"]["inputs"]["text"] == "a fox in the snow"
    assert fake.queued[0]["4"]["inputs"]["ckpt_name"] == "j.safetensors"

    fake.release.set()
    await _finish(studio)
    assert [n.ok for n in notices] == [True]
    assert notices[0].spoken == "Your picture is ready. I've opened it."
    assert notices[0].log_path is not None and notices[0].log_path.exists()
    assert opened == [notices[0].log_path]
    # Afterwards the card is freed and the card model comes back.
    assert fake.freed == 1
    assert card.warmed == 1
    assert [p.info.name for p in router.candidates("default")] == ["gpu", "local"]


async def test_no_drops_it_and_more_words_replace_it(tmp_path: Path) -> None:
    fake = FakeComfy()
    studio, _, _ = _studio(fake, tmp_path)
    studio.handle("draw a lighthouse")
    assert studio.handle("a lighthouse at night with a storm coming") == (
        "Picture: a lighthouse at night with a storm coming. Shall I make it?"
    )
    assert studio.handle("cool") == "Say yes to make it, or no to drop it."
    assert studio.handle("no") == "Okay, I won't make it."
    assert studio.pending is None and fake.queued == []


async def test_asks_what_to_make_when_not_said(tmp_path: Path) -> None:
    studio, _, _ = _studio(FakeComfy(), tmp_path)
    assert studio.handle("make a picture") == "Sure. What should the picture show?"
    assert studio.awaiting_answer
    assert studio.handle("a cat on a sofa") == "Picture: a cat on a sofa. Shall I make it?"


async def test_music_without_a_workflow_says_so(tmp_path: Path) -> None:
    studio, _, _ = _studio(FakeComfy(), tmp_path)
    assert studio.handle("write a song about rain") == (
        "I can't make music yet. It isn't set up on this computer, so nothing was made."
    )
    assert studio.pending is None


async def test_music_fills_seconds(tmp_path: Path) -> None:
    fake = FakeComfy()
    studio, notices, _ = _studio(fake, tmp_path, music=True)
    studio.music_dir = tmp_path / "music"
    notice = await studio.make(Request(Kind.MUSIC, "calm piano"))
    assert fake.queued[0]["1"]["inputs"] == {"tags": "calm piano", "len": 30}
    assert notice.log_path is not None and notice.log_path.parent == tmp_path / "music"
    assert notice.ok and notice.spoken == "Your music is ready. I've opened it."
    assert notices == []  # make() returns the notice; on_done is for background jobs


async def test_failure_is_reported_and_card_is_given_back(tmp_path: Path) -> None:
    card = CardModel("gpu")
    router = ProviderRouter(
        {"gpu": card, "local": FakeProvider("local")}, {"default": ["gpu", "local"]}
    )
    studio, notices, opened = _studio(FakeComfy(fail="CUDA out of memory"), tmp_path, router)
    notice = await studio.make(Request(Kind.IMAGE, "a fox"))
    assert not notice.ok
    assert notice.spoken == "I couldn't make the picture. KSampler failed: CUDA out of memory."
    assert opened == []
    assert router.held == frozenset()


async def test_comfyui_not_running(tmp_path: Path) -> None:
    fake = FakeComfy()
    fake.up = False
    studio, _, _ = _studio(fake, tmp_path)
    notice = await studio.make(Request(Kind.IMAGE, "a fox"))
    assert notice.spoken == "I couldn't make the picture. ComfyUI isn't running."


async def test_busy_and_cancel(tmp_path: Path) -> None:
    card = CardModel("gpu")
    router = ProviderRouter(
        {"gpu": card, "local": FakeProvider("local")}, {"default": ["gpu", "local"]}
    )
    fake = FakeComfy()
    fake.release.clear()
    studio, notices, _ = _studio(fake, tmp_path, router)
    studio.handle("make a picture of a fox")
    studio.handle("yes")
    await asyncio.sleep(0.02)
    assert studio.busy
    assert studio.handle("make a picture of a cat") == (
        "I'm still making the picture. Say cancel the picture if you want to stop it."
    )
    assert studio.handle("cancel the picture") == "Okay, I've stopped it."
    await _finish(studio)
    assert fake.interrupted == 1
    assert router.held == frozenset()
    assert notices == []
    assert studio.handle("cancel the picture") == "I'm not making anything right now."


# ---------------------------------------------------------------- the engine


async def collect(agen) -> str:  # type: ignore[no-untyped-def]
    return "".join([d async for d in agen])


async def test_engine_without_studio_never_lets_the_model_answer() -> None:
    model = FakeProvider("local", reply="Here is your picture of a fox!")
    engine = ConversationEngine(ProviderRouter({"local": model}, {"default": ["local"]}))
    reply = await collect(engine.respond("Friday, make a picture of a fox"))
    assert reply.startswith("I can't make pictures yet.")
    assert model.requests == []
    assert engine.is_control("generate a song about rain")
    # The model is told, too, for requests the phrases do not catch.
    await collect(engine.respond("hello"))
    assert "cannot make pictures" in model.requests[-1].system


async def test_engine_routes_to_studio(tmp_path: Path) -> None:
    model = FakeProvider("local")
    engine = ConversationEngine(
        ProviderRouter({"local": model}, {"default": ["local"]}),
        studio=_studio(FakeComfy(), tmp_path)[0],
    )
    reply = await collect(engine.respond("make a picture of a fox"))
    assert reply == "Picture: a fox. Shall I make it?"
    assert engine.awaiting_answer
    assert engine.is_control("yes")
    engine.dismiss_pending()
    assert not engine.awaiting_answer
    await collect(engine.respond("hello"))
    assert model.requests and "make a picture of" in model.requests[-1].system


# ---------------------------------------------------------------- the router


def test_router_hold_skips_but_never_leaves_nothing() -> None:
    gpu, cpu = FakeProvider("gpu"), FakeProvider("local")
    router = ProviderRouter({"gpu": gpu, "local": cpu}, {"default": ["gpu", "local"]})
    router.hold("gpu")
    assert [p.info.name for p in router.candidates("default")] == ["local"]
    router.hold("local")  # everything held: better slow than silent
    assert [p.info.name for p in router.candidates("default")] == ["gpu", "local"]
    router.release("gpu")
    router.release("local")
    assert router.held == frozenset()
    with pytest.raises(ValueError):
        router.hold("nope")


def test_built_in_music_workflow_needs_only_known_values() -> None:
    from friday.integrations.workflows import ace_step_music

    template = ace_step_music()
    assert placeholders(template) == {
        "checkpoint",
        "prompt",
        "lyrics",
        "seed",
        "bpm",
        "seconds",
        "key",
    }
    filled = fill_workflow(
        template,
        {
            "checkpoint": "ace.safetensors",
            "prompt": "calm piano",
            "lyrics": "[instrumental]",
            "bpm": 110,
            "seconds": 30,
            "key": "C major",
        },
    )
    assert filled["94"]["inputs"]["seed"] == filled["3"]["inputs"]["seed"]
    assert filled["98"]["inputs"]["seconds"] == 30


@pytest.mark.parametrize(
    ("text", "kind", "prompt"),
    [
        (
            "Ah, it's good. Can you generate an image of a cat on a rainy day sitting on a wall?",
            Kind.IMAGE,
            "a cat on a rainy day sitting on a wall",
        ),
        ("Thanks! Now make some music about the sea.", Kind.MUSIC, "music about the sea"),
        ("I like cats. They are nice.", None, None),
        ("Make a picture of a fox. Thanks.", Kind.IMAGE, "a fox"),
    ],
)
def test_request_after_other_sentences(text: str, kind: Kind | None, prompt: str | None) -> None:
    found = parse_request(text)
    if kind is None:
        assert found is None
    else:
        assert found == Request(kind, prompt or "")


async def test_the_owners_first_voice_test(tmp_path: Path) -> None:
    """The conversation from the first voice test, 2026-11-21."""
    fake = FakeComfy()
    studio, _, _ = _studio(fake, tmp_path)
    studio.handle("Can you generate an image of a government dog sitting in front of a home?")
    assert studio.handle("No, I meant Doberman Dog.") == "Picture: Doberman Dog. Shall I make it?"
    assert studio.handle("A Doberman dog sitting in front of a home.") == (
        "Picture: A Doberman dog sitting in front of a home. Shall I make it?"
    )
    assert studio.handle("Just don't do anything.") == "Okay, I won't make it."
    assert studio.pending is None
    studio.handle("make a picture of a cat")
    assert studio.handle("Cancel the picture request.") == "I'm not making anything right now."
    assert studio.pending is None and fake.queued == []


@pytest.mark.parametrize("answer", ["never mind", "forget it", "stop", "leave it, thanks"])
async def test_ways_of_dropping_a_read_back(tmp_path: Path, answer: str) -> None:
    studio, _, _ = _studio(FakeComfy(), tmp_path)
    studio.handle("make a picture of a cat")
    assert studio.handle(answer) == "Okay, I won't make it."
