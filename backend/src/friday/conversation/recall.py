"""Looking through past chats when the owner asks about them.

"What did we talk about yesterday?", "do you remember the flute song?": the
app searches the saved chats (never incognito ones) and hands FRIDAY the real
lines with their dates, so she answers from them instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from friday.conversation.keeper import SMALL_WORDS, stem

MAX_HITS = 6
SNIPPET = 220

# Asking about something from before.
_ASKS = re.compile(
    r"\b(?:remember|recall|forgot|forget|talk(?:ed)?\s+about|told\s+you|tell\s+you|"
    r"did\s+(?:i|we)\b|"
    r"i\s+(?:said|asked|mentioned|showed|told)|we\s+(?:said|did|made|discussed|talked|spoke)|"
    r"you\s+(?:said|made|told|did)|last\s+(?:time|week|month|night|chat)|earlier|"
    r"the\s+other\s+day|yesterday|before|previous(?:ly)?|first\s+(?:chat|conversation|time)|"
    r"(?:\d+|one|two|three|four|five|six|seven)\s+days?\s+ago)\b",
    re.IGNORECASE,
)
# Words about remembering itself, not about the thing to find.
_ABOUT_ASKING = SMALL_WORDS | set(
    """remember recall forgot forget talk talked talking about told tell said asked
    mentioned showed discussed spoke chat chats conversation conversations last time
    week month night earlier other day yesterday today before previous previously first
    ago days one two three four five six seven ask friday something anything thing things
    again ever know knew think thought please okay ok hey hi""".split()
)


@dataclass(frozen=True, slots=True)
class Line:
    conversation_id: str
    role: str
    content: str
    created: float


def wants_recall(text: str) -> bool:
    return bool(_ASKS.search(text))


_NUMBERS = ["one", "two", "three", "four", "five", "six", "seven"]


def _window(text: str, now: datetime) -> tuple[float, float] | None:
    """The time the question is about, if it names one."""
    lower = text.lower()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in lower:
        return (day - timedelta(days=1)).timestamp(), day.timestamp()
    if re.search(r"\btoday\b|\bthis morning\b", lower):
        return day.timestamp(), now.timestamp()
    if "last week" in lower:
        return (day - timedelta(days=7)).timestamp(), now.timestamp()
    if "last month" in lower:
        return (day - timedelta(days=31)).timestamp(), now.timestamp()
    if found := re.search(r"\b(\d+|one|two|three|four|five|six|seven)\s+days?\s+ago\b", lower):
        count = found.group(1)
        days = int(count) if count.isdigit() else _NUMBERS.index(count) + 1
        start = day - timedelta(days=days)
        return start.timestamp(), (start + timedelta(days=1)).timestamp()
    return None


def _keys(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {stem(w) for w in words if w not in _ABOUT_ASKING and len(w) > 2}


def topic(question: str) -> str:
    """What the question is about, without the asking ("do you remember ...")."""
    words = re.findall(r"[A-Za-z0-9']+", question)
    return " ".join(w for w in words if w.lower() not in _ABOUT_ASKING and len(w) > 2)


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET else text[: SNIPPET - 3] + "..."


def search(
    question: str,
    lines: Sequence[Line],
    user: str,
    now: datetime | None = None,
    closeness: Sequence[float] = (),
    close: float = 1.0,
) -> str:
    """What FRIDAY is told: the matching lines from past chats, or that none matched.

    ``closeness`` (one number per line, if the meaning model is on) says how
    alike each line is to the question; lines at or above ``close`` count as
    matches even without a shared word."""
    now = now or datetime.now()
    window = _window(question, now)
    keys = _keys(question)
    scored: list[tuple[float, int, Line]] = []
    for index, line in enumerate(lines):
        if window is not None and not window[0] <= line.created < window[1]:
            continue
        score: float = len(
            keys & {stem(w) for w in re.findall(r"[a-z0-9']+", line.content.lower())}
        )
        alike = closeness[index] if index < len(closeness) else 0.0
        if alike >= close:
            score += 2 + 10 * (alike - close)  # a close meaning counts like two words or more
        if score or (window is not None and not keys):
            scored.append((score, index, line))
    if not scored:
        where = " from that time" if window else ""
        return (
            f"A search of the saved past chats{where} found no lines matching "
            f'"{question.strip()}". If your history and notes above don\'t answer it '
            "either, say plainly that you can't find it; never guess."
        )
    if keys:
        best = sorted(scored, key=lambda s: (-s[0], -s[1]))[:MAX_HITS]
    else:  # "what did we talk about yesterday?": the first lines of that time
        best = [s for s in scored if s[2].role == "user"][:MAX_HITS] or scored[:MAX_HITS]
    best.sort(key=lambda s: s[2].created)
    found = []
    for _, _, line in best:
        who = user if line.role == "user" else "you (FRIDAY)"
        when = datetime.fromtimestamp(line.created).strftime("%A %d %B %Y at %H:%M")
        found.append(f'- {when}, {who} said: "{_snippet(line.content)}"')
    return (
        "Lines found by searching the saved past chats for this question, word for "
        "word with their dates (only these; the rest of those chats isn't here). "
        "Answer from them, and if they don't answer it, say so:\n" + "\n".join(found)
    )
