"""FRIDAY's sense of time: she answers "how long have you known me?" from the record."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from friday.app.services import AppServices
from friday.app.store import ConversationStore
from friday.conversation.engine import ConversationEngine
from friday.conversation.timeline import ChatSummary, timeline
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel, Mode, Persona

NOW = datetime(2026, 11, 24, 12, 50)


def _ts(day: int, hour: int) -> float:
    return datetime(2026, 11, day, hour, 0).timestamp()


def test_the_first_chat_is_called_the_first() -> None:
    text = timeline([ChatSummary("a", _ts(24, 12), _ts(24, 12), 2, ("Hi",))], "a", "Alex", NOW)
    assert "This is the first conversation you have had with Alex." in text
    assert "This conversation started today at 12:00." in text


def test_dates_counts_and_openings_come_from_the_saved_chats() -> None:
    chats = [
        ChatSummary("old", _ts(21, 15), _ts(21, 16), 44, ("Hey Friday", "make a song")),
        ChatSummary("empty", _ts(22, 9), _ts(22, 9), 0),  # opened, never used
        ChatSummary("mid", _ts(23, 18), _ts(23, 19), 6, ("what is 2 plus 2?",)),
        ChatSummary("now", _ts(24, 12), _ts(24, 12), 4, ("Do you remember me?",)),
    ]
    text = timeline(chats, "now", "Alex", NOW)
    assert "You first talked Saturday 21 November 2026 at 15:00 (3 days ago)." in text
    assert "3 saved chats so far, on 3 different days." in text
    assert "The last chat before this one: yesterday at 19:00." in text
    assert '44 messages. Began: "Hey Friday" / "make a song"' in text
    assert "Do you remember me?" not in text  # this chat is in the window already


def test_a_long_history_keeps_the_first_chat_and_the_latest_ones() -> None:
    chats = [
        ChatSummary(str(i), _ts(1, 0) + i * 3600, _ts(1, 0), 2, (f"chat {i}",)) for i in range(20)
    ]
    text = timeline(chats, None, "Alex", NOW)
    assert '"chat 0"' in text and '"chat 19"' in text and '"chat 5"' not in text
    assert "(11 more chats in between are not listed.)" in text


def test_incognito_chats_stay_out_of_the_history(tmp_path: Path) -> None:
    store = ConversationStore(tmp_path / "chats.db")
    open_chat = store.create()
    store.add(open_chat.id, "user", "text", "Hey Friday")
    store.add(open_chat.id, "user", "text", "make a song")
    store.add(open_chat.id, "user", "text", "a third thing")
    secret = store.create(incognito=True)
    store.add(secret.id, "user", "text", "a secret")
    store.create()  # never used
    [summary] = store.overview()
    assert summary.id == open_chat.id and summary.messages == 3
    assert summary.opening == ("Hey Friday", "make a song")


def test_the_app_tells_friday_her_history_on_every_reply(tmp_path: Path) -> None:
    talk = FakeProvider("gpu", reply="Sure.")
    router = ProviderRouter({"gpu": talk}, {"default": ["gpu"]})
    store = ConversationStore(tmp_path / "chats.db")
    earlier = store.create()
    store.add(earlier.id, "user", "text", "make a flute song")
    services = AppServices(store, lambda _: ConversationEngine(router, channel=Channel.TEXT))
    current = store.create()
    system = services.engine(current.id).build_request().system
    assert "Your real history with Alex" in system
    assert '"make a flute song"' in system


def test_friday_knows_the_date_and_what_she_is() -> None:
    prompt = Persona().system_prompt(Mode.COMPANION, Channel.VOICE)
    assert datetime.now().strftime("%A %d %B %Y") in prompt
    assert "you have never helped\n  anyone else" in prompt
    assert "You do not learn, grow, adapt or get smarter from talking" in prompt
