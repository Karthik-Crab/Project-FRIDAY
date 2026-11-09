import pytest

from friday.conversation.memory import ConversationWindow
from friday.core.messages import Role


def test_alternation_and_merge_of_consecutive_user_turns() -> None:
    w = ConversationWindow()
    w.add_user("hello")
    w.add_user("are you there?")
    assert [m.role for m in w.messages] == [Role.USER]
    assert w.messages[0].content == "hello\nare you there?"
    w.add_assistant("yes")
    with pytest.raises(ValueError):
        w.add_assistant("again")


def test_trimming_keeps_window_starting_with_user() -> None:
    w = ConversationWindow(max_messages=4)
    for i in range(5):
        w.add_user(f"u{i}")
        w.add_assistant(f"a{i}")
    msgs = w.messages
    assert len(msgs) <= 4
    assert msgs[0].role is Role.USER
    assert msgs[-1].content == "a4"


def test_char_budget_trimming() -> None:
    w = ConversationWindow(max_messages=100, max_chars=50)
    for i in range(10):
        w.add_user("x" * 10 + str(i))
        w.add_assistant("y" * 10)
    assert sum(len(m.content) for m in w.messages) <= 50
    assert w.messages[0].role is Role.USER
