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
