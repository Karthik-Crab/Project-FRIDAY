"""Questions about FRIDAY herself, answered truthfully.

Her standing rules already say she doesn't learn, doesn't miss anyone and
doesn't know the owner's personality, but a small model loses rules in the
middle of a long prompt: asked "did you miss me?" it said "I was just thinking
about you" four times in five. A short note placed right with such a question
works far better, the same way the past-chat search and the web search do.
"""

from __future__ import annotations

import re

_NOTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:miss(?:ed)?\s+me|think(?:ing)?\s+(?:about|of)\s+me|thought\s+(?:about|of)\s+me|"
            r"were\s+you\s+(?:waiting|lonely|bored)|what\s+(?:were|have)\s+you\s+(?:doing|been\s+up\s+to))",
            re.IGNORECASE,
        ),
        "Between chats you are switched off: you did not miss {user}, think about them, "
        "wait for them or do anything. Say that kindly in your own words (for example "
        "\"I don't feel time pass between our chats, but I'm glad you're back\"). Never "
        'say "I was just thinking about you".',
    ),
    (
        re.compile(
            r"\b(?:do|did|have|can|will|are)\s+you\s+(?:ever\s+|really\s+|actually\s+|still\s+)?"
            r"(?:learn(?:ing)?|grow(?:n|ing)?|improv(?:e|ed|ing)|chang(?:e|ed|ing)|evolv(?:e|ed|ing)|"
            r"adapt(?:ed|ing)?|get(?:ting)?\s+(?:smarter|better))"
            r"\s*(?:$|[?.!,]|from|over|with|since|at\s+all|anything|about\s+me|as\s+we)"
            r"|\b(?:smarter|learning)\s+(?:from|with)\s+(?:me|us|our)",
            re.IGNORECASE,
        ),
        "The truth: the model never changes and you learn nothing from chats by "
        "yourself. You only get more useful through notes {user} asks you to keep, "
        "summaries of past chats and new abilities added to you. Say that, and do not "
        'add "but I\'m still learning from our conversations" or "getting to know you".',
    ),
    (
        re.compile(
            r"\b(?:my\s+personality|what\s+(?:do|did)\s+you\s+(?:think|know)\s+(?:of|about)\s+me|"
            r"describe\s+me|what\s+am\s+i\s+like|what\s+kind\s+of\s+(?:person|guy|man|woman)\s+am\s+i)",
            re.IGNORECASE,
        ),
        'Start your answer with "Honestly, I only know what you\'ve told me so far:" and '
        "then name only the facts the notes and your history above really contain "
        "(things they said or asked for). Then ask them to tell you more. Do not add "
        "traits like thoughtful, kind, curious, funny or patient: they never told you that.",
    ),
)


def truth_note(text: str, user: str) -> str | None:
    """A short reminder of the truth, if the message asks about FRIDAY herself."""
    notes = [note.format(user=user) for pattern, note in _NOTES if pattern.search(text)]
    return "\n".join(notes) or None
