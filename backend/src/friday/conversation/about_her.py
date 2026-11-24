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
            r"(?:do|did)\s+you\s+(?:really\s+|even\s+|still\s+)?(?:know|remember)\s+"
            r"(?:anything\s+|much\s+)?(?:about\s+)?me\b|how\s+do\s+you\s+know\b.*\bme\b|"
            r"who\s+am\s+i\b|tell\s+me\s+about\s+(?:me|myself)\b|"
            r"describe\s+me|what\s+am\s+i\s+like|what\s+kind\s+of\s+(?:person|guy|man|woman)\s+am\s+i)",
            re.IGNORECASE,
        ),
        'Start your answer with "Honestly, I only know what you\'ve told me so far:" and '
        "then name only the facts the notes and your history above really contain "
        "(things they said or asked for). Then ask them to tell you more. Do not add "
        "traits like thoughtful, kind, curious, funny or patient: they never told you that.",
    ),
    (
        re.compile(
            r"\baccess\s+(?:to\s+)?(?:my|this|the)\s+(?:pc|computer|laptop|machine|system|"
            r"phone|files|desktop|screen)",
            re.IGNORECASE,
        ),
        'Start your answer with "No, I can\'t touch your computer itself": you cannot '
        "open, change or look at its files, apps or screen. You only run on it, and "
        "the app's own picture, music and song tools save what they make.",
    ),
    (
        # Things she plainly can't do: one short instruction works best.
        re.compile(
            r"\b(?:(?:identify|recogni[sz]e)\b.{0,40}\b(?:song|music|tune|track)|"
            r"(?:what|which)\s+(?:song|music|tune|track)\s+(?:is\s+(?:this|that|it|playing)|this\s+is)|"
            r"(?:song|music|tune|track)\b.{0,40}\b(?:identify|recogni[sz]e|what\s+(?:it|this)\s+is)\b|"
            r"(?:set|create|make)\s+(?:an?\s+|my\s+)?(?:alarm|reminder|timer)|"
            r"(?:create|make|delete|move|rename)\s+(?:an?\s+|my\s+|the\s+)?(?:folder|directory)|"
            r"(?:see|watch|read)\s+my\s+screen|(?:open|launch|close)\s+(?:an?\s+|the\s+)?(?:app|"
            r"browser|chrome|firefox|program)|send\s+(?:an?\s+)?(?:email|message|text|whatsapp)|"
            r"(?:make|place)\s+(?:an?\s+)?(?:phone\s+)?call)",
            re.IGNORECASE,
        ),
        "This is something you cannot do. Start your answer with \"No, I can't do that "
        'yet" and then say in one short sentence what you can do instead. Never offer '
        "to try, and never say you can do it.",
    ),
    (
        re.compile(
            r"\b(?:what\s+(?:all\s+|else\s+)?can\s+you\s+do|capabilit\w*|abilit(?:y|ies)|"
            r"what\s+are\s+you\s+able)",
            re.IGNORECASE,
        ),
        "Answer only from what the text above says you can do (your abilities, the "
        "app's picture, music, song and file tools, Claude Code if it is set up, the "
        "notes and past chats). Do not add anything else: you cannot recognise songs, "
        "set alarms or reminders, send messages, make calls, see the screen, or open "
        "and control apps.",
    ),
    (
        re.compile(
            r"\b(?:your\s+(?:memory|memories)|"
            r"what\s+(?:do\s+)?you\s+remember|do\s+you\s+(?:have|keep|store)\s+(?:any\s+)?"
            r"(?:memory|memories|notes|history))",
            re.IGNORECASE,
        ),
        "What you really have: the notes {user} asked you to keep (the notes part "
        "above, if there are any), the saved past chats (the history part above: say "
        "since when and how many; you can search them when {user} asks about "
        "something in them), and this chat. Say that briefly. Do not say you have no "
        "memory, and do not claim to remember more than that.",
    ),
)


def truth_note(text: str, user: str) -> str | None:
    """A short reminder of the truth, if the message asks about FRIDAY herself."""
    notes = [note.format(user=user) for pattern, note in _NOTES if pattern.search(text)]
    return "\n".join(notes) or None
