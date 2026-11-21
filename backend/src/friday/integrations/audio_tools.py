"""Changing a song the owner attached: more or less bass, with ffmpeg.

No AI is involved. It is the bass knob on a stereo: the song stays the
same, only its low notes get louder (or softer). A limiter stops the louder
bass from distorting.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from friday.core.errors import FridayError

# Low shelf at 100 Hz: kick drum and bass guitar, below most voices.
BASS_FREQUENCY_HZ = 100


@dataclass(frozen=True, slots=True)
class AudioEdit:
    gain_db: float  # positive: more bass

    @property
    def describe(self) -> str:
        amount = abs(self.gain_db)
        word = "more" if self.gain_db > 0 else "less"
        return f"{word} bass ({'+' if self.gain_db > 0 else '-'}{amount:g} dB)"


_BASS = re.compile(r"\bbass\b", re.I)
# A new bass part played on top: not this (see ROADMAP, "add an instrument").
_NEW_PART = re.compile(r"\bbass\s*(?:line|part|track|guitar|riff|solo|melody)s?\b", re.I)
_INSTRUMENT = re.compile(
    r"\b(?:add|play|put)\s+(?:in\s+)?(?:a|an|some)?\s*(?:drums?|guitars?|piano|violins?|"
    r"strings|beat|instruments?|synth|flute|saxophone|sax)\b",
    re.I,
)
_LESS = re.compile(
    r"\b(?:less|lower|reduce|cut|remove|softer|quieter|decrease|turn\s+down|tone\s+down|too\s+much)\b",
    re.I,
)
_MORE = re.compile(
    r"\b(?:add|more|boost|increase|louder|stronger|heavier|deeper|pump|raise|turn\s+up|"
    r"bump|punch|thump|fatter|extra|enhance)\w*",
    re.I,
)
_LITTLE = re.compile(r"\b(?:a\s+(?:little|bit|touch)|slightly|bit\s+of|subtle|some)\b", re.I)
_LOT = re.compile(
    r"\b(?:a\s+lot|lots|(?<!too\s)much|heavy|heavily|massive|huge|max(?:imum)?|really|very|tons|super)\b",
    re.I,
)
_DB = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:db|decibels?)\b", re.I)


def parse_audio_edit(text: str) -> AudioEdit | None:
    """What to do to an attached song, or None when the words don't say."""
    if not _BASS.search(text) or _NEW_PART.search(text):
        return None
    less = bool(_LESS.search(text))
    if not less and not _MORE.search(text):
        return None
    if match := _DB.search(text):
        gain = min(abs(float(match.group(1))), 15.0)
    elif _LOT.search(text):
        gain = 10.0
    elif _LITTLE.search(text) and not re.search(r"\bsome\b", text, re.I):
        gain = 3.0
    else:
        gain = 6.0
    return AudioEdit(-gain if less else gain)


def wants_new_part(text: str) -> bool:
    """The owner asks for an instrument to be played on top (not yet possible)."""
    return bool(_NEW_PART.search(text) or _INSTRUMENT.search(text))


class AudioToolError(FridayError):
    pass


def find_ffmpeg(configured: str = "") -> str | None:
    if configured:
        return configured if Path(configured).is_file() or shutil.which(configured) else None
    return shutil.which("ffmpeg")


def output_name(source: Path, edit: AudioEdit, folder: Path) -> Path:
    ext = source.suffix.lower() if source.suffix.lower() in {".mp3", ".wav", ".flac"} else ".mp3"
    stem = re.sub(r"[^\w\- ]+", "", source.stem).strip() or "song"
    tag = "bass" if edit.gain_db > 0 else "less-bass"
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (path := folder / f"{stem} ({tag} {number}){ext}").exists():
        number += 1
    return path


def ffmpeg_args(ffmpeg: str, source: Path, target: Path, edit: AudioEdit) -> list[str]:
    filters = f"bass=g={edit.gain_db:g}:f={BASS_FREQUENCY_HZ}:w=0.6"
    if edit.gain_db > 0:
        filters += ",alimiter=limit=0.95:level=disabled"
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(source), "-vn", "-af", filters]
    if target.suffix == ".mp3":
        args += ["-c:a", "libmp3lame", "-q:a", "0"]
    args.append(str(target))
    return args


async def apply_edit(ffmpeg: str, source: Path, target: Path, edit: AudioEdit) -> Path:
    """Write ``source`` with ``edit`` applied to ``target``."""
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_args(ffmpeg, source, target, edit),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await process.communicate()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        target.unlink(missing_ok=True)  # noqa: ASYNC240 (instant)
        raise
    if process.returncode != 0 or not target.is_file():  # noqa: ASYNC240 (instant)
        target.unlink(missing_ok=True)  # noqa: ASYNC240
        last = (err or b"").decode(errors="replace").strip().splitlines()[-1:] or ["no details"]
        raise AudioToolError(f"ffmpeg could not change the song: {last[0]}")
    return target
