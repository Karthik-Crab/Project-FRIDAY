"""Notes kept between sessions and shared with Claude Code."""

import asyncio
from pathlib import Path

import pytest

from friday.conversation.delegation import CodeDelegate
from friday.conversation.engine import ConversationEngine
from friday.conversation.notes import NoteBook
from friday.integrations.claude_code import CodeResult
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel


def engine_with(notes: NoteBook) -> tuple[ConversationEngine, FakeProvider]:
    provider = FakeProvider("fake", reply="Sure.")
    router = ProviderRouter({"fake": provider}, {"default": ["fake"]})
    return ConversationEngine(router, channel=Channel.VOICE, notes=notes), provider


async def ask(engine: ConversationEngine, text: str) -> str:
    return "".join([d async for d in engine.respond(text)])


async def test_remember_is_stored_word_for_word_and_survives_a_restart(tmp_path: Path) -> None:
    engine, provider = engine_with(NoteBook(tmp_path))
    reply = await ask(engine, "Friday, remember that my sister's birthday is the ninth of May.")
    assert reply == "Okay, I'll remember that. my sister's birthday is the ninth of May."
    assert provider.requests == []  # fixed logic, the language model is not asked
    assert engine.is_control("Remember that I prefer short answers")

    # A new session: nothing in the conversation, but the note is in the prompt.
    engine, provider = engine_with(NoteBook(tmp_path))
    await ask(engine, "When is my sister's birthday?")
    system = provider.requests[-1].system or ""
    assert "my sister's birthday is the ninth of May." in system
    assert "never add to them from imagination" in system


@pytest.mark.parametrize(
    "said",
    [
        "Do you remember what I said?",
        "Remember that film we saw?",
        "I can't remember that.",
        "Remember when we went to Goa.",
    ],
)
def test_ordinary_sentences_are_not_notes(said: str, tmp_path: Path) -> None:
    notes = NoteBook(tmp_path)
    assert not notes.concerns(said) and notes.handle(said) is None
    assert not notes.memory_path.exists()


async def test_recall_and_forget(tmp_path: Path) -> None:
    engine, _ = engine_with(NoteBook(tmp_path))
    assert "any notes yet" in await ask(engine, "What do you remember?")
    await ask(engine, "Remember that the demo is on Friday.")
    await ask(engine, "Make a note that I take my coffee black.")
    said = await ask(engine, "Friday, what do you remember about me?")
    assert said == "Here's what I've noted. the demo is on Friday. I take my coffee black."
    assert await ask(engine, "Forget that.") == "Okay, forgotten. I take my coffee black."
    assert NoteBook(tmp_path).facts()[-1].endswith("the demo is on Friday.")
    await ask(engine, "Forget the last note.")
    assert await ask(engine, "Forget that.") == "There's no note to forget."


def test_no_notes_means_no_extra_prompt_and_long_notes_are_capped(tmp_path: Path) -> None:
    notes = NoteBook(tmp_path, max_chars=300)
    assert notes.context() is None and notes.for_agent() == ""
    for number in range(40):
        notes.remember(f"fact number {number} is something fairly long to say")
    context = notes.context() or ""
    assert "fact number 39" in context and "fact number 0 " not in context
    assert len(context) < 600


def test_files_the_owner_edits_or_points_at_are_read(tmp_path: Path) -> None:
    extra = tmp_path / "CLAUDE.md"
    extra.write_text("The project uses pnpm, never npm.", encoding="utf-8")
    notes = NoteBook(tmp_path / "memory", also_read=(extra, tmp_path / "missing.md"))
    notes.folder.mkdir()
    notes.memory_path.write_text("# Mine\n\n* Edited by hand\nnot a note\n", encoding="utf-8")
    context = notes.context() or ""
    assert "- Edited by hand" in context and "not a note" not in context
    assert "From the file CLAUDE.md:\nThe project uses pnpm, never npm." in context


class Agent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def run(self, task: str, resume: str | None = None) -> CodeResult:
        self.calls.append((task, resume))
        return CodeResult(text="Done.\nSPOKEN: The toggle is in.", session_id="abcd1234", ok=True)


async def send(delegate: CodeDelegate, task: str) -> None:
    delegate.handle(f"Ask Claude Code to {task}")
    delegate.handle("yes")
    while True:
        if not delegate.busy:
            return
        await asyncio.sleep(0.01)


async def test_claude_code_gets_the_notes_once_and_its_work_is_logged(tmp_path: Path) -> None:
    notes = NoteBook(tmp_path)
    notes.remember("I prefer TypeScript over JavaScript")
    agent = Agent()
    delegate = CodeDelegate(agent, notes=notes)

    await send(delegate, "add a toggle")
    first, resume = agent.calls[0]
    assert first.startswith("add a toggle") and resume is None
    assert "I prefer TypeScript over JavaScript" in first and "not part of the task" in first

    # The same chat is not told again, until a new note is added.
    await send(delegate, "add tests")
    assert agent.calls[1] == ("add tests", "abcd1234")
    notes.remember("the app is called Tally")
    await send(delegate, "rename the title")
    assert "the app is called Tally" in agent.calls[2][0]
    assert "add a toggle" not in agent.calls[2][0]  # it knows its own earlier work

    # A fresh chat is told what was done before.
    delegate.handle("New Claude Code chat.")
    await send(delegate, "tidy up")
    assert '"add tests" -> The toggle is in.' in agent.calls[3][0]

    # Next session: the voice model knows what was done.
    context = NoteBook(tmp_path).context() or ""
    assert '"add a toggle" -> The toggle is in.' in context


async def test_notes_can_be_kept_from_claude_code(tmp_path: Path) -> None:
    notes = NoteBook(tmp_path, share=False)
    notes.remember("something private")
    agent = Agent()
    await send(CodeDelegate(agent, notes=notes), "add a toggle")
    assert agent.calls[0][0] == "add a toggle"
    assert notes.work()  # still logged for FRIDAY herself
