"""Changing a song the owner attached, with ffmpeg: more or less bass, less noise.

No AI is involved. The bass is the knob on a stereo: the song stays the
same, only its low notes get louder (or softer), and a limiter stops the
louder bass from distorting. Cleaning takes out clicks and crackle, then
the steady hiss of old recordings (ffmpeg's adeclick and afftdn).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from friday.core.errors import FridayError

# Low shelf at 100 Hz: kick drum and bass guitar, below most voices.
# Measured on the PC, a shelf at 100 Hz only lifted notes below 120 Hz, which
# laptop and phone speakers barely play: the owner heard no difference. So
# the shelf sits at 150 Hz, and more bass also gets a "punch" bump at 200 Hz.
BASS_FREQUENCY_HZ = 150
PUNCH_HZ = 200
PUNCH_SHARE = 0.6  # the punch bump is this much of the bass gain


# How much hiss is taken out (dB): normal, and when asked for "a lot".
CLEAN_DB = 12
CLEAN_MORE_DB = 20


@dataclass(frozen=True, slots=True)
class AudioEdit:
    gain_db: float  # positive: more bass, 0: bass unchanged
    clean_db: float = 0  # noise taken out, 0: none

    @property
    def describe(self) -> str:
        parts = []
        if self.clean_db:
            parts.append("less noise" + (" (strong)" if self.clean_db > CLEAN_DB else ""))
        if self.gain_db:
            amount = abs(self.gain_db)
            word = "more" if self.gain_db > 0 else "less"
            parts.append(f"{word} bass ({'+' if self.gain_db > 0 else '-'}{amount:g} dB)")
        return ", ".join(parts)


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
    r"\b(?:add|more|boost|increase|louder|stronger|heavy|heavier|deeper|pump|raise|turn\s+up|"
    r"bump|punch|thump|fatter|extra|enhance)\w*",
    re.I,
)
_LITTLE = re.compile(r"\b(?:a\s+(?:little|bit|touch)|slightly|bit\s+of|subtle|some)\b", re.I)
_LOT = re.compile(
    r"\b(?:a\s+lot|lots|(?<!too\s)much|heavy|heavily|massive|huge|max(?:imum)?|really|very|tons|super)\b",
    re.I,
)
_NOISE_WORD = (
    r"(?:noises?|noisy|hiss\w*|crackl\w*|static|hum+(?:ing)?|clicks?|clicking|pops|popping|"
    r"disturbances?|distortions?|background\s+sounds?)"
)
_NOISE = re.compile(
    rf"\b(?:{_NOISE_WORD}|de-?noise|clean(?:er)?(?:\s+up)?|clear(?:er)?(?:\s+up)?|"
    r"restore|old\s+recording)\b",
    re.I,
)
# "remove all the noises", "clear the noisy sound": about the noise, not the bass.
_NOISE_PHRASE = re.compile(
    r"\b(?:remove|reduce|cut|lower|less|clear|clean|get\s+rid\s+of|take\s+out|fix|"
    r"eliminate|kill|filter)\b"
    rf"(?:\s+\S+){{0,4}}?\s+{_NOISE_WORD}(?:\s+sounds?)?\b",
    re.I,
)
_NOT_ENOUGH = re.compile(r"\b(?:not|still|yet|again|isn'?t|didn'?t)\b", re.I)
_DB = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(?:db|decibels?)\b", re.I)


def parse_audio_edit(text: str) -> AudioEdit | None:
    """What to do to an attached song, or None when the words don't say."""
    clean = 0.0
    if _NOISE.search(text):
        # "noise is not removed yet", "still noisy": the last try wasn't enough.
        stronger = _LOT.search(text) or _NOT_ENOUGH.search(text)
        clean = CLEAN_MORE_DB if stronger else CLEAN_DB
    gain = _bass_gain(_NOISE_PHRASE.sub(" ", text))
    if gain is None and not clean:
        return None
    return AudioEdit(gain or 0.0, clean)


def _bass_gain(text: str) -> float | None:
    if not _BASS.search(text) or _NEW_PART.search(text):
        return None
    less_at = [m.start() for m in _LESS.finditer(text)]
    more_at = [m.start() for m in _MORE.finditer(text)]
    if not less_at and not more_at:
        return None
    less = bool(less_at)
    if less_at and more_at:
        # "remove the hiss and boost the bass": the action word just before
        # "bass" says what to do with it (else the nearest one after it).
        bass = _BASS.search(text)
        assert bass is not None
        before = [(at, True) for at in less_at if at < bass.start()]
        before += [(at, False) for at in more_at if at < bass.start()]
        if before:
            less = max(before)[1]
        else:
            less = min(less_at) < min(more_at)
    if match := _DB.search(text):
        gain = min(abs(float(match.group(1))), 15.0)
    elif _LOT.search(text):
        gain = 10.0
    elif _LITTLE.search(text) and not re.search(r"\bsome\b", text, re.I):
        gain = 3.0
    else:
        gain = 6.0
    return -gain if less else gain


def edit_note(edit: AudioEdit) -> str:
    """Plain words for the card: what was done, and why it only takes seconds."""
    done = []
    if edit.clean_db:
        strong = " (strong cleaning)" if edit.clean_db > CLEAN_DB else ""
        done.append(f"took out clicks and the steady hiss{strong}")
    if edit.gain_db > 0:
        done.append("made the bass louder")
    elif edit.gain_db < 0:
        done.append("made the bass softer")
    return (
        "I " + " and ".join(done) + ". This is a sound filter, not AI, so it takes "
        "seconds. Play the original and this one one after the other to hear it; the "
        "difference is clearest on headphones. Ask again for a stronger change."
    )


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
    tags = (["clean"] if edit.clean_db else []) + (
        ["bass" if edit.gain_db > 0 else "less-bass"] if edit.gain_db else []
    )
    tag = "+".join(tags)
    folder.mkdir(parents=True, exist_ok=True)
    number = 1
    while (path := folder / f"{stem} ({tag} {number}){ext}").exists():
        number += 1
    return path


def ffmpeg_args(ffmpeg: str, source: Path, target: Path, edit: AudioEdit) -> list[str]:
    steps = []
    if edit.clean_db:
        # Clicks first, then hiss; tn follows hiss that changes over the song.
        # Measured on a 1960s film song: hiss (6-10 kHz) -10 dB normal, -18 dB strong,
        # while the music itself moved 1.5-3 dB.
        floor = -25 if edit.clean_db > CLEAN_DB else -40
        steps += ["adeclick", f"afftdn=nr={edit.clean_db:g}:nf={floor}:tn=1"]
    if edit.gain_db:
        steps.append(f"bass=g={edit.gain_db:g}:f={BASS_FREQUENCY_HZ}:w=0.8")
    if edit.gain_db > 0:
        punch = round(edit.gain_db * PUNCH_SHARE, 1)
        steps.append(f"equalizer=f={PUNCH_HZ}:t=o:w=1.2:g={punch:g}")
        steps.append("alimiter=limit=0.95:level=disabled")
    filters = ",".join(steps)
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
