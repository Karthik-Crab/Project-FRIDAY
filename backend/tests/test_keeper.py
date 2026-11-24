"""What FRIDAY keeps from chats: summaries, suggested notes, and searching past chats."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_attachments import ORIGIN

from friday.app.server import create_app
from friday.app.services import AppServices
from friday.app.store import ConversationStore
from friday.conversation.engine import ConversationEngine
from friday.conversation.keeper import grounded, keep
from friday.conversation.notes import NoteBook
from friday.conversation.recall import Line, search, wants_recall
from friday.core.messages import ChatRequest
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel

CHAT = [
    ("user", "Hey Friday, I work as a GRC analyst and I love flute music."),
    ("assistant", "Nice! Flute music is lovely."),
    ("user", "Make a flute version of this song for my sister Priya."),
    ("assistant", "Done, the flute version is ready."),
]


def _asker(reply: dict[str, Any] | str) -> Any:
    asked: list[ChatRequest] = []

    async def ask(request: ChatRequest) -> str:
        asked.append(request)
        return reply if isinstance(reply, str) else json.dumps(reply)

    ask.asked = asked  # type: ignore[attr-defined]
    return ask


# Facts must come from the owner's own words


def test_a_fact_nobody_said_is_left_out() -> None:
    said = "\n".join(t for r, t in CHAT if r == "user")
    assert grounded("Karthik loves flute music", said, "Karthik")
    assert grounded("Karthik works as a GRC analyst", said, "Karthik")
    assert not grounded("Karthik is a perfectionist", said, "Karthik")
    assert not grounded("Karthik", said, "Karthik")


async def test_summary_and_suggested_facts_from_a_chat() -> None:
    ask = _asker(
        "<think>hmm</think>"
        + json.dumps(
            {
                "summary": "Karthik asked for a flute version of a song for his sister.",
                "facts": [
                    "Karthik works as a GRC analyst",
                    "Karthik loves flute music",
                    "Karthik is a perfectionist",  # never said
                    "Karthik has a sister called Priya",
                ],
            }
        )
    )
    kept = await keep(CHAT, ask, "Karthik", known=["[2026-11-20] Karthik loves flute music"])
    assert kept.summary.startswith("Karthik asked for a flute version")
    # the made-up trait is dropped, and so is what the notes already say
    assert kept.facts == ("Karthik works as a GRC analyst", "Karthik has a sister called Priya")
    request = ask.asked[0]
    assert request.think is False and "FRIDAY: Done" in request.messages[0].content


async def test_requests_are_not_kept_as_facts() -> None:
    chat = [("user", "I need to give my car for service. Make a warrior with a white beard.")]
    ask = _asker(
        {
            "summary": "Car service and a picture.",
            "facts": [
                "Karthik asked for a warrior with a white beard",
                "Karthik wants a warrior with a white beard",
                "Karthik needs to give his car for service",
            ],
        }
    )
    kept = await keep(chat, ask, "Karthik")
    assert kept.facts == ("Karthik needs to give his car for service",)


async def test_a_reply_without_json_is_an_error() -> None:
    with pytest.raises(ValueError):
        await keep(CHAT, _asker("Sure! Here's a summary."), "Karthik")


# Which chats get summarised


def test_only_quiet_saved_chats_with_new_messages_wait_for_a_summary(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "chats.db")
    chat = store.create()
    store.add(chat.id, "user", "text", "hi")
    last = store.add(chat.id, "assistant", "text", "hello")
    secret = store.create(incognito=True)
    store.add(secret.id, "user", "text", "a")
    store.add(secret.id, "assistant", "text", "b")
    assert store.needing_summary(time.time() - 600) == []  # still going
    assert store.needing_summary(time.time() + 1) == [chat.id]  # incognito never
    store.set_summary(chat.id, "A greeting.", last.id)
    assert store.needing_summary(time.time() + 1) == []
    store.add(chat.id, "user", "text", "one more thing")
    assert store.needing_summary(time.time() + 1) == [chat.id]
    assert store.overview()[0].summary == "A greeting."


# The app: summarise, suggest, keep or drop


def _app(tmp_path: Path, reply: dict[str, Any] | str) -> tuple[AppServices, NoteBook]:
    router = ProviderRouter({"gpu": FakeProvider("gpu", reply="Sure.")}, {"default": ["gpu"]})
    notes = NoteBook(tmp_path / "memory")
    services = AppServices(
        ConversationStore(tmp_path / "chats.db"),
        lambda _: ConversationEngine(router, channel=Channel.TEXT, notes=notes),
        notes=notes,
        ask=_asker(reply),
    )
    return services, notes


def _saved_chat(services: AppServices) -> str:
    chat = services.store.create()
    for role, text in CHAT:
        services.store.add(chat.id, role, "text", text)
    return chat.id


async def test_a_quiet_chat_gets_a_summary_and_suggestions_wait_for_the_owner(
    tmp_path: Path,
) -> None:
    services, notes = _app(
        tmp_path,
        {"summary": "A flute song for Priya.", "facts": ["Karthik loves flute music"]},
    )
    events: list[dict[str, Any]] = []
    services.emit = events.append  # type: ignore[method-assign]
    cid = _saved_chat(services)
    assert await services.keep_one(idle_s=-1) == cid
    assert await services.keep_one(idle_s=-1) is None  # done until it changes
    assert services.store.summary(cid)[0] == "A flute song for Priya."
    [item] = services.suggestions()
    assert item["fact"] == "Karthik loves flute music" and notes.facts() == []  # not yet
    assert events[-1]["type"] == "suggestions"
    # The timeline shows the summary from now on.
    other = services.store.create()
    assert "About: A flute song for Priya." in str(services.engine(other.id).timeline())

    with TestClient(create_app(services, port=8765), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/suggestions").json()["items"][0]["id"] == item["id"]
        url = f"/api/suggestions/{item['id']}"
        assert client.post(url, json={"keep": "yes"}, headers=ORIGIN).status_code == 400
        assert client.post(url, json={"keep": True}, headers=ORIGIN).status_code == 200
        assert client.post(url, json={"keep": True}, headers=ORIGIN).status_code == 409
        assert services.suggestions() == []
    assert [f.split("] ", 1)[1] for f in notes.facts()] == ["Karthik loves flute music"]


async def test_nothing_is_summarised_while_friday_is_busy_or_after_a_failure(
    tmp_path: Path,
) -> None:
    services, _ = _app(tmp_path, "not json")
    cid = _saved_chat(services)

    class Voice:
        state, conversation_id, available = "listening", None, True

    services.voice = Voice()  # type: ignore[assignment]
    assert await services.keep_one(idle_s=-1) is None
    services.voice = None
    assert await services.keep_one(idle_s=-1) is None  # the model gave no JSON
    assert cid in services._keep_failed
    assert services.store.summary(cid) == ("", 0)


# Searching past chats


def _line(day: int, hour: int, role: str, text: str) -> Line:
    return Line("c", role, text, datetime(2026, 11, day, hour).timestamp())


LINES = [
    _line(21, 19, "user", "make a flute instrumental of this song"),
    _line(21, 19, "assistant", "The flute version is ready."),
    _line(22, 18, "user", "Hey Friday, what is 2 plus 2?"),
    _line(23, 10, "user", "can you search online for the best family movie?"),
]
NOW = datetime(2026, 11, 24, 12)


def test_questions_about_the_past_are_recognised() -> None:
    for asked in (
        "Do you remember the flute song?",
        "What did we talk about yesterday?",
        "what did I tell you last time",
        "Remind me what you said 3 days ago",
        "What did I ask you about my car?",
        "What did we talk about two days ago?",
    ):
        assert wants_recall(asked), asked
    for asked in ("What is 2 plus 2?", "Make a picture of a cat", "Did you eat?"):
        assert not wants_recall(asked), asked


def test_matching_lines_come_back_with_their_dates() -> None:
    found = search("Do you remember the flute song?", LINES, "Karthik", NOW)
    assert 'Saturday 21 November 2026 at 19:00, Karthik said: "make a flute' in found
    assert "you (FRIDAY) said" in found and "family movie" not in found


def test_a_named_day_limits_the_search() -> None:
    found = search("What did we talk about yesterday?", LINES, "Karthik", NOW)
    assert "family movie" in found and "flute" not in found
    nothing = search("Did we talk about cricket?", LINES, "Karthik", NOW)
    assert "found nothing matching" in nothing and "never guess" in nothing


async def test_friday_searches_only_when_asked_about_the_past(tmp_path: Path) -> None:
    services, _ = _app(tmp_path, {"summary": "", "facts": []})
    _saved_chat(services)
    now = services.store.create()
    engine = services.engine(now.id)
    model = engine.router.providers["gpu"]
    [_ async for _ in engine.respond("Do you remember the flute song?")]
    assert "flute version of this song" in model.requests[-1].system  # type: ignore[attr-defined]
    [_ async for _ in engine.respond("What is 2 plus 2?")]
    assert "Lines found by searching" not in model.requests[-1].system  # type: ignore[attr-defined]
