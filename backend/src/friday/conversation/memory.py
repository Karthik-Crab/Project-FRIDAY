"""Short-term (in-session) conversation memory.

Phase 1 only needs the immediate conversation. Persistent personal/project
memory (Phase 4) will sit behind a separate interface and contribute to the
system prompt via ``extra_context``; it will not replace this window.
"""

from __future__ import annotations

from friday.core.messages import Message, Role

INTERRUPTED_MARKER = " [interrupted]"


class ConversationWindow:
    """Bounded user/assistant history with strict role alternation.

    Strict alternation (user, assistant, user, ...) is required by some APIs
    (Anthropic) and harmless for the others, so we enforce it once here instead
    of in each adapter.
    """

    def __init__(self, max_messages: int = 40, max_chars: int = 24_000) -> None:
        if max_messages < 2:
            raise ValueError("max_messages must be >= 2")
        self._messages: list[Message] = []
        self._max_messages = max_messages
        self._max_chars = max_chars

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def add_user(self, text: str) -> None:
        if self._messages and self._messages[-1].role is Role.USER:
            # Two user turns in a row (e.g. the previous reply failed): merge
            # them so the model sees everything the user said.
            prev = self._messages.pop()
            text = f"{prev.content}\n{text}"
        self._messages.append(Message(Role.USER, text))
        self._trim()

    def add_assistant(self, text: str) -> None:
        if not self._messages or self._messages[-1].role is not Role.USER:
            raise ValueError("assistant message must follow a user message")
        self._messages.append(Message(Role.ASSISTANT, text))
        self._trim()

    def replace_last_assistant(self, text: str) -> None:
        if not self._messages or self._messages[-1].role is not Role.ASSISTANT:
            raise ValueError("no assistant message to replace")
        self._messages[-1] = Message(Role.ASSISTANT, text)

    def clear(self) -> None:
        self._messages.clear()

    def _trim(self) -> None:
        def too_big() -> bool:
            return len(self._messages) > self._max_messages or (
                sum(len(m.content) for m in self._messages) > self._max_chars
            )

        # Drop oldest messages, keeping the window starting on a user turn.
        while too_big() and len(self._messages) > 1:
            self._messages.pop(0)
            while self._messages and self._messages[0].role is not Role.USER:
                self._messages.pop(0)
