"""Make model text safe to speak.

The persona asks for plain spoken text, but models (especially small local
ones) do not always comply. This is the provider-independent safety net that
keeps FRIDAY from reading out asterisks, hashes and URLs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_EMPHASIS = re.compile(r"(\*{1,3}|_{2,3})(\S.*?\S|\S)\1")
# Stage directions such as *laughs* or (sighs): a voice would read them out.
_STAGE = re.compile(r"(?:\*[^*\n]{0,40}\*|\([^)\n]{0,40}\)|\[[^\]\n]{0,40}\])", re.IGNORECASE)
_STAGE_WORDS = re.compile(
    r"^\W*(?:laugh|chuckl|giggl|sigh|smil|grin|pause|gasp|wink|clears? (?:her |his |my )?throat"
    r"|nod|shrug|whisper)",
    re.IGNORECASE,
)
# Which sound a stage direction stands for, when the voice can make sounds.
_TAG_FOR = (
    (re.compile(r"^\W*(?:chuckl|giggl)", re.IGNORECASE), "chuckle"),
    (re.compile(r"^\W*laugh", re.IGNORECASE), "laugh"),
    (re.compile(r"^\W*sigh", re.IGNORECASE), "sigh"),
    (re.compile(r"^\W*gasp", re.IGNORECASE), "gasp"),
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff️‍]")
_SPACES = re.compile(r"[ \t]{2,}")


class SoundBudget:
    """How often FRIDAY may laugh, chuckle or sigh out loud.

    Small local models are asked to use these sounds rarely but put them in
    almost every reply, which sounds odd fast. This keeps it to one sound in a
    reply, in at most one reply out of ``every``; extra marks are dropped.
    """

    def __init__(self, every: int = 4) -> None:
        self.every = every
        self._reply = 0
        self._last: int | None = None  # the reply that last had a sound

    def new_reply(self) -> None:
        self._reply += 1

    def allow(self) -> bool:
        if self._last is not None and self._reply - self._last < self.every:
            return False
        self._last = self._reply
        return True


class SpeechSanitizer:
    """Stateful because fenced code blocks can span several chunks."""

    def __init__(
        self, expression_tags: Iterable[str] = (), sounds: SoundBudget | None = None
    ) -> None:
        # Sounds the voice can make itself, written as [laugh]. Empty for a
        # voice that can only read words: every stage direction is then removed.
        self._tags = frozenset(expression_tags)
        self._sounds = sounds
        self._in_code = False
        self._announced_code = False

    def clean(self, text: str) -> str:
        out: list[str] = []
        segments = text.split("```")
        for i, segment in enumerate(segments):
            if i > 0:
                self._in_code = not self._in_code
                if self._in_code and not self._announced_code:
                    out.append(" I've put the code on screen. ")
                    self._announced_code = True
            if not self._in_code:
                out.append(segment)
        cleaned = "".join(out)
        cleaned = _LINK.sub(r"\1", cleaned)
        cleaned = _URL.sub("the link", cleaned)
        cleaned = _INLINE_CODE.sub(r"\1", cleaned)
        cleaned = _STAGE.sub(self._stage, cleaned)
        cleaned = _EMPHASIS.sub(r"\2", cleaned)
        cleaned = _HEADING.sub("", cleaned)
        cleaned = _BULLET.sub("", cleaned)
        cleaned = _EMOJI.sub("", cleaned)
        cleaned = cleaned.replace("\n", " ")
        return _SPACES.sub(" ", cleaned).strip()

    def _stage(self, match: re.Match[str]) -> str:
        inner = match.group()[1:-1]
        if not _STAGE_WORDS.match(inner):
            return match.group()
        for pattern, tag in _TAG_FOR:
            if tag in self._tags and pattern.match(inner):
                if self._sounds is not None and not self._sounds.allow():
                    return ""
                return f" [{tag}] "
        return ""


def strip_expression_tags(text: str, tags: Iterable[str]) -> str:
    """Remove [laugh]-style marks, for a voice that cannot make the sounds."""
    for tag in tags:
        text = text.replace(f"[{tag}]", " ")
    return _SPACES.sub(" ", text).strip()
