"""Questions worth thinking about before answering.

The small talking model gets sums, times and dates wrong about one time in
three when it answers straight away, and nearly always right when it first
works them out (a few seconds longer). Everyday talk stays instant: only a
message that asks for some working out turns thinking on.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(
    r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"fifteen|twenty|thirty|forty|fifty|hundred|thousand|lakh|crore|half|quarter|dozen)\b",
    re.IGNORECASE,
)
_WORKING_OUT = re.compile(
    r"\b(?:plus|minus|times|multipl\w*|divid\w*|total|sum|cost\w*|change|sav\w*|spend\w*|"
    r"percent\w*|discount|interest|profit|loss|average|split|each|per|left|remain\w*|"
    r"how\s+(?:much|many|long|far|old|late|early)|what\s+time|when\s+(?:should|do|will|can|must)|"
    r"will\s+i\s+(?:make|reach|get|be)|in\s+time|late|early|before|after|until|"
    r"hours?|minutes?|mins?|days?|weeks?|months?|years?|km|kilomet\w*|kg|litres?|liters?|"
    r"bigger|smaller|larger|greater|more\s+than|less\s+than|fewer|older|younger|"
    r"faster|slower|cheaper)\b|%|[-+*/\u00d7\u00f7=]\s*\d|\d\s*x\s*\d",
    re.IGNORECASE,
)
_DATE_QUESTION = re.compile(
    r"\b(?:what|which)\s+day\b|\bday\s+of\s+the\s+week\b|\bhow\s+many\s+days\b",
    re.IGNORECASE,
)


def needs_working_out(text: str) -> bool:
    """Does the message ask for a sum, a time or a date to be worked out?"""
    if _DATE_QUESTION.search(text):
        return True
    return bool(_NUMBER.search(text) and _WORKING_OUT.search(text))
