"""The graphics card: voice mode and jobs take turns without colliding."""

from __future__ import annotations

import asyncio
import threading
from http.server import ThreadingHTTPServer
from typing import Any

import httpx
from test_voice_units import _voice_server_module

from friday.integrations.card import GraphicsCard
from friday.integrations.voice_service import VoiceServiceLauncher
from friday.llm.router import ProviderRouter


class Model:
    """A talking model on the card, as OllamaProvider: unload, warm_up."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    async def unload(self) -> None:
        self.log.append(f"{self.name} unloaded")

    async def warm_up(self, request: Any = None) -> None:
        self.log.append(f"{self.name} loaded")


class Voice:
    def __init__(self, log: list[str], movable: bool = True) -> None:
        self.log, self.up, self.movable, self.on = log, False, movable, False

    @property
    def started_here(self) -> bool:
        return self.up

    async def is_up(self) -> bool:
        return self.up

    async def start(self) -> bool:
        if self.up:
            return False
        self.log.append("voice started")
        self.up = True
        return True

    def stop(self) -> None:
        if self.up:
            self.log.append("voice stopped")
        self.up = self.on = False

    async def to_card(self, on: bool) -> bool:
        if not self.movable:
            return False
        self.log.append("voice onto the card" if on else "voice into RAM")
        self.on = on
        return True


class Comfy:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def is_up(self) -> bool:
        return True

    async def free(self, *, release_ram: bool = False) -> None:
        self.log.append("ComfyUI let go")


async def _plenty() -> int:
    return 8000


def _card(log: list[str], **kw: Any) -> tuple[GraphicsCard, ProviderRouter, Voice]:
    router = ProviderRouter(
        {"gpu": Model("talking model", log), "vision": Model("picture reader", log)},
        {"default": ["gpu"]},
    )
    voice = Voice(log, kw.pop("movable", True))
    kw.setdefault("free_mb", _plenty)
    card = GraphicsCard(router, ["gpu"], ["vision"], voice=voice, comfy=Comfy(log), **kw)
    return card, router, voice


async def test_voice_on_makes_room_then_the_talking_model_comes_back_beside_it() -> None:
    log: list[str] = []
    card, router, voice = _card(log)
    await card.prepare_voice()
    assert log == ["voice started"] and not voice.on  # waiting in RAM, not on the card
    log.clear()
    await card.voice_on()
    assert log == [
        "ComfyUI let go",
        "talking model unloaded",
        "picture reader unloaded",
        "voice onto the card",
        "talking model loaded",
    ]
    assert voice.on and not router.held
    log.clear()
    await card.voice_off()
    assert log == ["voice into RAM", "talking model unloaded", "talking model loaded"]
    assert voice.up and not voice.on  # kept in RAM for a quick start next time


async def test_a_job_moves_the_voice_off_the_card_and_back_afterwards() -> None:
    log: list[str] = []
    card, router, voice = _card(log)
    await card.voice_on()
    log.clear()
    await card.begin_job()
    assert log == ["voice into RAM", "talking model unloaded", "picture reader unloaded"]
    assert router.held == {"gpu", "vision"} and not voice.on  # FRIDAY talks through the CPU
    log.clear()
    await card.end_job()
    assert "voice onto the card" in log and log[-1] == "talking model loaded"
    assert voice.on and not router.held


async def test_voice_switched_on_during_a_job_joins_when_the_job_is_done() -> None:
    log: list[str] = []
    card, _, voice = _card(log)
    await card.begin_job()
    await card.voice_on()
    assert not voice.on  # the job has the card; the built-in voice speaks meanwhile
    await card.end_job()
    assert voice.on


async def test_without_a_job_or_voice_the_talking_model_gets_the_card_back() -> None:
    log: list[str] = []
    card, router, voice = _card(log)
    await card.begin_job()
    log.clear()
    await card.end_job()
    assert log == ["talking model loaded"]  # not the picture reader: it loads when needed
    assert not router.held and not voice.up


async def test_without_keeping_it_in_ram_the_voice_service_stops() -> None:
    log: list[str] = []
    card, _, voice = _card(log, keep_voice_in_ram=False)
    await card.prepare_voice()
    assert not voice.up  # not loaded ahead of time
    await card.voice_on()
    await card.voice_off()
    assert "voice stopped" in log and not voice.up


async def test_an_older_voice_service_that_cant_move_is_stopped_instead() -> None:
    log: list[str] = []
    card, _, voice = _card(log, movable=False)
    await card.voice_on()
    assert card.voice_on_card
    await card.voice_off()
    assert "voice stopped" in log


async def test_it_waits_until_the_card_really_has_room() -> None:
    readings = iter([500, 1200, 4000])
    asked: list[int] = []

    async def free() -> int:
        value = next(readings)
        asked.append(value)
        return value

    log: list[str] = []
    card, _, _ = _card(log, free_mb=free)
    await card.voice_on()
    assert asked == [500, 1200, 4000]  # Ollama takes a moment to let go


async def test_the_voice_service_parks_in_ram_and_moves_onto_the_card() -> None:
    module = _voice_server_module()

    class StandIn:
        name, device, sample_rate, parked = "stand-in", "cpu", 22_050, True

        def move(self, on: bool) -> None:
            self.parked = not on
            self.device = "cuda" if on else "cpu"

        def speak(self, text: str) -> bytes:
            if self.parked:
                raise module.Parked
            return b"\x01\x00" * 10

    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(StandIn()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    launcher = VoiceServiceLauncher([], base)
    try:
        async with httpx.AsyncClient(base_url=base) as client:
            assert (await client.get("/health")).json()["on_card"] is False
            assert (await client.post("/speak", json={"text": "Hi"})).status_code == 503
            assert await launcher.to_card(True)
            assert (await client.get("/health")).json()["device"] == "cuda"
            assert (await client.post("/speak", json={"text": "Hi"})).status_code == 200
            assert await launcher.to_card(False)
            assert (await client.post("/speak", json={"text": "Hi"})).status_code == 503
            assert (await client.post("/card", json={"on": "yes"})).status_code == 400
    finally:
        server.shutdown()
        await launcher.aclose()


def test_parked_start_is_asked_for_through_the_environment() -> None:
    launcher = VoiceServiceLauncher(["x"], "http://127.0.0.1:5005", start_parked=True)
    assert launcher.env["FRIDAY_VOICE_PARKED"] == "1"
    asyncio.run(launcher.aclose())
