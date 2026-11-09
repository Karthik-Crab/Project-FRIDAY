from friday.conversation.engine import FAILURE_REPLY, ConversationEngine
from friday.conversation.memory import INTERRUPTED_MARKER
from friday.core.errors import ProviderError
from friday.core.messages import Role
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Mode


async def collect(agen) -> str:  # type: ignore[no-untyped-def]
    return "".join([d async for d in agen])


async def test_reply_streams_and_is_remembered(engine: ConversationEngine, fake) -> None:  # type: ignore[no-untyped-def]
    text = await collect(engine.respond("hi"))
    assert text == "Sure. Here is a perfectly reasonable answer for you."
    await collect(engine.respond("and again"))
    # second request carries the full immediate history
    sent = fake.requests[-1].messages
    assert [m.role for m in sent] == [Role.USER, Role.ASSISTANT, Role.USER]
    assert sent[0].content == "hi"


async def test_mode_switch_is_local_instant_and_not_in_history(
    engine: ConversationEngine,
    fake,  # type: ignore[no-untyped-def]
) -> None:
    ack = await collect(engine.respond("Friday, work mode."))
    assert engine.mode is Mode.WORK
    assert "Work mode" in ack
    assert fake.requests == []  # no provider call
    assert engine.window.messages == ()
    await collect(engine.respond("status?"))
    assert "Current mode: WORK" in fake.requests[-1].system
    await collect(engine.respond("Friday, companion mode"))
    assert engine.mode is Mode.COMPANION


async def test_modes_route_to_different_purposes() -> None:
    chat = FakeProvider("chat", reply="chatty")
    think = FakeProvider("think", reply="precise")
    router = ProviderRouter(
        {"chat": chat, "think": think},
        {"conversation": ["chat"], "reasoning": ["think"]},
    )
    engine = ConversationEngine(router)
    assert await collect(engine.respond("hey")) == "chatty"
    engine.set_mode(Mode.WORK)
    assert await collect(engine.respond("analyse")) == "precise"
    assert engine.last_turn.provider is not None and engine.last_turn.provider.name == "think"


async def test_total_failure_gives_spoken_apology_and_keeps_user_text() -> None:
    bad = FakeProvider("bad", fail_with=ProviderError("bad", "down"))
    engine = ConversationEngine(
        ProviderRouter({"bad": bad}, {"default": ["bad"]}, cooldown_seconds=0)
    )
    assert await collect(engine.respond("first thing")) == FAILURE_REPLY
    assert engine.last_turn.failed
    # user's words are not lost: they are merged into the next turn
    await collect(engine.respond("second thing"))
    assert bad.requests[-1].messages[-1].content == "first thing\nsecond thing"


async def test_mid_stream_failure_is_acknowledged() -> None:
    flaky = FakeProvider("flaky", reply="one two three four", fail_after_chunks=2)
    engine = ConversationEngine(ProviderRouter({"flaky": flaky}, {"default": ["flaky"]}))
    text = await collect(engine.respond("go"))
    assert text.startswith("one two")
    assert "lost my connection" in text


async def test_early_close_stores_partial_reply_marked_interrupted(
    engine: ConversationEngine,
) -> None:
    gen = engine.respond("tell me a story")
    first = await gen.__anext__()
    await gen.aclose()
    last = engine.window.messages[-1]
    assert last.role is Role.ASSISTANT
    assert last.content == first + INTERRUPTED_MARKER


async def test_record_interruption_rewrites_to_heard_text(engine: ConversationEngine) -> None:
    await collect(engine.respond("hi"))
    engine.record_interruption("Sure. Here is")
    assert engine.window.messages[-1].content == "Sure. Here is" + INTERRUPTED_MARKER


async def test_record_interruption_ignores_command_turns(engine: ConversationEngine) -> None:
    await collect(engine.respond("hi"))
    before = engine.window.messages
    await collect(engine.respond("Friday, work mode"))
    engine.record_interruption("Work")
    assert engine.window.messages == before
