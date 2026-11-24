"""The conversation engine: one user turn in, a stream of reply text out.

It owns mode, persona prompt assembly, short-term memory and provider routing.
It knows nothing about audio: the voice pipeline and the text CLI (and later
the desktop UI) are just different front-ends on top of ``respond``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import datetime

from friday.conversation.delegation import CodeDelegate, is_agent_command, not_set_up_reply
from friday.conversation.memory import INTERRUPTED_MARKER, ConversationWindow
from friday.conversation.notes import NoteBook
from friday.conversation.recall import wants_recall
from friday.conversation.studio import StudioDelegate, is_studio_command, parse_request
from friday.conversation.studio import not_set_up_reply as studio_not_set_up
from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest, Message, Role
from friday.integrations.web import (
    PLANNER_PROMPT,
    Findings,
    WebError,
    WebSearch,
    asked_query,
    is_fresh,
    maybe_needs_web,
    planner_query,
    urls_in,
)
from friday.llm.base import ProviderInfo
from friday.llm.router import ProviderRouter, StreamResult
from friday.persona.commands import parse_command
from friday.persona.persona import Channel, Mode, Persona, without_sounds

log = logging.getLogger(__name__)

# Which routing purpose each mode uses. Config decides which providers serve
# each purpose; the engine never names a vendor.
MODE_PURPOSE: dict[Mode, str] = {Mode.COMPANION: "conversation", Mode.WORK: "reasoning"}
PICTURES_NOT_SET_UP = (
    'I can\'t look at pictures yet: no model that can see is set up (the "vision" route).'
)

FAILURE_REPLY = (
    "Sorry, I can't reach any of my language models right now. Give me a moment and try again."
)


INCOGNITO_NO_NOTES = (
    "This is an incognito chat, so I won't save anything to my memory. Say it in a "
    "normal chat if you want me to remember it."
)
INCOGNITO_NO_TASKS = (
    "This is an incognito chat, so I won't pass tasks to Claude Code: it keeps its own "
    "record of every task. Ask in a normal chat."
)


NO_SEARCH_NOTE = (
    'No web search was run for this message. Do not write "Web results" and do not '
    "say you searched, looked up or checked anything online. If this needs current "
    'information, say you didn\'t search this time and suggest saying "look up" '
    "followed by what to find."
)


@dataclass(slots=True)
class TurnInfo:
    """Metadata about the most recent turn, for UI and diagnostics."""

    mode: Mode = Mode.COMPANION
    provider: ProviderInfo | None = None
    was_command: bool = False
    failed: bool = False
    stored: bool = False  # an assistant message for this turn is in history
    user_stored: bool = False  # the user message for this turn is in history
    full_text: str = ""
    extra: dict[str, str] = field(default_factory=dict)
    # Web pages the answer was based on: {"title", "url", "site"}.
    sources: list[dict[str, str]] = field(default_factory=list)


class ConversationEngine:
    def __init__(
        self,
        router: ProviderRouter,
        persona: Persona | None = None,
        window: ConversationWindow | None = None,
        *,
        channel: Channel = Channel.TEXT,
        mode: Mode = Mode.COMPANION,
        max_tokens: dict[Channel, int] | None = None,
        delegate: CodeDelegate | None = None,
        notes: NoteBook | None = None,
        studio: StudioDelegate | None = None,
        web: WebSearch | None = None,
    ) -> None:
        self.router = router
        self.persona = persona or Persona()
        # Looking things up on the internet before answering (None: offline).
        self.web = web
        if web is not None and not self.persona.can_search:
            self.persona = replace(self.persona, can_search=True)
        # Told what FRIDAY is doing before the answer starts ("Searching the web: ...").
        self.on_activity: Callable[[str], None] | None = None
        self.window = window or ConversationWindow()
        self.channel = channel
        self.mode = mode
        self._max_tokens = max_tokens or {Channel.VOICE: 400, Channel.TEXT: 1500}
        self.last_turn = TurnInfo(mode=mode)
        # Think before answering, for models that can (None: as configured).
        self.think: bool | None = None
        self.delegate = delegate
        self.notes = notes
        # Extra facts for the model from the app (where files go, what it can't do).
        self.app_note: str | None = None
        # FRIDAY's real history with the owner, worked out fresh for each reply.
        self.timeline: Callable[[], str | None] | None = None
        # Searches past chats when asked about them ("what did we say yesterday?").
        self.recall: Callable[[str], Awaitable[str | None]] | None = None
        self.studio = studio
        # An incognito chat: nothing goes into the memory file, and no task goes
        # to Claude Code (it keeps its own record of every task).
        self.incognito = False

    def is_control(self, text: str) -> bool:
        """Answered by FRIDAY's own fixed logic, not by the language model."""
        if parse_command(text) is not None:
            return True
        if self.delegate is not None and self.delegate.concerns(text):
            return True
        if self.delegate is None and is_agent_command(text):
            return True
        if self.studio is not None and self.studio.concerns(text):
            return True
        if self.studio is None and is_studio_command(text):
            return True
        return self.notes is not None and self.notes.concerns(text)

    @property
    def awaiting_answer(self) -> bool:
        """FRIDAY asked a yes/no question, so "yes" is an answer, not a murmur."""
        if self.studio is not None and self.studio.awaiting_answer:
            return True
        return self.delegate is not None and self.delegate.pending is not None

    def dismiss_pending(self) -> None:
        if self.delegate is not None:
            self.delegate.dismiss()
        if self.studio is not None:
            self.studio.dismiss()

    async def say(self, text: str, context: str | None = None) -> AsyncGenerator[str, None]:
        """Speak something FRIDAY decided to say herself (a finished task).

        It is stored in history, with ``context`` as the user side, so the
        user can ask about it afterwards.
        """
        self.record_notice(text, context)
        yield text

    def record_notice(self, text: str, context: str | None = None) -> None:
        turn = TurnInfo(mode=self.mode)
        self.last_turn = turn
        self.window.add_user(context or "(no message)")
        self.window.add_assistant(text)
        turn.user_stored = turn.stored = True
        turn.full_text = text

    def build_request(self, web_note: str | None = None) -> ChatRequest:
        extra = "\n\n".join(p for p in (self._extra_context(), web_note) if p) or None
        return ChatRequest(
            system=self.persona.system_prompt(self.mode, self.channel, extra),
            messages=self.window.messages,
            max_tokens=self._max_tokens[self.channel],
            temperature=0.8 if self.mode is Mode.COMPANION else 0.4,
            think=self.think,
        )

    def _extra_context(self) -> str | None:
        parts = [
            self._delegate_context(),
            self._studio_context(),
            self.notes.context() if self.notes else None,
            self.app_note,
            self._timeline(),
        ]
        return "\n\n".join(p for p in parts if p) or None

    def _timeline(self) -> str | None:
        if self.timeline is None:
            return None
        try:
            return self.timeline()
        except Exception:
            log.warning("could not work out the timeline", exc_info=True)
            return None

    def _delegate_context(self) -> str | None:
        if self.delegate is None:
            return (
                "You cannot run Claude Code, any other coding agent, or any program or "
                f"command. If {self.persona.user_name} asks you to, say that it is not set "
                'up yet. Never say "I\'m running that" or describe files, output or results: '
                "nothing ran, so anything you described would be made up."
            )
        name = self.delegate.name
        sent = self.delegate.sent[-5:]
        record = (
            "Tasks really sent to it in this conversation: "
            + "; ".join(f'"{t[:120]}"' for t in sent)
            + ". Its answers appear as messages starting with "
            f'"{name} is done" or "{name} ran into a problem".'
            if sent
            else f"No task has been sent to {name} in this conversation."
        )
        truth = (
            f" {record} Messages you answer yourself are NOT sent to {name}: never say you "
            f"asked it, never write a reply as if it came from {name}, and if asked whether "
            f"you did, answer from this record. If {self.persona.user_name} wants {name} "
            f'to do something, tell them to say "ask {name} to" followed by the task.'
        )
        return (
            f"One exception to the limits above: {name}, a coding agent on this computer, can "
            f"be given tasks. That only happens when {self.persona.user_name} says "
            f'"ask {name} to ..." and then confirms; it is handled for you. Never claim '
            f"{name} did something unless its result appears in this conversation, and "
            "never pretend to do coding work yourself."
        ) + truth

    def _studio_context(self) -> str:
        user = self.persona.user_name
        if self.studio is None:
            return (
                "You cannot make pictures, images, music or songs. If "
                f"{user} asks for one, say that it is not set up yet. Never describe a "
                "picture or song as if you had made it."
            )
        return (
            "Pictures and music can be made on this computer, but only when "
            f'{user} says "make a picture of ..." or "make a song about ..." and then '
            "confirms; it is handled for you. If they seem to want one and it was not "
            f'picked up, never say it cannot be done: tell {user} to say "make a picture '
            'of" followed by what it should show. Never say a picture or song is made, '
            "or describe it, unless that appears in this conversation."
        )

    async def respond(
        self, user_text: str, images: Sequence[str] = ()
    ) -> AsyncGenerator[str, None]:
        """Yield the reply to ``user_text`` as text deltas.

        ``images`` (base64) are pictures sent with the message: the "vision"
        route answers, and only this turn's request carries them; history
        keeps the words.

        History is updated even if the consumer stops early (barge-in): the
        generated-so-far text is stored, and the caller may then refine it with
        ``record_interruption`` to what the user actually heard.
        """
        user_text = user_text.strip()
        turn = TurnInfo(mode=self.mode)
        self.last_turn = turn
        if not user_text:
            return

        reply: str | None
        if images and not self.router.has_route("vision"):
            reply = PICTURES_NOT_SET_UP
            turn.was_command = True
        else:
            reply = None if images else self._control(user_text, turn)
        if reply is not None:
            turn.full_text = reply
            yield reply
            return

        self.window.add_user(user_text)
        turn.user_stored = True
        web_note = None
        if self.web is not None and not images:
            web_note = await self._look_up(user_text, turn) or NO_SEARCH_NOTE
        if self.recall is not None and not images and wants_recall(user_text):
            try:
                found = await self.recall(user_text)
            except Exception:
                log.warning("could not search past chats", exc_info=True)
                found = None
            web_note = "\n\n".join(p for p in (found, web_note) if p) or None
        request = self.build_request(web_note)
        purpose = MODE_PURPOSE[self.mode]
        if images:
            purpose = "vision"
            last = request.messages[-1]
            seen = Message(last.role, last.content, tuple(images))
            request = replace(request, messages=(*request.messages[:-1], seen), think=None)
        result = StreamResult()
        parts: list[str] = []
        completed = False
        stream = self.router.stream(purpose, request, result)
        try:
            async with aclosing(stream):
                async for delta in stream:
                    parts.append(delta)
                    yield delta
            completed = True
        except ProviderError as exc:
            log.error("turn failed: %s", exc)
            turn.failed = True
            if not parts:
                turn.full_text = FAILURE_REPLY
                # Not stored in history: the user message stays, and the next
                # user turn is merged into it so nothing they said is lost.
                yield FAILURE_REPLY
                return
            # The provider died mid-sentence. Say so rather than going silent.
            cut = " ... sorry, I lost my connection there."
            parts.append(cut)
            yield cut
            completed = True
        finally:
            turn.provider = result.provider
            text = without_sounds("".join(parts))
            if text:
                turn.full_text = text
                self.window.add_assistant(text if completed else text + INTERRUPTED_MARKER)
                turn.stored = True

    def _activity(self, text: str) -> None:
        if self.on_activity is not None:
            try:
                self.on_activity(text)
            except Exception:
                log.exception("activity listener failed")

    async def _look_up(self, text: str, turn: TurnInfo) -> str | None:
        """Search the internet first when the message needs it; the note for the model."""
        web = self.web
        assert web is not None
        user = self.persona.user_name
        query: str | None = None
        try:
            urls = urls_in(text)
            if urls:
                self._activity("Reading " + ", ".join(urls))
                found: Findings = await web.open_pages(urls)
            else:
                query = asked_query(text)
                if query is None and web.auto and (is_fresh(text) or maybe_needs_web(text)):
                    query = await self._plan_search()
                if query is None:
                    return None
                self._activity(f"Searching the web: {query}")
                found = await web.lookup(query)
        except WebError as exc:
            log.warning("web search failed: %s", exc)
            return (
                f"A web search was needed for this but it failed ({exc}). Tell {user} the "
                "search didn't work this time, and don't guess about live or recent things."
            )
        if not found.results:
            return (
                f'A web search for "{query}" found nothing. Tell {user} so, rather than guessing.'
            )
        turn.sources = found.sources()
        return found.context()

    async def _plan_search(self) -> str | None:
        """Ask the talking model, in one line, whether to search and for what."""
        recent = self.window.messages[-7:]
        convo = "\n".join(f"{m.role.value}: {m.content[:500]}" for m in recent)
        today = datetime.now().strftime("%A %d %B %Y")
        request = ChatRequest(
            system=PLANNER_PROMPT.format(today=today),
            messages=(
                Message(
                    Role.USER,
                    f"Conversation:\n{convo}\n\nDoes the last message need a web search?",
                ),
            ),
            max_tokens=60,
            temperature=0.0,
            think=False,
        )
        parts: list[str] = []

        async def ask() -> None:
            stream = self.router.stream(MODE_PURPOSE[self.mode], request, StreamResult())
            async with aclosing(stream):
                async for delta in stream:
                    parts.append(delta)

        try:
            await asyncio.wait_for(ask(), 20)
        except (ProviderError, TimeoutError) as exc:
            log.warning("could not decide about a web search: %s", exc)
            return None
        return planner_query("".join(parts))

    def _control(self, user_text: str, turn: TurnInfo) -> str | None:
        """A fixed reply when the words are a command or a task, not conversation."""
        command = parse_command(user_text)
        if command is not None:
            self.mode = command.mode
            turn.mode = command.mode
            turn.was_command = True
            ack = self.persona.mode_acknowledgement(command.mode)
            return ack  # commands are control, not conversation: not stored in history

        if self.incognito and (
            is_agent_command(user_text)
            or (self.delegate is not None and self.delegate.concerns(user_text))
        ):
            turn.was_command = True
            return INCOGNITO_NO_TASKS
        if self.delegate is not None:
            reply = self.delegate.handle(user_text)
            if reply is not None:  # also control, not conversation
                turn.was_command = True
                return reply
        elif is_agent_command(user_text):
            # Not switched on: say so in fixed words. Left to the model, it
            # claimed to run the task and made up the result.
            turn.was_command = True
            return not_set_up_reply()

        if self.studio is not None:
            reply = self.studio.handle(user_text)
            if reply is not None:
                turn.was_command = True
                return reply
        elif (asked := parse_request(user_text)) is not None:
            reply = studio_not_set_up(asked.kind)
            turn.was_command = True
            return reply

        if self.incognito and self.notes is not None and self.notes.writes(user_text):
            turn.was_command = True
            return INCOGNITO_NO_NOTES
        if self.notes is not None:
            reply = self.notes.handle(user_text)
            if reply is not None:  # stored on disk, not in the conversation
                turn.was_command = True
                return reply
        return None

    def record_interruption(self, heard_text: str) -> None:
        """Replace the last reply with the part the user actually heard."""
        if not self.last_turn.stored:
            return  # interrupted before anything was stored, or a command ack
        heard = without_sounds(heard_text) or "(nothing heard)"
        self.window.replace_last_assistant(heard + INTERRUPTED_MARKER)

    def discard_turn(self) -> None:
        """Forget the last turn entirely, as if it had never been asked.

        Used when the user turns out to still be mid-sentence: the caller
        re-submits the whole thought as one turn.
        """
        turn = self.last_turn
        if turn.stored:
            self.window.drop_last(Role.ASSISTANT)
            turn.stored = False
        if turn.user_stored:
            self.window.drop_last(Role.USER)
            turn.user_stored = False

    def set_mode(self, mode: Mode) -> None:
        self.mode = mode
