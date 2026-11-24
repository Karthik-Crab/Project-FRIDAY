"""FRIDAY's sense of time: what really happened, and when.

Built from the saved chats (never the incognito ones) each time she answers,
so "how long have you known me?" or "what did we talk about yesterday?" is
answered from the record instead of made up.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

RECENT = 8  # earlier chats listed one by one
OPENING = 90  # characters of each opening line


@dataclass(frozen=True, slots=True)
class ChatSummary:
    id: str
    created: float
    updated: float
    messages: int
    # The first things the owner said in it, oldest first.
    opening: Sequence[str] = field(default_factory=tuple)


def _ago(day: date, today: date) -> str:
    days = (today - day).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def _when(ts: float, today: date) -> str:
    moment = datetime.fromtimestamp(ts)
    ago = _ago(moment.date(), today)
    if ago in ("today", "yesterday"):
        return f"{ago} at {moment:%H:%M}"
    return f"{moment:%A %d %B %Y} at {moment:%H:%M} ({ago})"


def _line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= OPENING else text[: OPENING - 3] + "..."


def timeline(
    chats: Sequence[ChatSummary],
    current_id: str | None,
    user_name: str,
    now: datetime | None = None,
) -> str:
    """What FRIDAY is told about her history with the owner."""
    now = now or datetime.now()
    today = now.date()
    chats = sorted((c for c in chats if c.messages), key=lambda c: c.created)
    earlier = [c for c in chats if c.id != current_id]
    current = next((c for c in chats if c.id == current_id), None)
    lines = [
        f"Your real history with {user_name}, from the chats saved on this computer "
        "(incognito chats are left out). It is all you know about earlier "
        "conversations besides the notes. Of each earlier chat you see only when it "
        "was and how it began, not the rest, so never say you remember every "
        "conversation, and never guess dates, how long you have known them, or what "
        "else was said.",
    ]
    if not earlier:
        lines.append(f"- This is the first conversation you have had with {user_name}.")
    else:
        first = chats[0]
        days = len({datetime.fromtimestamp(c.created).date() for c in chats})
        lines += [
            f"- You first talked {_when(first.created, today)}.",
            f"- {len(chats)} saved chats so far, on {days} different "
            f"day{'s' if days != 1 else ''}.",
            f"- The last chat before this one: {_when(earlier[-1].updated, today)}.",
        ]
    if current is not None:
        lines.append(f"- This conversation started {_when(current.created, today)}.")
    if earlier:
        shown = earlier[-RECENT:]
        if earlier[0] not in shown:
            shown = [earlier[0], *shown]
        lines.append("Earlier chats, oldest first, with how each began:")
        for chat in shown:
            said = " / ".join(f'"{_line(t)}"' for t in chat.opening) or "(no words)"
            lines.append(f"- {_when(chat.created, today)}, {chat.messages} messages: {said}")
        if len(earlier) > len(shown):
            lines.append(f"({len(earlier) - len(shown)} more chats in between are not listed.)")
    return "\n".join(lines)
