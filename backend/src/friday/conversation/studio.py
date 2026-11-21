"""Making pictures and music: spotting the request, then running the job.

Like passing tasks to Claude Code, this is fixed logic, not the language model:
a recognised phrase ("make a picture of ...", "compose a song about ...")
starts it, FRIDAY reads the request back, and only a spoken "yes" starts the
job. A small model can never start a job by itself, and a misheard request is
caught before the graphics card is taken over for a minute.

The job runs in the background while the conversation carries on:

1. ComfyUI is started if it is not running (the command is in the config).
2. The talking model on the graphics card is set aside and unloaded; FRIDAY
   keeps talking through the next model in her route (the CPU one).
3. The workflow is filled in and queued; FRIDAY waits for the files.
4. ComfyUI is asked to free the card, and the talking model comes back.

A ``Notice`` reports the outcome through ``on_done``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from friday.conversation.delegation import Notice, is_no, is_yes
from friday.core.errors import FridayError
from friday.integrations.comfyui import ComfyClient, ComfyLauncher, fill_workflow, open_file
from friday.llm.router import ProviderRouter

T = TypeVar("T")

log = logging.getLogger(__name__)


class Kind(StrEnum):
    IMAGE = "image"
    MUSIC = "music"


# What each kind is called when FRIDAY speaks about it.
_SPOKEN = {Kind.IMAGE: "picture", Kind.MUSIC: "music"}
_LABEL = {Kind.IMAGE: "Picture", Kind.MUSIC: "Music"}

_SEP = r"[\s,.:;!?-]+"
_LEAD = (
    rf"^\W*(?:(?:hey|ok|okay|so|now|and|also|alright|right|well|then){_SEP}){{0,3}}"
    rf"(?:friday{_SEP})?(?:(?:now|then){_SEP})?(?:please{_SEP})?"
    rf"(?:(?:can|could|would|will){_SEP}you{_SEP})?(?:please{_SEP})?"
    rf"(?:(?:i{_SEP}(?:want|d{_SEP}like|would{_SEP}like){_SEP}you{_SEP}to){_SEP})?"
    rf"(?:(?:go{_SEP}ahead{_SEP}and|quickly|just){_SEP})?"
)
_ARTICLE = rf"(?:(?:me|us){_SEP})?(?:(?:a|an|the|some|another|one|new|nice){_SEP})?"
# Up to four describing words before the noun ("a realistic photo", "some
# relaxing piano music"); a preposition there means it is something else
# ("a story about music").
_NOT_STYLE = r"of|about|for|with|on|to|in|from|showing|that|and"
_STYLE = rf"(?P<style>(?:(?!(?:{_NOT_STYLE})\b)[\w'&-]+{_SEP}){{0,4}}?)"
_IMAGE_VERBS = r"make|generate|create|draw|paint|render|design|produce|imagine|show"
_IMAGE_NOUNS = (
    r"images?|pictures?|pics?|photos?|photographs?|drawings?|paintings?|illustrations?|"
    r"artworks?|art|wallpapers?|portraits?|sketch(?:es)?|posters?|logos?|renders?"
)
_MUSIC_VERBS = r"make|generate|create|compose|write|produce"
_MUSIC_NOUNS = (
    r"songs?|music|tracks?|tunes?|melod(?:y|ies)|beats?|jingles?|soundtracks?|"
    r"instrumentals?|lullab(?:y|ies)|anthems?|ballads?|raps?"
)
_IMAGE_LINK = r"of|showing|depicting|with|for|about|where|that{_SEP}shows?"
_MUSIC_LINK = r"about|of|for|with|that|like|where|in{_SEP}the{_SEP}style{_SEP}of"


def _request(verbs: str, nouns: str, links: str) -> re.Pattern[str]:
    links = links.replace("{_SEP}", _SEP)
    return re.compile(
        rf"{_LEAD}(?:{verbs}){_SEP}{_ARTICLE}{_STYLE}(?P<noun>{nouns})(?=$|{_SEP})"
        rf"(?:{_SEP}(?P<link>{links})\b)?(?:{_SEP}(?P<subject>\S.*?))?\W*$",
        re.IGNORECASE | re.DOTALL,
    )


_IMAGE = _request(_IMAGE_VERBS, _IMAGE_NOUNS, _IMAGE_LINK)
_MUSIC = _request(_MUSIC_VERBS, _MUSIC_NOUNS, _MUSIC_LINK)
# "Draw a fox in the snow", "paint me a sunset": the verb says it all.
# Not a picture: "draw conclusions", "draw up a plan", "paint the wall".
_NOT_DRAWING = (
    r"conclusions?|attention|up|on|from|out|back|near|breath|blood|straws|lots|"
    rf"(?:the|a){_SEP}line|the{_SEP}(?:walls?|house|room|door|fence)"
)
_DRAW = re.compile(
    rf"{_LEAD}(?:draw|paint|sketch){_SEP}(?:(?:me|us){_SEP})?(?!(?:{_NOT_DRAWING})\b)"
    rf"(?P<subject>\S.*?)\W*$",
    re.IGNORECASE | re.DOTALL,
)
_ANY_NOUN = rf"(?:{_IMAGE_NOUNS}|{_MUSIC_NOUNS}|photo|drawing|song)"
_CANCEL = re.compile(
    rf"{_LEAD}(?:cancel|stop|abort){_SEP}(?:(?:the|that|my){_SEP})?(?P<noun>{_ANY_NOUN})"
    rf"(?:{_SEP}(?:job|task|request))?\W*$",
    re.IGNORECASE,
)
# While a read-back waits for "yes": the owner does not want it after all.
_DROP = re.compile(
    r"\b(?:cancel|stop|abort|never\s*mind|forget\s+(?:it|that|about\s+it)|"
    r"don'?t\s+(?:do|make|bother)|do\s+not\s+(?:do|make)|leave\s+it|drop\s+it)\b",
    re.IGNORECASE,
)
# "No, I meant a Doberman": the words after it replace the description.
_CORRECTION = re.compile(
    rf"^\W*(?:no|nope|not\s+that|wait)\b{_SEP}(?:(?:i{_SEP}(?:meant|mean|said|wanted|want)|"
    rf"make{_SEP}it|it{_SEP}should{_SEP}(?:be|show)|instead){_SEP})*(?P<rest>\w.*?)\W*$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Request:
    kind: Kind
    prompt: str  # "" = the user did not say what it should be yet


# Where a new sentence starts: after . ! or ? and a space.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def parse_request(text: str) -> Request | None:
    """Is this a request for a picture or music? Returns what to make.

    The request may come after something else ("Ah, it's good. Can you make a
    picture of ..."), so each sentence is tried, the last one first.
    """
    text = text.strip()
    sentences = _SENTENCE.split(text)
    if len(sentences) > 1:
        for sentence in reversed(sentences):
            found = _parse_sentence(sentence.strip())
            if found:
                return found
        return None
    return _parse_sentence(text)


def _parse_sentence(text: str) -> Request | None:
    for kind, pattern in ((Kind.IMAGE, _IMAGE), (Kind.MUSIC, _MUSIC)):
        match = pattern.match(text)
        if not match:
            continue
        style = (match.group("style") or "").strip(" ,.:;!?-")
        subject = (match.group("subject") or "").strip()
        link = match.group("link")
        if subject and not link and not style:
            # "make the picture brighter": about an existing one, not a new one.
            continue
        if kind is Kind.IMAGE:
            prompt = f"{style} {match.group('noun')} {link or ''} {subject}" if style else subject
        elif style or subject:
            prompt = text[match.start("style") :].strip(" .!?")
        else:
            prompt = ""  # "generate music": ask what kind
        return Request(kind, re.sub(r"\s+", " ", prompt).strip(" ,.:;!?-"))
    match = _DRAW.match(text)
    if match:
        return Request(Kind.IMAGE, match.group("subject").strip(" ,.:;!?-"))
    return None


def is_studio_command(text: str) -> bool:
    return parse_request(text) is not None or bool(_CANCEL.match(text.strip()))


def not_set_up_reply(kind: Kind) -> str:
    """Fixed words, so a small model never pretends it made something."""
    if kind is Kind.MUSIC:
        return "I can't make music yet. It isn't set up on this computer, so nothing was made."
    return "I can't make pictures yet. It isn't set up on this computer, so nothing was made."


@dataclass(frozen=True, slots=True)
class Workflow:
    """A ComfyUI workflow in API format, with the extra values it needs."""

    template: Mapping[str, Any]
    values: Mapping[str, str | int | float] = field(default_factory=dict)


class StudioDelegate:
    def __init__(
        self,
        comfy: ComfyClient,
        workflows: Mapping[Kind, Workflow],
        output_dir: Path,
        *,
        launcher: ComfyLauncher | None = None,
        router: ProviderRouter | None = None,
        gpu_providers: Sequence[str] = (),
        gpu_occasional: Sequence[str] = (),
        timeout_s: float = 600.0,
        open_results: bool = True,
        on_done: Callable[[Notice], None] | None = None,
        opener: Callable[[Path], None] = open_file,
        music_dir: Path | None = None,
        edit_workflow: Workflow | None = None,
    ) -> None:
        self.comfy = comfy
        self.workflows = dict(workflows)
        self.output_dir = output_dir
        self.music_dir = music_dir or output_dir
        self.edit_workflow = edit_workflow  # changing an attached picture
        self.launcher = launcher
        self.router = router
        self.gpu_providers = tuple(gpu_providers)
        # Card models used now and then (the one that reads pictures): set
        # aside during a job like the others, but not loaded again afterwards.
        self.gpu_occasional = tuple(gpu_occasional)
        self.timeout_s = timeout_s
        self.open_results = open_results
        self.on_done = on_done
        self._opener = opener
        self.pending: Request | None = None  # read back, waiting for "yes"
        self._job: asyncio.Task[None] | None = None
        self._job_kind: Kind | None = None
        # One job on the graphics card at a time; others wait their turn.
        self._card = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._job is not None and not self._job.done()

    @property
    def awaiting_answer(self) -> bool:
        return self.pending is not None

    def concerns(self, text: str) -> bool:
        return self.pending is not None or is_studio_command(text)

    def dismiss(self) -> None:
        self.pending = None

    def handle(self, text: str) -> str | None:
        """FRIDAY's reply if this is about making a picture or music, else None."""
        cancel = _CANCEL.match(text)
        if cancel:
            self.pending = None
            if not self.busy:
                return "I'm not making anything right now."
            assert self._job is not None
            self._job.cancel()
            return "Okay, I've stopped it."
        request = parse_request(text)
        if request is not None:
            return self._start(request)
        if self.pending is None:
            return None
        if not self.pending.prompt:  # answering "what should it be?"
            return self._start(Request(self.pending.kind, text.strip(" .!?")))
        if is_yes(text):
            request, self.pending = self.pending, None
            self._job_kind = request.kind
            self._job = asyncio.get_running_loop().create_task(self._run(request))
            what = "the picture" if request.kind is Kind.IMAGE else "the music"
            return (
                f"Okay, I'm making {what}. It takes a minute or two; I'll tell you when it's ready."
            )
        if is_no(text) or _DROP.search(text):
            self.pending = None
            return "Okay, I won't make it."
        correction = _CORRECTION.match(text)
        if correction:
            self.pending = Request(self.pending.kind, correction.group("rest"))
            return self._read_back()
        if len(text.split()) < 3:
            return "Say yes to make it, or no to drop it."
        # Neither yes nor no: a new description, which replaces the old one.
        # (Adding it on made a long muddle out of every correction.)
        self.pending = Request(self.pending.kind, text.strip(" .!?"))
        return self._read_back()

    def _start(self, request: Request) -> str:
        if request.kind not in self.workflows:
            self.pending = None
            return not_set_up_reply(request.kind)
        if self.busy:
            self.pending = None
            what = "picture" if self._job_kind is Kind.IMAGE else "music"
            return f"I'm still making the {what}. Say cancel the {what} if you want to stop it."
        self.pending = request
        if not request.prompt:
            if request.kind is Kind.IMAGE:
                return "Sure. What should the picture show?"
            return "Sure. What should the music be like?"
        return self._read_back()

    def _read_back(self) -> str:
        assert self.pending is not None
        return f"{_LABEL[self.pending.kind]}: {self.pending.prompt}. Shall I make it?"

    async def _run(self, request: Request) -> None:
        notice = await self.make(request)
        if self.on_done is not None:
            try:
                self.on_done(notice)
            except Exception:  # a broken listener must not lose the result
                log.exception("studio listener failed")

    @property
    def queued_or_running(self) -> bool:
        return self._card.locked()

    async def make(self, request: Request, on_start: Callable[[], None] | None = None) -> Notice:
        """Run one job (after any job already running) and return what to tell the user.

        ``on_start`` is called when its turn comes and the work begins.
        """
        async with self._card:
            if on_start is not None:
                on_start()
            return await self._make(request)

    async def exclusive(
        self, work: Callable[[], Awaitable[T]], on_start: Callable[[], None] | None = None
    ) -> T:
        """Run ``work`` with the graphics card to itself (after any job in front).

        For other programs that need the card, such as ACE-Step changing a song:
        ComfyUI lets go of its models and the talking models step aside.
        """
        async with self._card:
            if on_start is not None:
                on_start()
            with contextlib.suppress(Exception):
                if await self.comfy.is_up():
                    await self.comfy.free()
            held = await self._take_card()
            try:
                return await work()
            finally:
                await self._give_back(held)

    async def edit_picture(
        self, source: Path, instruction: str, on_start: Callable[[], None] | None = None
    ) -> Path:
        """Change ``source`` as ``instruction`` says; returns the new picture.

        Waits its turn for the graphics card like any other job. Raises
        FridayError when it can't be done.
        """
        if self.edit_workflow is None:
            raise FridayError("changing pictures is not set up")
        async with self._card:
            if on_start is not None:
                on_start()
            held: list[str] = []
            try:
                if self.launcher is not None:
                    await self.launcher.ensure_running(self.comfy)
                elif not await self.comfy.is_up():
                    raise FridayError("ComfyUI isn't running")
                held = await self._take_card()
                image = await self.comfy.upload_image(source)
                workflow = self.edit_workflow
                filled = fill_workflow(
                    workflow.template, {**workflow.values, "prompt": instruction, "image": image}
                )
                prompt_id = await self.comfy.queue(filled)
                files = await self.comfy.wait(prompt_id, self.timeout_s)
                return await self.comfy.download(files[0], self.output_dir)
            except asyncio.CancelledError:
                await self.comfy.interrupt()
                raise
            finally:
                await self._give_back(held)

    async def _make(self, request: Request) -> Notice:
        held: list[str] = []
        path: Path | None = None
        try:
            if self.launcher is not None:
                await self.launcher.ensure_running(self.comfy)
            elif not await self.comfy.is_up():
                raise FridayError("ComfyUI isn't running")
            held = await self._take_card()
            workflow = self.workflows[request.kind]
            filled = fill_workflow(workflow.template, {**workflow.values, "prompt": request.prompt})
            prompt_id = await self.comfy.queue(filled)
            files = await self.comfy.wait(prompt_id, self.timeout_s)
            folder = self.music_dir if request.kind is Kind.MUSIC else self.output_dir
            saved = [await self.comfy.download(f, folder) for f in files]
            path = saved[0]
            spoken = self._ready_words(request.kind, len(saved))
            if self.open_results:
                self._opener(path)
            ok = True
        except asyncio.CancelledError:
            await self.comfy.interrupt()
            await self._give_back(held)
            raise
        except FridayError as exc:
            log.error("studio job failed: %s", exc)
            what = "the picture" if request.kind is Kind.IMAGE else "the music"
            spoken = f"I couldn't make {what}. {str(exc).rstrip('.')}."
            ok = False
        await self._give_back(held)
        return Notice(task=request.prompt, spoken=spoken, ok=ok, log_path=path)

    def _ready_words(self, kind: Kind, count: int) -> str:
        if kind is Kind.IMAGE:
            thing = "Your picture is ready." if count == 1 else f"Your {count} pictures are ready."
        else:
            thing = "Your music is ready."
        return f"{thing} I've opened it." if self.open_results else f"{thing} It's saved."

    async def _take_card(self) -> list[str]:
        """Set the graphics-card talking models aside and unload them."""
        if self.router is None:
            return []
        held: list[str] = []
        for name in (*self.gpu_providers, *self.gpu_occasional):
            provider = self.router.providers.get(name)
            if provider is None:
                continue
            self.router.hold(name)
            held.append(name)
            unload = getattr(provider, "unload", None)
            if unload is not None:
                await unload()
        return held

    async def _give_back(self, held: list[str]) -> None:
        with contextlib.suppress(Exception):
            await self.comfy.free()
        if self.router is None:
            return
        for name in held:
            self.router.release(name)
            if name not in self.gpu_providers:
                continue
            warm_up = getattr(self.router.providers.get(name), "warm_up", None)
            if warm_up is not None:  # load it again now, not on the next question
                with contextlib.suppress(Exception):
                    await warm_up()

    async def aclose(self) -> None:
        if self.busy:
            assert self._job is not None
            self._job.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._job
        await self.comfy.aclose()
