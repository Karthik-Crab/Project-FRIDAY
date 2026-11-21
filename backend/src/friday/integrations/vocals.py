"""Taking the singing out of a song, or keeping only the singing, with Demucs.

Demucs is a free AI from Meta that splits a song into the voice and the
music. It runs as its own program (its own folder, on the processor by
default), so FRIDAY only starts it and picks up the file it writes.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from friday.core.errors import FridayError

REMOVE = "remove"  # the music without the singing (karaoke)
KEEP = "keep"  # the singing without the music

_V = r"(?:vocals?|voices?|singing|singers?|vocalists?)"
_TAKE_AWAY = (
    r"(?:remove|take\s+out|cut|delete|strip|mute|drop|get\s+rid\s+of|erase|eliminate|kill|lose)"
)
_REMOVE_VOICE = re.compile(
    rf"\b(?:{_TAKE_AWAY}\b(?:\s+\S+){{0,3}}?\s+{_V}|without\s+(?:the\s+|any\s+)?{_V}|"
    rf"no\s+{_V}|karaoke|instrumental|minus\s+track|backing\s+track|music\s+only|"
    r"only\s+(?:the\s+)?music|just\s+(?:the\s+)?music)\b",
    re.I,
)
_KEEP_VOICE = re.compile(
    rf"\b(?:{_V}\s+only|only\s+(?:the\s+)?{_V}|just\s+(?:the\s+)?{_V}|a\s*cap+el+a|"
    rf"(?:isolate|extract|separate|keep|pull\s+out)\b(?:\s+\S+){{0,3}}?\s+{_V}|"
    rf"{_TAKE_AWAY}\b(?:\s+\S+){{0,3}}?\s+(?:music|instruments?|instrumentals?))\b",
    re.I,
)


def parse_vocal_task(text: str) -> str | None:
    """REMOVE, KEEP, or None when the words aren't about the singing.

    "remove the vocals only" means take the vocals out, so removing is
    checked first; "keep the vocals", "only the voice" mean keep them.
    """
    if _REMOVE_VOICE.search(text):
        return REMOVE
    if _KEEP_VOICE.search(text):
        return KEEP
    return None


def describe(mode: str) -> str:
    return "singing taken out" if mode == REMOVE else "only the singing"


def note(mode: str) -> str:
    kept = "the music without the singing" if mode == REMOVE else "the singing on its own"
    return (
        f"Demucs (an AI) split the song into voice and music; this is {kept}. On old "
        "recordings a faint trace of the other part can stay. It takes about a minute "
        "for a 3-minute song."
    )


def output_name(source: Path, mode: str, folder: Path) -> Path:
    stem = re.sub(r"[^\w\- ]+", "", source.stem).strip() or "song"
    tag = "no vocals" if mode == REMOVE else "vocals only"
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (path := folder / f"{stem} ({tag} {number}).mp3").exists():
        number += 1
    return path


class VocalError(FridayError):
    pass


class VocalSplitter:
    """Runs Demucs (``command`` is how to start it, e.g. its python with
    ``-m demucs.separate``) and keeps the part that was asked for."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        model: str = "htdemucs",
        device: str = "cpu",
        timeout_s: float = 1800.0,
    ) -> None:
        self.command = list(command)
        self.env = dict(env or {})
        self.model = model
        self.device = device
        self.timeout_s = timeout_s

    def args(self, source: Path, out_dir: Path) -> list[str]:
        return [
            *self.command,
            "-d",
            self.device,
            "-n",
            self.model,
            "--two-stems=vocals",
            "--mp3",
            "--mp3-bitrate",
            "256",
            "-o",
            str(out_dir),
            str(source),
        ]

    async def run(self, source: Path, mode: str, target: Path) -> Path:
        work = Path(tempfile.mkdtemp(prefix=".split-", dir=target.parent))
        try:
            process = await asyncio.create_subprocess_exec(
                *self.args(source, work),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.env},
            )
            try:
                _, err = await asyncio.wait_for(process.communicate(), self.timeout_s)
            except (asyncio.CancelledError, TimeoutError):
                process.kill()
                await process.wait()
                raise
            wanted = "no_vocals.mp3" if mode == REMOVE else "vocals.mp3"
            found = next(work.rglob(wanted), None)  # noqa: ASYNC240 (a few files)
            if process.returncode != 0 or found is None:
                lines = (err or b"").decode(errors="replace").strip().splitlines()
                last = [ln for ln in lines if "%|" not in ln][-1:] or ["no details"]
                raise VocalError(f"Demucs could not split the song: {last[0]}")
            shutil.move(found, target)
            return target
        finally:
            shutil.rmtree(work, ignore_errors=True)
