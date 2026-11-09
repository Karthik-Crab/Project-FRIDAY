"""Deterministic control commands ("Friday, work mode").

These are recognised *before* the language model is involved so they are
instant, free, work offline, and behave identically on every provider.
Matching is deliberately strict (the whole utterance must be the command) so
"I hate work mode" does not switch modes. It is tolerant of the punctuation and
filler words speech recognisers commonly produce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from friday.persona.persona import Mode

_WAKE = r"(?:(?:hey|ok|okay)\s+)?(?:friday|fri day)"
_VERB = r"(?:(?:switch|change|go|swap)\s+(?:to|into|back\s+to)\s+|enter\s+|back\s+to\s+)?"
_MODE = r"(?P<mode>work|companion|chat|casual)"
_TAIL = r"(?:\s+mode)(?:\s+(?:please|now|on))?"

_PATTERN = re.compile(rf"^(?:{_WAKE}\s+)?{_VERB}{_MODE}{_TAIL}$")
_ALIASES = {
    "work": Mode.WORK,
    "companion": Mode.COMPANION,
    "chat": Mode.COMPANION,
    "casual": Mode.COMPANION,
}


@dataclass(frozen=True, slots=True)
class ModeCommand:
    mode: Mode


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_command(text: str) -> ModeCommand | None:
    match = _PATTERN.match(_normalise(text))
    if not match:
        return None
    return ModeCommand(mode=_ALIASES[match.group("mode")])
