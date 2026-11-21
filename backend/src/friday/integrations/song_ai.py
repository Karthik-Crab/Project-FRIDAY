"""AI changes to a song the owner attached, through ACE-Step 1.5's own server.

What the owner can ask for (with a song attached):

- **add** an instrument part: "add a funky bass line", "put drums on this"
  (ACE-Step "lego": it plays a new part that fits the song)
- **pull out** one instrument: "give me just the vocals" ("extract")
- **restyle** the whole song: "make it a jazz version" ("cover")
- **redo** a stretch: "redo 0:30 to 0:45 with a guitar solo" ("repaint")

These need ACE-Step's *base* model (``acestep-v15-base``) and run on the
graphics card. The server is started by FRIDAY when needed (127.0.0.1 only).
A bass *boost* is not this: that is ffmpeg, see ``audio_tools``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from friday.core.errors import FridayError
from friday.integrations.voice_service import VoiceServiceLauncher

log = logging.getLogger(__name__)

# ACE-Step's instrument names, and the words people use for them.
_TRACKS: dict[str, str] = {
    "backing_vocals": r"backing\s+vocals?|harmon(?:y|ies)|choir|backing\s+singers?",
    "vocals": r"vocals?|voices?|singing|singers?|lyrics",
    "bass": r"bass(?:\s*line|\s+guitar|\s+part|\s+track|\s+riff)?|basslines?|sub\s*bass",
    "drums": r"drums?|drum\s*(?:beat|kit|track|part)s?|beats?",
    "percussion": r"percussion|tabla|congas?|bongos?|shakers?|tambourines?|claps?",
    "guitar": r"(?:acoustic\s+|electric\s+)?guitars?(?:\s+(?:solo|riff|part))?|riffs?",
    "keyboard": r"pianos?|keys|keyboards?|organs?|rhodes",
    "strings": r"strings|violins?|cellos?|violas?|orchestra(?:l)?",
    "synth": r"synths?|synthesi[sz]ers?|pads?",
    "brass": r"brass|trumpets?|trombones?|horns?|tubas?",
    "woodwinds": r"woodwinds?|flutes?|sax(?:ophone)?s?|clarinets?|oboes?|bansuri",
    "fx": r"sound\s+effects|fx|effects",
}
_TRACK_WORD = {name: re.compile(rf"\b(?:{words})\b", re.I) for name, words in _TRACKS.items()}

_EXTRACT = re.compile(
    r"\b(?:extract|isolate|separate|split\s+out|pull\s+out|take\s+out|get\s+me|give\s+me|"
    r"just\s+the|only\s+the|keep\s+only)\b",
    re.I,
)
_ADD = re.compile(
    r"\b(?:add|put|play|layer|throw\s+in|bring\s+in|give\s+it|with\s+(?:a|an|some)\s+new)\b",
    re.I,
)
_TIME = r"(\d{1,2}:\d{2}|\d+(?:\.\d+)?\s*(?:s|sec|secs|seconds)?)"
_REPAINT = re.compile(
    rf"\b(?:redo|regenerate|remake|replace|rewrite|change|fix)\b.*?(?:\b(?:from|between)\s+|\s){_TIME}"
    rf"\s*(?:to|and|-|until|till)\s*{_TIME}",
    re.I,
)
_STYLE = re.compile(
    r"\b(?:make|turn|remix|redo|change|convert)\s+(?:it|this|the\s+song|this\s+song|the\s+track)?"
    r"\s*(?:into|as|to|in\s+the\s+style\s+of|sound\s+like|like|in)\s+(?:an?\s+)?(.+)",
    re.I,
)
_STYLE_VERSION = re.compile(r"\b([\w\s-]{2,40}?)\s+(?:version|cover|remix|style)\b", re.I)
_FILLER = re.compile(
    r"^(?:please\s+|can\s+you\s+|could\s+you\s+|friday,?\s+)*"
    r"|\s*(?:to|on|over|into)\s+(?:this|the|my)\s+(?:song|track|music|tune)\b.*$"
    r"|\s*please\.?$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class SongTask:
    task: str  # lego, extract, cover, repaint
    caption: str  # what the new part or style should sound like
    track: str | None = None
    start: float | None = None
    end: float | None = None

    @property
    def describe(self) -> str:
        name = (self.track or "").replace("_", " ")
        if self.task == "lego":
            return f"add {name}: {self.caption}" if self.caption != name else f"add {name}"
        if self.task == "extract":
            return f"just the {name}"
        if self.task == "cover":
            return f"new style: {self.caption}"
        assert self.start is not None and self.end is not None
        return f"redo {_clock(self.start)} to {_clock(self.end)}: {self.caption}"

    @property
    def tag(self) -> str:
        """A short word for the file name."""
        if self.task == "lego":
            return f"with {(self.track or 'part').replace('_', ' ')}"
        if self.task == "extract":
            return f"only {(self.track or 'part').replace('_', ' ')}"
        return "new style" if self.task == "cover" else "redone"


def _clock(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def _seconds(text: str) -> float:
    text = text.strip().lower()
    if ":" in text:
        minutes, seconds = text.split(":")
        return int(minutes) * 60 + int(seconds)
    return float(re.sub(r"[a-z\s]+$", "", text))


def _track(text: str) -> str | None:
    """The first instrument named in ``text``.

    "backing vocals" starts before the "vocals" inside it, so it wins.
    """
    found = [(m.start(), name) for name, rx in _TRACK_WORD.items() if (m := rx.search(text))]
    return min(found)[1] if found else None


def _clean(text: str) -> str:
    text = text.strip().rstrip("?.!")
    for _ in range(3):
        text = _FILLER.sub("", text).strip()
    return text


def parse_song_task(text: str) -> SongTask | None:
    """What the owner wants done to the attached song, or None."""
    text = text.strip()
    if not text:
        return None
    if match := _REPAINT.search(text):
        start, end = _seconds(match.group(1)), _seconds(match.group(2))
        if end > start:
            rest = text[match.end() :].strip(" ,.")
            rest = re.sub(r"^(?:with|as|into|so\s+it\s+has)\s+", "", rest, flags=re.I)
            return SongTask("repaint", rest or "same style", start=start, end=end)
    track = _track(text)
    if track and _EXTRACT.search(text):
        return SongTask("extract", track.replace("_", " "), track=track)
    if track and _ADD.search(text):
        caption = re.sub(
            r"^(?:add|put|play|layer|throw\s+in|bring\s+in|give\s+it)\s+(?:in\s+)?"
            r"(?:a\s+new\s+|an?\s+|some\s+)?",
            "",
            _clean(text),
            flags=re.I,
        )
        return SongTask("lego", caption or track.replace("_", " "), track=track)
    if (match := _STYLE.search(text)) or (match := _STYLE_VERSION.search(text)):
        style = _clean(match.group(1))
        style = re.sub(
            r"^(?:make|turn|do|give\s+me)\s+(?:it|this)?\s*(?:into\s+)?(?:an?\s+)?",
            "",
            style,
            flags=re.I,
        )
        style = re.sub(r"\s+(?:version|cover|remix|style|song)$", "", style, flags=re.I)
        if style and not re.fullmatch(r"(?:it|this|better|nice|good|louder|softer)", style, re.I):
            return SongTask("cover", style)
    return None


class SongAIError(FridayError):
    pass


class AceStepClient:
    """ACE-Step 1.5's API server: submit a task, wait for it, fetch the result."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001",
        *,
        model: str = "acestep-v15-base",
        steps: int = 50,
        timeout_s: float = 900.0,
        poll_s: float = 2.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.steps = steps
        self.timeout_s = timeout_s
        self._poll_s = poll_s
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    def form(self, task: SongTask) -> dict[str, str]:
        """The form fields for ``task`` (the song itself goes as a file)."""
        fields: dict[str, Any] = {
            "task_type": task.task,
            "prompt": task.caption,
            "model": self.model,
            "inference_steps": self.steps,
            "batch_size": 1,
            "thinking": False,  # no planning model: it would need more of the card
            "use_cot_caption": False,
            "use_cot_language": False,
            "audio_format": "mp3",
        }
        if task.track:
            fields["instruction"] = (
                f"Generate the {task.track} track based on the audio context:"
                if task.task == "lego"
                else f"Extract the {task.track} track from the audio:"
            )
        if task.task == "repaint":
            fields["repainting_start"] = task.start
            fields["repainting_end"] = task.end
        if task.task == "cover":
            fields["audio_cover_strength"] = 0.5  # keep the tune, change the sound
        return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in fields.items()}

    async def run(self, source: Path, task: SongTask, target: Path) -> Path:
        """Do ``task`` to ``source`` and save the result as ``target``."""
        try:
            with source.open("rb") as song:
                response = await self._client.post(
                    f"{self.base_url}/release_task",
                    data=self.form(task),
                    files={"src_audio": (source.name, song, "application/octet-stream")},
                )
            task_id = _data(response)["task_id"]
            log.info("ACE-Step task %s: %s", task_id, task.describe)
            result = await self._wait(task_id)
            url = result.get("file")
            if not url:
                raise SongAIError("ACE-Step finished but sent no file")
            async with self._client.stream("GET", f"{self.base_url}{url}") as download:
                if download.status_code != 200:
                    raise SongAIError(f"couldn't fetch the result ({download.status_code})")
                with target.open("wb") as out:
                    async for chunk in download.aiter_bytes():
                        out.write(chunk)
        except httpx.HTTPError as exc:
            target.unlink(missing_ok=True)  # noqa: ASYNC240 (instant)
            raise SongAIError(f"ACE-Step stopped answering ({type(exc).__name__})") from exc
        except BaseException:
            target.unlink(missing_ok=True)  # noqa: ASYNC240 (instant)
            raise
        return target

    async def _wait(self, task_id: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while loop.time() < deadline:
            await asyncio.sleep(self._poll_s)
            response = await self._client.post(
                f"{self.base_url}/query_result", json={"task_id_list": [task_id]}
            )
            items = _data(response)
            item = items[0] if isinstance(items, list) and items else {}
            status = item.get("status", 0)
            if status == 0:
                continue
            results = item.get("result")
            parsed = json.loads(results) if isinstance(results, str) and results else results
            first = parsed[0] if isinstance(parsed, list) and parsed else {}
            if status == 1 and isinstance(first, dict):
                return first
            reason = (first.get("error") if isinstance(first, dict) else None) or item.get("error")
            raise SongAIError(f"ACE-Step couldn't do it: {reason or 'no reason given'}")
        raise SongAIError(f"ACE-Step took longer than {self.timeout_s / 60:.0f} minutes")


def _data(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError as exc:
        raise SongAIError(f"ACE-Step sent something unexpected ({response.status_code})") from exc
    if response.status_code != 200 or body.get("code", 200) != 200:
        detail = body.get("error") or body.get("detail") or response.status_code
        raise SongAIError(f"ACE-Step refused: {detail}")
    return body.get("data")


def output_path(source: Path, task: SongTask, folder: Path) -> Path:
    stem = re.sub(r"[^\w\- ]+", "", source.stem).strip() or "song"
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (path := folder / f"{stem} ({task.tag} {number}).mp3").exists():
        number += 1
    return path


class SongEditor:
    """Starts ACE-Step's server when needed, runs one change, stops it again."""

    def __init__(
        self,
        client: AceStepClient,
        launcher: VoiceServiceLauncher | None = None,
        *,
        stop_after: bool = True,
    ) -> None:
        self.client = client
        self.launcher = launcher
        self.stop_after = stop_after

    @property
    def model(self) -> str:
        return self.client.model

    async def run(self, source: Path, task: SongTask, target: Path) -> Path:
        if self.launcher is not None:
            await self.launcher.start()
        try:
            return await self.client.run(source, task, target)
        finally:
            if self.launcher is not None and self.stop_after:
                self.launcher.stop()
