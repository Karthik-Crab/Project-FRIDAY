"""Provider-neutral conversation data types.

Every provider adapter translates *from* these types. Nothing in here may
reference a specific vendor's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str
    # Pictures sent with this message, base64-encoded. Only models that can
    # see (the "vision" route) are given them.
    images: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A complete, self-contained request to a language model.

    ``system`` is kept separate from ``messages`` because providers disagree on
    where it goes (OpenAI: first message, Anthropic: top-level field).
    """

    system: str
    messages: tuple[Message, ...]
    max_tokens: int = 1024
    temperature: float = 0.7
    # Think before answering (models that can, e.g. Qwen3). None: as configured.
    think: bool | None = None
    metadata: dict[str, str] = field(default_factory=dict)
