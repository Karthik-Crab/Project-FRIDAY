"""Passing spoken tasks to Claude Code. The real program is never started here."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from friday.conversation.delegation import CodeDelegate, Notice, parse_task
from friday.conversation.engine import ConversationEngine
from friday.core.messages import Role
from friday.integrations.claude_code import (
    RELAY_NOTE,
    ClaudeCodeCLI,
    CodeAgentError,
    CodeResult,
    parse_result,
    spoken_summary,
)
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel
from friday.voice import session as vs
from friday.voice.fakes import (
    FakeAudioSink,
    FakeTTS,
    ScriptedAudioSource,
    ScriptedSTT,
    Silence,
    Speak,
    WaitFor,
)
from friday.voice.vad import EnergyVAD


class FakeAgent:
    def __init__(self, reply: str = "Added it.\nSPOKEN: The toggle is in.", delay: float = 0.0):
        self.calls: list[tuple[str, str | None]] = []
        self.reply = reply
        self.delay = delay

    async def run(self, task: str, resume: str | None = None) -> CodeResult:
        self.calls.append((task, resume))
        await asyncio.sleep(self.delay)
        return CodeResult(text=self.reply, session_id="abcd1234-0000", ok=True)


def setup(agent: FakeAgent, tmp_path: Path | None = None):  # type: ignore[no-untyped-def]
    notices: list[Notice] = []
    delegate = CodeDelegate(agent, log_dir=tmp_path, on_done=notices.append)
    provider = FakeProvider("fake", reply="Sure.")
    engine = ConversationEngine(
        ProviderRouter({"fake": provider}, {"default": ["fake"]}),
        channel=Channel.VOICE,
        delegate=delegate,
    )
    return delegate, engine, provider, notices


async def until(done) -> None:  # type: ignore[no-untyped-def]
    while True:
        if done():
            return
        await asyncio.sleep(0.01)


async def ask(engine: ConversationEngine, text: str) -> str:
    return "".join([d async for d in engine.respond(text)])


@pytest.mark.parametrize(
    ("said", "task"),
    [
        ("Ask Claude Code to add a dark mode toggle.", "add a dark mode toggle."),
        ("Friday, tell cloud code to fix the login bug", "fix the login bug"),
        ("Hey Friday, can you ask Claude to run the tests?", "run the tests?"),
        ("Pass this on to clod code: rename the module", "rename the module"),
        ("ask Claude Code, why is the build slow", "why is the build slow"),
    ],
)
def test_trigger_phrases_survive_speech_recognition(said: str, task: str) -> None:
    assert parse_task(said) == task


@pytest.mark.parametrize(
    "said",
    ["I love Claude Code.", "What is Claude Code?", "Ask me anything.", "The cloud is grey."],
)
def test_ordinary_sentences_are_not_tasks(said: str) -> None:
    assert parse_task(said) is None


async def test_task_is_read_back_and_only_sent_after_yes(tmp_path: Path) -> None:
    agent = FakeAgent()
    delegate, engine, provider, notices = setup(agent, tmp_path)

    reply = await ask(engine, "Ask Claude Code to add a dark mode toggle.")
    assert reply == "For Claude Code: add a dark mode toggle. Shall I send it?"
    assert agent.calls == []  # nothing runs before the confirmation

    assert (await ask(engine, "Yes.")).startswith("Sent.")
    await until(lambda: bool(notices))
    assert agent.calls == [("add a dark mode toggle.", None)]
    assert notices[0].spoken == "Claude Code is done. The toggle is in."
    assert notices[0].log_path is not None
    assert "add a dark mode toggle." in notices[0].log_path.read_text(encoding="utf-8")
    # the language model was never involved, and none of it is chat history
    assert provider.requests == []
    assert engine.window.messages == ()

    # a follow-up continues the same Claude Code session
    await ask(engine, "Tell Claude Code to also add tests.")
    await ask(engine, "Go ahead.")
    await until(lambda: len(notices) >= 2)
    assert agent.calls[1] == ("also add tests.", "abcd1234-0000")


async def test_no_drops_it_and_more_words_extend_it() -> None:
    agent = FakeAgent()
    delegate, engine, provider, notices = setup(agent)

    await ask(engine, "Ask Claude Code to add a dark mode toggle")
    reply = await ask(engine, "to the settings page.")
    assert reply == "For Claude Code: add a dark mode toggle to the settings page. Shall I send it?"
    assert await ask(engine, "I see.") == "Say yes to send it, or no to drop it."
    assert await ask(engine, "No.") == "Okay, I won't send it."
    assert delegate.pending is None and agent.calls == []
    assert await ask(engine, "Yes.") == "Sure."  # an ordinary turn again


async def test_busy_cancel_and_fresh_session() -> None:
    agent = FakeAgent(delay=5)
    delegate, engine, provider, notices = setup(agent)
    await ask(engine, "Ask Claude Code to refactor everything.")
    await ask(engine, "Yes.")
    await asyncio.sleep(0.01)
    assert "still working" in await ask(engine, "Ask Claude Code to do more.")
    assert await ask(engine, "Cancel Claude Code.") == "Okay, I've stopped Claude Code."
    await asyncio.sleep(0.01)
    assert not delegate.busy and notices == []
    delegate.session_id = "abcd1234-0000"
    assert "fresh" in await ask(engine, "New Claude Code session.")
    assert delegate.session_id is None


async def test_agent_failure_is_reported_not_raised() -> None:
    class Broken:
        async def run(self, task: str, resume: str | None = None) -> CodeResult:
            raise CodeAgentError("'claude' is not installed or not on the PATH")

    delegate, engine, provider, notices = setup(Broken())  # type: ignore[arg-type]
    await ask(engine, "Ask Claude Code to do something.")
    await ask(engine, "Yes.")
    await until(lambda: bool(notices))
    assert not notices[0].ok
    assert notices[0].spoken.startswith("Claude Code ran into a problem. 'claude' is not installed")


def test_result_parsing_and_spoken_line() -> None:
    out = json.dumps(
        {
            "result": "Done.\n\n- changed a.py\n\nSPOKEN: I changed one file.",
            "session_id": "b38a34c7-d19b-4dff-97e7-33aeb7bf4c35",
            "is_error": False,
            "permission_denials": [{"tool_name": "Bash"}],
        }
    )
    result = parse_result(out, 0)
    assert result.ok and result.denied == 1
    assert result.session_id == "b38a34c7-d19b-4dff-97e7-33aeb7bf4c35"
    assert spoken_summary(result.text) == "I changed one file."
    assert spoken_summary("word " * 200).endswith("the rest is in the log.")
    assert (
        parse_result(json.dumps({"result": "x", "session_id": "$(rm -rf)"}), 0).session_id is None
    )
    with pytest.raises(CodeAgentError):
        parse_result("not json", 1, "Not logged in")


def test_what_the_user_said_never_reaches_the_command_line(tmp_path: Path) -> None:
    cli = ClaudeCodeCLI(tmp_path, permission_mode="plan")
    assert cli.arguments() == ["-p", "--output-format", "json", "--permission-mode", "plan"]
    assert cli.arguments("abcd1234-0000")[-2:] == ["--resume", "abcd1234-0000"]
    assert "--resume" not in cli.arguments("x; rm -rf /")


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX shell script as the program")
async def test_cli_sends_the_task_on_stdin_in_the_project_folder(tmp_path: Path) -> None:
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        'cat > "$PWD/stdin.txt"\n'
        'echo "$@" > "$PWD/args.txt"\n'
        'printf \'{"result": "ok\\\\nSPOKEN: fine", "session_id": "abcd1234-0000"}\'\n'
    )
    script.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    result = await ClaudeCodeCLI(project, command=str(script)).run('say "hi" && echo $HOME')
    assert result.ok and spoken_summary(result.text) == "fine"
    assert (project / "stdin.txt").read_text() == 'say "hi" && echo $HOME' + RELAY_NOTE
    assert (project / "args.txt").read_text().strip() == "-p --output-format json"

    with pytest.raises(CodeAgentError):
        await ClaudeCodeCLI(project, command="no-such-program-xyz").run("x")


async def test_voice_end_to_end_confirm_send_and_announce_result() -> None:
    agent = FakeAgent(delay=0.3)
    delegate, engine, provider, notices = setup(agent)
    events: list[object] = []
    said = lambda: [e.text for e in events if isinstance(e, vs.AssistantSaid)]  # noqa: E731
    turns = lambda: [e for e in events if isinstance(e, vs.TurnFinished)]  # noqa: E731
    session = vs.VoiceSession(
        engine,
        ScriptedAudioSource(
            [
                Silence(300),
                Speak(900),
                Silence(900),
                WaitFor(lambda: len(turns()) >= 1, timeout_s=10),
                Speak(400),
                Silence(900),
                WaitFor(lambda: len(turns()) >= 3, timeout_s=10),  # "Sent", then the result
            ]
        ),
        EnergyVAD(),
        ScriptedSTT(["Ask Claude Code to add a dark mode toggle.", "Yes."]),
        FakeTTS(),
        FakeAudioSink(speed=20.0),
        vs.VoiceConfig(acknowledgements=("Hmm.",)),
        on_event=events.append,
    )
    delegate.on_done = lambda n: session.announce(lambda: engine.say(n.spoken, f"(task: {n.task})"))
    await asyncio.wait_for(session.run(), 30)

    assert " ".join(said()) == (
        "For Claude Code: add a dark mode toggle. Shall I send it? "
        "Sent. I'll tell you when Claude Code is done. "
        "Claude Code is done. The toggle is in."
    )
    assert all(t.metrics.ack_ms is None for t in turns())  # no "Hmm." before fixed replies
    assert provider.requests == []
    # the result is in history, so "what did it say?" can be answered
    assert [m.role for m in engine.window.messages] == [Role.USER, Role.ASSISTANT]
    assert engine.window.messages[1].content == "Claude Code is done. The toggle is in."


async def test_yes_while_the_task_is_still_being_read_back_counts() -> None:
    agent = FakeAgent()
    delegate, engine, provider, notices = setup(agent)
    events: list[object] = []
    sink = FakeAudioSink(speed=1.0)
    session = vs.VoiceSession(
        engine,
        ScriptedAudioSource(
            [
                Silence(300),
                Speak(900),
                Silence(900),
                WaitFor(lambda: sink.is_playing, timeout_s=10),
                Speak(500),  # "Yes." over the read-back
                Silence(900),
                WaitFor(lambda: bool(agent.calls), timeout_s=10),
                Silence(500),
            ]
        ),
        EnergyVAD(),
        ScriptedSTT(["Ask Claude Code to add a dark mode toggle to the settings page.", "Yes."]),
        FakeTTS(),
        sink,
        on_event=events.append,
    )
    await asyncio.wait_for(session.run(), 30)
    assert agent.calls == [("add a dark mode toggle to the settings page.", None)]


async def test_one_chat_lasts_the_day_across_restarts(tmp_path: Path) -> None:
    state = tmp_path / "session.json"

    def start(keep: str = "day", scope: str = "proj") -> tuple[CodeDelegate, FakeAgent]:
        agent = FakeAgent()
        return CodeDelegate(agent, state_path=state, keep_session=keep, scope=scope), agent

    first, agent = start()
    first.handle("Ask Claude Code to add a toggle.")
    first.handle("yes")
    await until(lambda: not first.busy)
    assert agent.calls == [("add a toggle.", None)]

    # FRIDAY is closed and started again: the next task continues the same chat.
    second, agent = start()
    second.handle("Ask Claude Code to add tests for it.")
    second.handle("yes")
    await until(lambda: not second.busy)
    assert agent.calls == [("add tests for it.", "abcd1234-0000")]

    assert start(scope="another-folder")[0].session_id is None
    assert start(keep="run")[0].session_id is None

    # Yesterday's chat is not continued, unless the owner asked to keep it always.
    saved = json.loads(state.read_text())
    state.write_text(json.dumps(saved | {"day": "2000-01-01"}))
    assert start()[0].session_id is None
    assert start(keep="always")[0].session_id == "abcd1234-0000"

    third, _ = start(keep="always")
    assert "fresh" in (third.handle("Friday, new Claude Code chat.") or "")
    assert not state.exists() and start(keep="always")[0].session_id is None


async def test_a_chat_that_no_longer_exists_is_forgotten(tmp_path: Path) -> None:
    class Gone(FakeAgent):
        async def run(self, task: str, resume: str | None = None) -> CodeResult:
            return CodeResult(text="No conversation found with session ID: x", ok=False)

    state = tmp_path / "session.json"
    state.write_text(json.dumps({"session_id": "abcd1234-0000", "day": "x", "scope": ""}))
    delegate = CodeDelegate(Gone(), state_path=state, keep_session="always")
    assert delegate.session_id == "abcd1234-0000"
    delegate.handle("Ask Claude Code to carry on.")
    delegate.handle("yes")
    await until(lambda: not delegate.busy)
    assert delegate.session_id is None and not state.exists()


async def test_saying_the_command_again_replaces_the_waiting_task() -> None:
    agent = FakeAgent()
    delegate, engine, provider, notices = setup(agent)

    await ask(engine, "Ask Claude Code to list on the files. I am a project.")
    # misheard "ask" as "as": still the command again, not more words for the task
    reply = await ask(engine, "Now, as Claude Code list all the files in my project.")
    assert reply == "For Claude Code: list all the files in my project. Shall I send it?"
    # without a waiting task the loose wording is ordinary conversation
    delegate.pending = None
    assert await ask(engine, "As Claude Code said, it is fine.") == "Sure."
