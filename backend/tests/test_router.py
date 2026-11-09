import pytest

from friday.core.errors import ProviderError, ProviderUnavailableError
from friday.core.messages import ChatRequest, Message, Role
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter, StreamResult

REQ = ChatRequest(system="s", messages=(Message(Role.USER, "hi"),))


async def run(router: ProviderRouter, purpose: str = "default") -> tuple[str, StreamResult]:
    result = StreamResult()
    text = "".join([d async for d in router.stream(purpose, REQ, result)])
    return text, result


async def test_falls_back_before_first_token_and_applies_cooldown() -> None:
    now = [0.0]
    cloud = FakeProvider("cloud", fail_with=ProviderError("cloud", "503"), local=False)
    local = FakeProvider("local", reply="offline answer")
    router = ProviderRouter(
        {"cloud": cloud, "local": local},
        {"conversation": ["cloud", "local"]},
        cooldown_seconds=30,
        clock=lambda: now[0],
    )
    text, result = await run(router, "conversation")
    assert text == "offline answer"
    assert result.provider is not None and result.provider.name == "local"
    assert len(cloud.requests) == 1
    await run(router, "conversation")
    assert len(cloud.requests) == 1  # skipped while cooling down
    now[0] = 31
    await run(router, "conversation")
    assert len(cloud.requests) == 2


async def test_mid_stream_failure_is_not_spliced() -> None:
    a = FakeProvider("a", reply="one two three", fail_after_chunks=1)
    b = FakeProvider("b", reply="other")
    router = ProviderRouter({"a": a, "b": b}, {"default": ["a", "b"]})
    with pytest.raises(ProviderError):
        await run(router)
    assert b.requests == []


async def test_offline_mode_uses_only_local_providers() -> None:
    cloud = FakeProvider("cloud", reply="cloud", local=False)
    local = FakeProvider("local", reply="local")
    router = ProviderRouter(
        {"cloud": cloud, "local": local}, {"default": ["cloud", "local"]}, offline=True
    )
    assert (await run(router))[0] == "local"


async def test_offline_route_is_always_appended_as_last_resort() -> None:
    cloud = FakeProvider("cloud", fail_with=ProviderError("cloud", "x"), local=False)
    local = FakeProvider("local", reply="still here")
    router = ProviderRouter(
        {"cloud": cloud, "local": local}, {"conversation": ["cloud"], "offline": ["local"]}
    )
    assert (await run(router, "conversation"))[0] == "still here"


async def test_manual_override() -> None:
    a, b = FakeProvider("a", reply="A"), FakeProvider("b", reply="B")
    router = ProviderRouter({"a": a, "b": b}, {"default": ["a"]})
    router.set_override("b")
    assert (await run(router))[0] == "B"
    with pytest.raises(ValueError):
        router.set_override("nope")


async def test_all_failed_raises_unavailable() -> None:
    a = FakeProvider("a", fail_with=ProviderError("a", "x"))
    router = ProviderRouter({"a": a}, {"default": ["a"]})
    with pytest.raises(ProviderUnavailableError):
        await run(router)


def test_unknown_provider_in_route_rejected() -> None:
    with pytest.raises(ValueError):
        ProviderRouter({}, {"default": ["ghost"]})
