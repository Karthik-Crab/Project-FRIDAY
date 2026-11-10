"""The conversation engine: one user turn in, a stream of reply text out.

It owns mode, persona prompt assembly, short-term memory and provider routing.
It knows nothing about audio: the voice pipeline and the text CLI (and later
the desktop UI) are just different front-ends on top of ``respond``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass, field

from friday.conversation.memory import INTERRUPTED_MARKER, ConversationWindow
from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest, Role
from friday.llm.base import ProviderInfo
from friday.llm.router import ProviderRouter, StreamResult
from friday.persona.commands import parse_command
from friday.persona.persona import Channel, Mode, Persona

log = logging.getLogger(__name__)

# Which routing purpose each mode uses. Config decides which providers serve
# each purpose; the engine never names a vendor.
MODE_PURPOSE: dict[Mode, str] = {Mode.COMPANION: "conversation", Mode.WORK: "reasoning"}

FAILURE_REPLY = (
    "Sorry, I can't reach any of my language models right now. Give me a moment and try again."
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
    ) -> None:
        self.router = router
        self.persona = persona or Persona()
        self.window = window or ConversationWindow()
        self.channel = channel
        self.mode = mode
        self._max_tokens = max_tokens or {Channel.VOICE: 400, Channel.TEXT: 1500}
        self.last_turn = TurnInfo(mode=mode)

    def build_request(self) -> ChatRequest:
        return ChatRequest(
            system=self.persona.system_prompt(self.mode, self.channel),
            messages=self.window.messages,
            max_tokens=self._max_tokens[self.channel],
            temperature=0.8 if self.mode is Mode.COMPANION else 0.4,
        )

    async def respond(self, user_text: str) -> AsyncGenerator[str, None]:
        """Yield the reply to ``user_text`` as text deltas.

        History is updated even if the consumer stops early (barge-in): the
        generated-so-far text is stored, and the caller may then refine it with
        ``record_interruption`` to what the user actually heard.
        """
        user_text = user_text.strip()
        turn = TurnInfo(mode=self.mode)
        self.last_turn = turn
        if not user_text:
            return

        command = parse_command(user_text)
        if command is not None:
            self.mode = command.mode
            turn.mode = command.mode
            turn.was_command = True
            ack = self.persona.mode_acknowledgement(command.mode)
            turn.full_text = ack
            yield ack
            return  # commands are control, not conversation: not stored in history

        self.window.add_user(user_text)
        turn.user_stored = True
        request = self.build_request()
        result = StreamResult()
        parts: list[str] = []
        completed = False
        stream = self.router.stream(MODE_PURPOSE[self.mode], request, result)
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
            text = "".join(parts).strip()
            if text:
                turn.full_text = text
                self.window.add_assistant(text if completed else text + INTERRUPTED_MARKER)
                turn.stored = True

    def record_interruption(self, heard_text: str) -> None:
        """Replace the last reply with the part the user actually heard."""
        if not self.last_turn.stored:
            return  # interrupted before anything was stored, or a command ack
        heard = heard_text.strip() or "(nothing heard)"
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
