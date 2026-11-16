"""Voice-only control intents recognised while FRIDAY is speaking.

* STOP: "stop", "wait", "hold on", "Friday, stop"... -> stop talking, no reply.
* BACKCHANNEL: "yeah", "mm-hmm", "okay"... -> the user is just listening;
  keep talking instead of treating it as an interruption.
"""

from __future__ import annotations

import re
from enum import Enum

_WAKE = r"(?:(?:hey|ok|okay)\s+)?friday\s+"
_STOP = re.compile(
    rf"^(?:{_WAKE})?(?:stop|wait|hold on|hang on|pause|quiet|be quiet|shush|"
    r"that s enough|enough|never ?mind|cancel|shut up|stop talking)"
    r"(?:\s+(?:please|friday|now|a (?:sec|second|moment)))*$"
)
_BACKCHANNEL = re.compile(
    r"^(?:yeah|yep|yes|yup|mm+|h+m+|m+ ?h+m+|uh huh|okay|ok|right|sure|got it|i see|cool|"
    r"nice|ah|oh|aha|true|exactly)(?:\s+(?:yeah|okay|ok|right|sure))*$"
)


# Sounds that carry no content at all. Unlike "yes" or "okay" (which can answer
# a question), these are ignored even when FRIDAY is not speaking.
_FILLER = re.compile(r"^(?:h+m+|m+|m+ ?h+m+|u+h+|u+m+|uh huh|ah+|oh+|er+|huh)$")


def is_filler(text: str) -> bool:
    return bool(_FILLER.match(_normalise(text)))


# Whisper turns a click or a cough into a tidy phrase ("Good night.",
# "All right."). Nobody says two words in a third of a second, so the length of
# the sound gives it away. Single words ("yes", "stop") are always possible.
_SHORT_MS = 600.0
_MS_PER_WORD = 250.0


def is_implausible(text: str, speech_ms: float) -> bool:
    """True when ``text`` has more words than ``speech_ms`` of sound could hold."""
    if speech_ms >= _SHORT_MS:
        return False
    words = len(_normalise(text).split())
    return words > max(1, int(speech_ms // _MS_PER_WORD))


class ControlIntent(Enum):
    STOP = "stop"
    BACKCHANNEL = "backchannel"


def _normalise(text: str) -> str:
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def classify(text: str) -> ControlIntent | None:
    norm = _normalise(text)
    if _STOP.match(norm):
        return ControlIntent.STOP
    if _BACKCHANNEL.match(norm):
        return ControlIntent.BACKCHANNEL
    return None
