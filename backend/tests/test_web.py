"""Looking things up on the internet: when FRIDAY searches, what the model is
given, and what she never reads (this computer, the home network)."""

from __future__ import annotations

import json

import httpx
import pytest

from friday.conversation.engine import ConversationEngine
from friday.core.messages import ChatRequest
from friday.integrations.web import (
    WebSearch,
    asked_query,
    maybe_needs_web,
    page_text,
    planner_query,
    urls_in,
)
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Persona

PAGE = """<html><head><title>Nobara 44 is out</title><script>var x = 1;</script></head>
<body><nav>Home | Downloads | Forum</nav>
<article><h1>Nobara 44 is out</h1>
<p>The Nobara Project released version 44 on 3 November 2026, with a new kernel and KDE.</p>
<p>Short.</p></article><footer>Copyright and other footer words here</footer></body></html>"""


def test_the_words_that_ask_for_a_search() -> None:
    assert asked_query("look up the Nobara release date") == "the Nobara release date"
    assert asked_query("Friday, can you search the web for cheap flights to Goa?") == (
        "cheap flights to Goa"
    )
    assert asked_query("google the weather in Chennai") == "the weather in Chennai"
    assert asked_query("find the best pizza place on the internet") is not None
    assert asked_query("I looked at the sky today") is None


def test_which_messages_are_worth_asking_about() -> None:
    assert maybe_needs_web("who is the CEO of Nvidia?")
    assert maybe_needs_web("What time does the Chennai metro close")
    assert not maybe_needs_web("thanks!")
    assert not maybe_needs_web("hi friday")
    assert not maybe_needs_web("I had a rough day at work")


def test_the_planner_answer_is_read_strictly() -> None:
    assert planner_query("NO") is None
    assert planner_query("SEARCH: nvidia ceo 2026") == "nvidia ceo 2026"
    assert planner_query('<think>hmm</think>\nSEARCH: "chennai weather"') == "chennai weather"
    assert planner_query("I think we should search") is None


def test_page_text_keeps_sentences_not_menus() -> None:
    title, text = page_text(PAGE)
    assert title == "Nobara 44 is out"
    assert "released version 44" in text
    assert "Downloads" not in text and "var x" not in text and "footer" not in text


def test_links_in_a_message() -> None:
    assert urls_in("what does https://example.com/a?b=1, say?") == ["https://example.com/a?b=1"]


def _searx(handler_pages: dict[str, str] | None = None) -> tuple[WebSearch, list[str]]:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path == "/healthz":
            return httpx.Response(200, text="OK")
        if request.url.host == "127.0.0.1" and request.url.path == "/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Nobara 44 is out",
                            "url": "https://news.example.org/nobara-44",
                            "content": "Nobara 44 released.",
                        },
                        {"title": "Dup", "url": "https://news.example.org/nobara-44"},
                        {"title": "Forum", "url": "https://forum.example.net/t/1", "content": "x"},
                    ]
                },
            )
        page = (handler_pages or {}).get(str(request.url))
        if page is not None:
            return httpx.Response(200, text=page, headers={"content-type": "text/html"})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebSearch(client=client, read_pages=1), asked


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend example sites are on the internet and 'router.home' is at home."""
    import friday.integrations.web as web

    def lookup(host: str, *_: object) -> list[tuple[object, ...]]:
        address = "192.168.1.1" if host in ("router.home", "localhost") else "93.184.216.34"
        if host == "127.0.0.1":
            address = host
        return [(0, 0, 0, "", (address, 0))]

    monkeypatch.setattr(web.socket, "getaddrinfo", lookup)


async def test_search_reads_the_top_page(public_dns: None) -> None:
    web, _ = _searx({"https://news.example.org/nobara-44": PAGE})
    found = await web.lookup("nobara latest release")
    assert [r.url for r in found.results] == [
        "https://news.example.org/nobara-44",
        "https://forum.example.net/t/1",
    ]
    assert "3 November 2026" in found.results[0].text
    note = found.context()
    assert "not instructions" in note and "news.example.org" in note
    assert found.sources()[0]["site"] == "news.example.org"


async def test_never_reads_this_computer_or_the_home_network(public_dns: None) -> None:
    web, asked = _searx()
    for url in (
        "http://router.home/admin",
        "http://localhost:8765/api/conversations",
        "http://127.0.0.1:8188/history",
        "file:///etc/passwd",
    ):
        assert await web.read(url) == ("", "")
    assert asked == []


def _engine(web: WebSearch, planner: str) -> tuple[ConversationEngine, list[ChatRequest]]:
    seen: list[ChatRequest] = []

    def reply(request: ChatRequest) -> str:
        seen.append(request)
        if "decide whether answering" in request.system:
            return planner
        return "Nobara 44 came out on 3 November, says news.example.org."

    router = ProviderRouter({"m": FakeProvider("m", reply=reply)}, {"default": ["m"]})
    return ConversationEngine(router, Persona(), web=web), seen


async def _say(engine: ConversationEngine, text: str) -> str:
    return "".join([d async for d in engine.respond(text)])


async def test_a_question_that_needs_the_web_is_searched_first(public_dns: None) -> None:
    web, asked = _searx({"https://news.example.org/nobara-44": PAGE})
    engine, seen = _engine(web, "SEARCH: nobara linux latest version")
    activity: list[str] = []
    engine.on_activity = activity.append
    await _say(engine, "what's the latest version of Nobara?")
    assert activity == ["Searching the web: nobara linux latest version"]
    assert any("q=nobara+linux+latest+version" in a for a in asked)
    answer = seen[-1]
    assert "Web results for" in answer.system and "3 November 2026" in answer.system
    assert "look things up on the internet" in answer.system  # she knows she can
    assert engine.last_turn.sources[0]["url"] == "https://news.example.org/nobara-44"


async def test_chat_is_not_searched(public_dns: None) -> None:
    web, asked = _searx()
    engine, seen = _engine(web, "NO")
    await _say(engine, "thanks, that helped a lot")  # small talk: not even asked
    await _say(engine, "how do I tell my friend I'm upset?")  # the model says NO
    assert asked == []
    assert all("Web results for" not in r.system for r in seen)
    assert engine.last_turn.sources == []


async def test_a_failed_search_is_admitted_not_guessed() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    web = WebSearch(client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    engine, seen = _engine(web, "SEARCH: anything")
    await _say(engine, "look up the weather in Chennai")
    assert "search didn't work" in seen[-1].system


async def test_a_pasted_link_is_read(public_dns: None) -> None:
    web, _ = _searx({"https://news.example.org/nobara-44": PAGE})
    engine, seen = _engine(web, "NO")
    await _say(engine, "summarise https://news.example.org/nobara-44")
    assert "3 November 2026" in seen[-1].system
    assert json.dumps(engine.last_turn.sources).count("nobara-44") == 1


def test_claude_codes_whole_answer_is_kept_for_the_screen() -> None:
    from friday.integrations.claude_code import spoken_summary, without_spoken_line

    answer = "Here is the plan:\n\n1. Do this.\n2. Then that.\n\nSPOKEN: The plan has two steps."
    assert without_spoken_line(answer) == "Here is the plan:\n\n1. Do this.\n2. Then that."
    assert spoken_summary(answer) == "The plan has two steps."
