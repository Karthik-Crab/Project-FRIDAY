"""After a chat goes quiet: a short summary of it, and facts worth keeping.

The local talking model reads the chat and writes:

* a summary of one or two sentences, which FRIDAY's timeline shows from then
  on ("About: changing a folk song into a flute version");
* at most three facts the owner said about themselves. These are only
  *suggested*: they wait in the app until the owner keeps or drops each one,
  and only a kept one goes into the memory file.

A fact must be built from the owner's own words, so the model can't slip in
something nobody said ("Alex is a perfectionist").
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from friday.core.messages import ChatRequest, Message, Role

log = logging.getLogger(__name__)

MAX_FACTS = 3
MAX_CHAT_CHARS = 8000  # what the model reads of a long chat: the newest part
_SOUND = re.compile(r"\[(?:laugh|chuckle|sigh|gasp)\]", re.IGNORECASE)

PROMPT = """\
You read a chat between {user} and FRIDAY, {user}'s AI companion, and answer
with JSON only, no other text:
{{"summary": "...", "facts": ["...", "..."]}}

summary: one or two plain sentences on what the chat was about and what was
done, written about {user} ("{user} asked for ..."). No opinions, nothing that
isn't in the chat.

facts: up to three lasting things {user} said about themselves, worth
remembering in later chats: their life, work, people, plans, likes and
dislikes. Write each one short, in {user}'s own words, starting with "{user}".
Only what {user} clearly said, never a guess about their personality, never
something FRIDAY said, never a one-off request, and never anything about
FRIDAY herself. Most chats have none: then [].
Good facts: "{user} works as a nurse", "{user} has a sister called
Anna", "{user} likes flute music", "{user} needs to give his car for service".
Not facts, never write these: "{user} asked for a picture of Kratos", "{user}
wants bass added to the song", "{user} asked what 2 plus 2 is".
"""

# What the model still writes now and then that is a request, not a fact about
# the owner: "Alex asked ...", "... the song", "... FRIDAY ...".
_NOT_A_FACT = re.compile(
    r"^\S+\s+(?:asked|requested|questioned|tested|described|told|said|wanted\s+the|"
    r"is\s+asking|was\s+asking|can|could|was\s+just|is\s+just|"
    r"(?:wants?|wanted)\s+(?:a|an|to\s+(?:add|make|remove|change|create|generate|try|see)))\b"
    r"|\b(?:this|the|that|a)\s+(?:song|picture|image|photo|file|answer|question|version)\b"
    r"|\b(?:friday|claude)\b",
    re.IGNORECASE,
)

# Words that carry no meaning of their own, for checking a fact against what
# the owner really said.
SMALL_WORDS = set(
    """a an the and or but so to of in on at for with from by about as is are was were be
    been being am i me my mine you your he she it its they them their we us our this that
    these those there here what which who whom when where why how do does did done have has
    had having will would can could should may might must not no yes very really just also
    too than then like likes liked want wants wanted love loves loved enjoy enjoys enjoyed
    get gets got make makes made go goes going s t m d ll re ve said says say tell told""".split()
)


@dataclass(frozen=True, slots=True)
class Kept:
    summary: str
    facts: tuple[str, ...]


def stem(word: str) -> str:
    for end in ("ing", "ed", "es", "s"):
        if len(word) > len(end) + 2 and word.endswith(end):
            return word[: -len(end)]
    return word


def _words(text: str) -> set[str]:
    return {stem(w) for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in SMALL_WORDS}


def grounded(fact: str, said: str, user: str) -> bool:
    """Does the fact come from what the owner said? Most of its words must."""
    words = _words(fact) - {user.lower()}
    if not words:
        return False
    found = _words(said)
    return len(words & found) / len(words) >= 0.6


def chat_text(messages: Sequence[tuple[str, str]], user: str) -> str:
    """The chat as plain lines, the newest part if it is long."""
    lines = [
        f"{user if role == 'user' else 'FRIDAY'}: {_SOUND.sub('', text).strip()}"
        for role, text in messages
        if text.strip()
    ]
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHAT_CHARS else "...\n" + text[-MAX_CHAT_CHARS:]


def _json(reply: str) -> dict[str, object]:
    reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL)
    start, end = reply.find("{"), reply.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON in the reply")
    data = json.loads(reply[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


_STAMP = re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}\]\s*")


def _new(fact: str, known: Sequence[str]) -> bool:
    words = _words(fact)
    for old in known:
        theirs = _words(_STAMP.sub("", old))
        if words and theirs and len(words & theirs) / len(words | theirs) >= 0.6:
            return False
    return True


async def keep(
    messages: Sequence[tuple[str, str]],
    ask: Callable[[ChatRequest], Awaitable[str]],
    user: str,
    known: Sequence[str] = (),
) -> Kept:
    """The summary and the suggested facts for one chat.

    ``messages`` are (role, text) pairs; ``ask`` runs the talking model;
    ``known`` are notes and suggestions already there (not suggested again).
    """
    request = ChatRequest(
        system=PROMPT.format(user=user),
        messages=(Message(Role.USER, chat_text(messages, user)),),
        max_tokens=400,
        temperature=0.2,
        think=False,
    )
    data = _json(await ask(request))
    summary = " ".join(str(data.get("summary") or "").split())[:400]
    said = "\n".join(text for role, text in messages if role == "user")
    facts: list[str] = []
    raw = data.get("facts")
    for item in raw if isinstance(raw, list) else []:
        fact = " ".join(str(item).split()).strip(" -")[:200]
        if not fact or _NOT_A_FACT.search(fact) or not grounded(fact, said, user):
            log.info("left out a suggested fact nobody said: %r", fact)
            continue
        if _new(fact, [*known, *facts]):
            facts.append(fact)
        if len(facts) == MAX_FACTS:
            break
    return Kept(summary, tuple(facts))
