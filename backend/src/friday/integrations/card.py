"""The graphics card: who has it, and handing it over without collisions.

The card (8 GB on the owner's PC) is shared by:

* the talking model (Ollama, about 6 GB) and the one that reads pictures;
* the expressive voice (Chatterbox, about 2.7 GB) while voice mode is on;
* ComfyUI (pictures, music) and ACE-Step (changing songs) during a job.

Everything that needs the card goes through one ``GraphicsCard``, so the
rules live in one place:

* Voice on: ComfyUI lets go of its models, the card models step aside, the
  voice model moves onto the card, then the talking model loads again in the
  room that is left (partly in RAM; still faster than speech).
* A job (picture, music, song change): the voice model steps off the card
  for the job, and FRIDAY talks in her built-in voice meanwhile; the talking
  models are set aside and FRIDAY talks through the CPU model. Afterwards it
  all comes back.
* Voice off: the voice model leaves the card and the talking model gets the
  whole card again.

The voice model is kept loaded in RAM between uses (``keep_voice_in_ram``):
moving it onto the card takes about half a second, starting it from disk
about fifteen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from friday.llm.router import ProviderRouter

log = logging.getLogger(__name__)

VOICE_MB = 3000  # what the voice model needs on the card, with room to work


class VoiceService(Protocol):
    """The expressive voice service (``VoiceServiceLauncher``)."""

    @property
    def started_here(self) -> bool: ...

    async def is_up(self) -> bool: ...

    async def start(self) -> bool: ...

    def stop(self) -> None: ...

    async def to_card(self, on: bool) -> bool: ...


class Freeable(Protocol):
    """ComfyUI (``ComfyClient``)."""

    async def is_up(self) -> bool: ...

    async def free(self, *, release_ram: bool = False) -> None: ...


async def nvidia_free_mb() -> int | None:
    """Free memory on the first NVIDIA card, or None if it can't be read."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe,
            "--query-gpu=memory.free",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), 5)
        return int(out.decode().split()[0])
    except (OSError, ValueError, IndexError, TimeoutError):
        return None


class GraphicsCard:
    def __init__(
        self,
        router: ProviderRouter | None = None,
        gpu_providers: Sequence[str] = (),
        gpu_occasional: Sequence[str] = (),
        *,
        voice: VoiceService | None = None,
        comfy: Freeable | None = None,
        keep_voice_in_ram: bool = True,
        voice_mb: int = VOICE_MB,
        free_mb: Callable[[], Awaitable[int | None]] = nvidia_free_mb,
        wait_s: float = 15.0,
    ) -> None:
        self.router = router
        self.gpu_providers = tuple(gpu_providers)
        # Used now and then (reading pictures): set aside, never reloaded.
        self.gpu_occasional = tuple(gpu_occasional)
        self.voice = voice
        self.comfy = comfy
        self.keep_voice_in_ram = keep_voice_in_ram
        self.voice_mb = voice_mb
        self._free_mb = free_mb
        self._wait_s = wait_s
        self._lock = asyncio.Lock()
        self._jobs = 0
        self._held: list[str] = []
        self.voice_wanted = False
        self.voice_on_card = False

    # Voice mode ---------------------------------------------------------------

    async def voice_on(self) -> None:
        """Voice mode starts. During a job the voice joins once the job is done."""
        async with self._lock:
            self.voice_wanted = True
            if self._jobs == 0:
                await self._voice_to_card()

    async def voice_off(self) -> None:
        async with self._lock:
            self.voice_wanted = False
            was_on = self.voice_on_card
            await self._voice_off_card(stop=not self.keep_voice_in_ram)
            if was_on and self._jobs == 0:
                # The talking model loaded beside the voice: give it the whole card.
                await self._unload(self.gpu_providers)
                await self._warm(self.gpu_providers)

    async def prepare_voice(self) -> None:
        """Load the voice model into RAM ahead of time, so voice mode starts fast."""
        if self.voice is None or not self.keep_voice_in_ram:
            return
        try:
            if not await self.voice.is_up():
                await self.voice.start()
        except Exception as exc:
            log.warning("could not load the voice ahead of time: %s", exc)

    # Jobs ---------------------------------------------------------------------

    async def begin_job(self) -> None:
        """A job is about to use the card."""
        async with self._lock:
            self._jobs += 1
            if self._jobs > 1:
                return
            await self._voice_off_card(stop=False)
            self._held = self._hold(*self.gpu_providers, *self.gpu_occasional)
            await self._unload(self._held)

    async def end_job(self) -> None:
        """The job is done (ComfyUI has been asked to let go already)."""
        async with self._lock:
            self._jobs = max(0, self._jobs - 1)
            if self._jobs > 0:
                return
            held, self._held = self._held, []
            self._release(held)
            if self.voice_wanted:
                await self._voice_to_card()
            else:
                await self._warm([n for n in held if n in self.gpu_providers])

    # The steps ----------------------------------------------------------------

    async def _voice_to_card(self) -> None:
        if self.voice is None or self.voice_on_card:
            return
        if self.comfy is not None:
            with contextlib.suppress(Exception):
                if await self.comfy.is_up():
                    await self.comfy.free()
        names = (*self.gpu_providers, *self.gpu_occasional)
        held = self._hold(*names)  # nothing loads them again in the meantime
        try:
            await self._unload(names)
            await self._room_for(self.voice_mb)
            await self.voice.start()  # nothing happens if it's already running
            if not await self.voice.to_card(True):
                log.info("the voice service went on the card when it started")
            self.voice_on_card = True
        finally:
            self._release(held)
            await self._warm(self.gpu_providers)  # in the room that's left

    async def _voice_off_card(self, *, stop: bool) -> None:
        if self.voice is None:
            return
        on_card, self.voice_on_card = self.voice_on_card, False
        if stop:
            self.voice.stop()
            return
        if not on_card:
            return
        try:
            moved = await self.voice.to_card(False)
        except Exception as exc:
            log.warning("the voice didn't leave the card, stopping it: %s", exc)
            moved = False
        if not moved:
            self.voice.stop()

    async def _room_for(self, mb: int) -> None:
        """Wait until the card has ``mb`` free (unloading takes a moment)."""
        deadline = time.monotonic() + self._wait_s
        while True:
            free = await self._free_mb()
            if free is None or free >= mb:
                return
            if time.monotonic() >= deadline:
                log.warning("only %d MB free on the graphics card (wanted %d)", free, mb)
                return
            await asyncio.sleep(0.3)

    def _providers(self, names: Sequence[str]) -> list[tuple[str, Any]]:
        if self.router is None:
            return []
        return [(n, self.router.providers[n]) for n in names if n in self.router.providers]

    def _hold(self, *names: str) -> list[str]:
        held = [n for n, _ in self._providers(names)]
        for name in held:
            assert self.router is not None
            self.router.hold(name)
        return held

    def _release(self, names: Sequence[str]) -> None:
        for name in names:
            assert self.router is not None
            self.router.release(name)

    async def _unload(self, names: Sequence[str]) -> None:
        for _, provider in self._providers(names):
            unload = getattr(provider, "unload", None)
            if unload is not None:
                with contextlib.suppress(Exception):
                    await unload()

    async def _warm(self, names: Sequence[str]) -> None:
        for _, provider in self._providers(names):
            warm_up = getattr(provider, "warm_up", None)
            if warm_up is not None:  # load it now, not on the next question
                with contextlib.suppress(Exception):
                    await warm_up()

    def stop_voice(self) -> None:
        if self.voice is not None:
            self.voice.stop()
        self.voice_on_card = False
