"""Wire-format tests for the HTTP adapters, using httpx.MockTransport."""

import json

import httpx
import pytest

from friday.core.errors import ProviderError
from friday.core.messages import ChatRequest, Message, Role
from friday.llm.providers.anthropic import AnthropicProvider
from friday.llm.providers.ollama import OllamaProvider
from friday.llm.providers.openai_compat import OpenAICompatProvider

REQ = ChatRequest(
    system="be nice",
    messages=(Message(Role.USER, "hi"), Message(Role.ASSISTANT, "hey"), Message(Role.USER, "?")),
    max_tokens=50,
)


def client(handler, base: str, headers: dict[str, str] | None = None) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=base, headers=headers or {}
    )


async def collect(provider) -> str:  # type: ignore[no-untyped-def]
    return "".join([d async for d in provider.stream(REQ)])


async def test_ollama_request_and_stream() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        lines = [
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

    p = OllamaProvider("local", "llama", client=client(handler, "http://x"))
    assert await collect(p) == "Hello"
    assert seen["path"] == "/api/chat"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be nice"}
    assert seen["body"]["stream"] is True
    assert p.info.local


async def test_openai_request_and_stream() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        events = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"content": "Hi "}}]},
            {"choices": [{"delta": {"content": "there"}}]},
        ]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body)

    c = client(handler, "https://api.test/v1", {"Authorization": "Bearer sk-test"})
    p = OpenAICompatProvider("oa", "m", "sk-test", client=c)
    assert await collect(p) == "Hi there"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"][0]["role"] == "system"
    assert len(seen["body"]["messages"]) == 4


async def test_anthropic_request_and_stream() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        events = [
            {"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Good "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "morning"},
            },
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, text=body)

    p = AnthropicProvider("cl", "model-x", "k", client=client(handler, "https://a.test"))
    assert await collect(p) == "Good morning"
    assert seen["path"] == "/v1/messages"
    assert seen["body"]["system"] == "be nice"
    assert [m["role"] for m in seen["body"]["messages"]] == ["user", "assistant", "user"]


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (429, True), (500, True), (400, False)]
)
async def test_http_errors_map_to_provider_error(status: int, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text='{"error": "nope"}')

    p = OpenAICompatProvider(
        "oa", "m", "sk-secret-value-1234567890", client=client(handler, "https://api.test/v1")
    )
    with pytest.raises(ProviderError) as info:
        await collect(p)
    assert info.value.retryable is retryable
    assert "sk-secret" not in str(info.value)


async def test_connection_error_maps_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    p = OllamaProvider("local", "m", client=client(handler, "http://x"))
    with pytest.raises(ProviderError):
        await collect(p)
    assert await p.health_check() is False
