"""Make model text safe to speak.

The persona asks for plain spoken text, but models (especially small local
ones) do not always comply. This is the provider-independent safety net that
keeps FRIDAY from reading out asterisks, hashes and URLs.
"""

from __future__ import annotations

import re

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
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+", re.MULTILINE)
_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff️‍]")
_SPACES = re.compile(r"[ \t]{2,}")


class SpeechSanitizer:
    """Stateful because fenced code blocks can span several chunks."""

    def __init__(self) -> None:
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
        cleaned = _STAGE.sub(
            lambda m: "" if _STAGE_WORDS.match(m.group()[1:-1]) else m.group(), cleaned
        )
        cleaned = _EMPHASIS.sub(r"\2", cleaned)
        cleaned = _HEADING.sub("", cleaned)
        cleaned = _BULLET.sub("", cleaned)
        cleaned = _EMOJI.sub("", cleaned)
        cleaned = cleaned.replace("\n", " ")
        return _SPACES.sub(" ", cleaned).strip()
