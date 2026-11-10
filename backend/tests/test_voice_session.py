"""End-to-end voice session tests with the real VAD/segmenter/chunker/engine.

Only the microphone, Whisper, Kokoro and the speaker are faked. These are the
automated acceptance tests for Phase 1's interaction requirements.
"""

import asyncio

from friday.conversation.engine import ConversationEngine
from friday.conversation.memory import INTERRUPTED_MARKER
from friday.core.messages import Role
from friday.llm.providers.fake import FakeProvider
from friday.llm.router import ProviderRouter
from friday.persona.persona import Channel, Mode
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

LONG_REPLY = (
    "Octopuses are remarkable animals. They have three hearts and blue blood. "
    "Each arm has its own cluster of neurons, so the arms can taste and react on their own. "
    "They can also change colour and texture in a fraction of a second."
)


def long_then_short(request):  # type: ignore[no-untyped-def]
    """Long answer to the first question, short answers afterwards."""
    return LONG_REPLY if len(request.messages) == 1 else "Yes, three hearts."


def build(steps, transcripts, reply=LONG_REPLY, sink_speed=20.0, llm_delay=0.001, config=None):  # type: ignore[no-untyped-def]
    provider = FakeProvider("fake", reply=reply, delay=llm_delay)
    engine = ConversationEngine(
        ProviderRouter({"fake": provider}, {"default": ["fake"]}), channel=Channel.VOICE
    )
    sink = FakeAudioSink(speed=sink_speed)
    tts = FakeTTS()
    events: list[object] = []
    session = vs.VoiceSession(
        engine,
        ScriptedAudioSource(steps),
        EnergyVAD(),
        ScriptedSTT(transcripts),
        tts,
        sink,
        config,
        on_event=events.append,
    )
    return session, engine, provider, sink, tts, events


def of(events, kind):  # type: ignore[no-untyped-def]
    return [e for e in events if isinstance(e, kind)]


async def test_listen_understand_respond() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: not session._turn_active(), timeout_s=10),
        ],
        ["Tell me about octopuses"],
    )
    await asyncio.wait_for(session.run(), 15)
    assert of(events, vs.UserSaid)[0].text == "Tell me about octopuses"
    spoken = " ".join(e.text for e in of(events, vs.AssistantSaid))
    assert spoken.split() == LONG_REPLY.split()
    assert len(tts.spoken) >= 3  # streamed sentence by sentence
    finished = of(events, vs.TurnFinished)
    assert len(finished) == 1 and not finished[0].interrupted
    m = finished[0].metrics
    assert m.first_token_ms is not None and m.first_audio_ms is not None
    assert m.first_audio_ms >= m.first_token_ms
    assert m.provider == "fake"
    states = [e.state for e in of(events, vs.StateChanged)]
    assert states[:3] == [
        vs.VoiceState.USER_SPEAKING,
        vs.VoiceState.THINKING,
        vs.VoiceState.SPEAKING,
    ]
    assert states[-1] is vs.VoiceState.LISTENING


async def test_barge_in_stops_speech_quickly_and_history_reflects_what_was_heard() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(600),
            Silence(900),
            WaitFor(lambda: len(of(events, vs.TurnFinished)) >= 2, timeout_s=10),
        ],
        ["Tell me about octopuses", "Wait, three hearts?"],
        reply=long_then_short,
        sink_speed=1.0,  # real-time playback so the reply is long enough to interrupt
    )
    await asyncio.wait_for(session.run(), 20)

    interrupted = of(events, vs.Interrupted)
    assert len(interrupted) == 1 and interrupted[0].reason == "barge_in"
    heard = interrupted[0].heard
    assert len(heard) < len(LONG_REPLY)
    assert sink.played[0] < 1.0  # first chunk was cut off mid-playback

    # The second user turn was answered.
    finished = of(events, vs.TurnFinished)
    assert [f.interrupted for f in finished] == [True, False]

    # History: assistant turn rewritten to what was actually heard.
    msgs = engine.window.messages
    assert [m.role for m in msgs] == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    assert msgs[1].content.endswith(INTERRUPTED_MARKER)
    assert msgs[1].content == (heard or "(nothing heard)") + INTERRUPTED_MARKER
    assert msgs[2].content == "Wait, three hearts?"
    # And the model was told about it on the next request.
    assert INTERRUPTED_MARKER in provider.requests[-1].messages[1].content


async def test_barge_in_latency_is_bounded_by_threshold() -> None:
    """Playback must stop within ~barge_in_min_ms of audio after the user starts talking.

    Measured in audio frames (not wall-clock), so it is deterministic.
    """
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(1500),
            Silence(900),
            WaitFor(lambda: len(of(events, vs.TurnFinished)) >= 2, timeout_s=10),
        ],
        ["Tell me about octopuses", "Never mind that, what time is it?"],
        reply=long_then_short,
        sink_speed=1.0,
    )
    probe = _BargeInProbe(session.source, sink)
    session.source = probe  # type: ignore[assignment]
    await asyncio.wait_for(session.run(), 20)
    assert of(events, vs.Interrupted)
    assert probe.frames_until_stop is not None
    budget_frames = session.config.barge_in_min_ms / 20 + 2  # threshold + scheduling slack
    assert probe.frames_until_stop <= budget_frames


class _BargeInProbe:
    """Counts speech frames delivered after onset-during-playback until playback stops."""

    def __init__(self, inner, sink) -> None:  # type: ignore[no-untyped-def]
        self.inner, self.sink = inner, sink
        self.frames_until_stop: int | None = None

    async def frames(self):  # type: ignore[no-untyped-def]
        from friday.voice.vad import frame_dbfs

        counting = False
        count = 0
        async for frame in self.inner.frames():
            loud = frame_dbfs(frame.pcm) > -30
            if not counting and loud and self.sink.is_playing:
                counting = True
            if counting and self.frames_until_stop is None:
                if not self.sink.is_playing:
                    self.frames_until_stop = count
                elif loud:
                    count += 1
            yield frame


async def test_stop_command_silences_without_new_reply() -> None:
    # A short "stop" (under the barge-in threshold) is recognised by transcript.
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(300),
            Silence(900),
            WaitFor(lambda: not session._turn_active(), timeout_s=10),
            Silence(200),
        ],
        ["Tell me about octopuses", "Stop."],
        sink_speed=1.0,
    )
    await asyncio.wait_for(session.run(), 20)
    assert [i.reason for i in of(events, vs.Interrupted)] == ["stop"]
    assert len(provider.requests) == 1  # no reply to "stop"


async def test_backchannel_does_not_interrupt() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(300),
            Silence(900),
            WaitFor(lambda: not session._turn_active(), timeout_s=30),
        ],
        ["Tell me about octopuses", "mm-hmm"],
        sink_speed=5.0,
    )
    await asyncio.wait_for(session.run(), 30)
    assert of(events, vs.Interrupted) == []
    assert [f.interrupted for f in of(events, vs.TurnFinished)] == [False]


async def test_voice_mode_switch() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: not session._turn_active() and bool(of(events, vs.TurnFinished))),
        ],
        ["Friday, work mode."],
    )
    await asyncio.wait_for(session.run(), 10)
    assert engine.mode is Mode.WORK
    assert "Work mode" in " ".join(e.text for e in of(events, vs.AssistantSaid))
    assert provider.requests == []


async def test_stt_failure_is_reported_and_session_survives() -> None:
    from friday.core.errors import VoiceError

    class BrokenSTT:
        calls = 0

        async def transcribe(self, pcm: bytes, sample_rate: int):  # type: ignore[no-untyped-def]
            BrokenSTT.calls += 1
            if BrokenSTT.calls == 1:
                raise VoiceError("model crashed")
            from friday.voice.base import Transcript

            return Transcript("hello again")

    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(600),
            Silence(900),
            Speak(600),
            Silence(900),
            WaitFor(lambda: bool(of(events, vs.TurnFinished)), timeout_s=10),
        ],
        [],
        reply="Hi there.",
    )
    session.stt = BrokenSTT()
    await asyncio.wait_for(session.run(), 10)
    assert of(events, vs.VoiceErrorEvent)
    assert of(events, vs.UserSaid)[0].text == "hello again"


async def test_noise_pauses_then_answer_resumes_in_full() -> None:
    """A long noise (empty transcript) must not kill the answer."""
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(700),  # long enough to trigger a hold
            Silence(900),
            WaitFor(lambda: bool(of(events, vs.TurnFinished)), timeout_s=30),
        ],
        ["Tell me about octopuses", ""],
        sink_speed=5.0,
    )
    await asyncio.wait_for(session.run(), 30)
    assert of(events, vs.Interrupted) == []
    assert [f.interrupted for f in of(events, vs.TurnFinished)] == [False]
    assert sink.played[0] < 1.0  # it really was paused mid-sentence
    spoken = " ".join(e.text for e in of(events, vs.AssistantSaid))
    assert spoken.split() == LONG_REPLY.split()  # nothing lost, nothing duplicated
    assert not engine.window.messages[-1].content.endswith(INTERRUPTED_MARKER)


async def test_long_backchannel_pauses_then_resumes() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(700),
            Silence(900),
            WaitFor(lambda: bool(of(events, vs.TurnFinished)), timeout_s=30),
        ],
        ["Tell me about octopuses", "Yeah, okay."],
        sink_speed=5.0,
    )
    await asyncio.wait_for(session.run(), 30)
    assert of(events, vs.Interrupted) == []
    assert len(provider.requests) == 1


async def test_stop_works_even_if_the_sink_swallows_cancellation() -> None:
    """Regression: asyncio.wait_for before Python 3.12 can swallow a cancel."""

    class SwallowingSink(FakeAudioSink):
        async def play(self, chunk):  # type: ignore[no-untyped-def]
            try:
                return await super().play(chunk)
            except asyncio.CancelledError:
                return 0.0

    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: sink.is_playing),
            Speak(300),
            Silence(900),
            WaitFor(lambda: not session._turn_active(), timeout_s=5),
        ],
        ["Tell me about octopuses", "Stop."],
        sink_speed=1.0,
    )
    swallowing = SwallowingSink(speed=1.0)
    session.sink = sink = swallowing  # the lambdas above read `sink`
    await asyncio.wait_for(session.run(), 15)
    assert [i.reason for i in of(events, vs.Interrupted)] == ["stop"]


# ---- found on the first hardware run (docs/reports) -------------------------


def done(events):  # type: ignore[no-untyped-def]
    return [e for e in of(events, vs.TurnFinished) if not e.interrupted]


async def test_speaking_again_before_any_audio_merges_into_one_turn() -> None:
    """A pause mid-sentence must not produce a reply to half a thought."""
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),  # endpoint: a turn starts for the first half
            Speak(600),  # ...but the user carries on before anything is said
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
        ],
        ["It was really good, this is", "my first trial run with you."],
        reply="Glad to hear that.",
        llm_delay=0.2,  # slow first sentence, like a local model on a CPU
    )
    await asyncio.wait_for(session.run(), 20)

    assert [e.reason for e in of(events, vs.Interrupted)] == ["merge"]
    assert len(done(events)) == 1
    final = provider.requests[-1].messages
    assert [m.role for m in final] == [Role.USER]
    assert final[0].content == "It was really good, this is my first trial run with you."
    history = engine.window.messages
    assert [m.role for m in history] == [Role.USER, Role.ASSISTANT]
    assert history[1].content == "Glad to hear that."
    assert tts.spoken == ["Glad to hear that."]


async def test_noise_before_any_audio_still_gets_the_question_answered() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            Speak(600),  # a cough while FRIDAY is still thinking
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
        ],
        ["What time is it?", ""],
        reply="About half past nine.",
        llm_delay=0.2,
    )
    await asyncio.wait_for(session.run(), 20)

    assert len(done(events)) == 1
    assert [m.content for m in provider.requests[-1].messages] == ["What time is it?"]
    assert [m.role for m in engine.window.messages] == [Role.USER, Role.ASSISTANT]
    assert of(events, vs.VoiceErrorEvent) == []


async def test_filler_into_silence_is_not_answered() -> None:
    session, engine, provider, sink, tts, events = build(
        [Silence(300), Speak(500), Silence(1200)],
        ["Hmm."],
    )
    await asyncio.wait_for(session.run(), 10)
    assert provider.requests == []
    assert of(events, vs.TurnFinished) == []
    assert session.state is vs.VoiceState.LISTENING


async def test_yes_into_silence_is_an_answer_not_a_filler() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(500),
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
        ],
        ["Yes."],
        reply="Great, let's do it.",
    )
    await asyncio.wait_for(session.run(), 10)
    assert len(provider.requests) == 1


# ---- found on the second hardware run ---------------------------------------


async def test_okay_before_any_audio_does_not_restart_the_reply() -> None:
    """A filler while FRIDAY is still thinking must not throw the turn away."""
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            Speak(500),  # "Okay." before anything was said
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
        ],
        ["How do I find out how to do this?", "Okay."],
        reply="Start with the manual.",
        llm_delay=0.2,
    )
    await asyncio.wait_for(session.run(), 20)

    assert of(events, vs.Interrupted) == []
    assert len(provider.requests) == 1
    assert tts.spoken == ["Start with the manual."]


async def test_merging_is_capped_so_background_voices_cannot_silence_friday() -> None:
    steps: list[object] = [Silence(300), Speak(800), Silence(900)]
    for _ in range(4):  # other people keep talking in the room
        steps += [Speak(600), Silence(900)]
    steps.append(WaitFor(lambda: len(done(events)) >= 1, timeout_s=10))
    session, engine, provider, sink, tts, events = build(
        steps,
        ["What should I cook tonight?", "Something quick.", "With rice.", "Blah one.", "Blah two."],
        reply="Fried rice, then.",
        llm_delay=0.4,
    )
    await asyncio.wait_for(session.run(), 30)

    assert [e.reason for e in of(events, vs.Interrupted)] == ["merge", "merge"]
    assert len(done(events)) == 1
    asked = provider.requests[-1].messages[-1].content
    assert asked == "What should I cook tonight? Something quick. With rice."
    assert " ".join(tts.spoken) == "Fried rice, then."


async def test_acknowledgement_plays_first_and_is_not_repeated_by_the_reply() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(800),
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
        ],
        ["Tell me something about octopuses."],
        reply="Hmm, good question. They have three hearts.",
        config=vs.VoiceConfig(acknowledgements=("Hmm.", "Okay.")),
    )
    await asyncio.wait_for(session.run(), 20)

    # both clips are prepared up front; the reply does not say "Hmm" again
    assert tts.spoken == ["Hmm.", "Okay.", "Good question.", "They have three hearts."]
    assert len(sink.played) == 3  # the clip, then two sentences
    metrics = done(events)[0].metrics
    assert metrics.ack_ms is not None and metrics.ack_ms <= (metrics.first_audio_ms or 0)
    # the sound is not part of the conversation
    assert engine.window.messages[1].content == "Hmm, good question. They have three hearts."


async def test_no_acknowledgement_for_short_answers_commands_or_work_mode() -> None:
    session, engine, provider, sink, tts, events = build(
        [
            Silence(300),
            Speak(500),
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 1, timeout_s=10),
            Speak(700),
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 2, timeout_s=10),
            Speak(900),
            Silence(900),
            WaitFor(lambda: len(done(events)) >= 3, timeout_s=10),
        ],
        ["Yes please.", "Friday, switch to work mode.", "What is the status of the build?"],
        reply="Done.",
        config=vs.VoiceConfig(acknowledgements=("Hmm.",)),
    )
    await asyncio.wait_for(session.run(), 20)

    assert engine.mode is Mode.WORK
    assert [e.metrics.ack_ms for e in done(events)] == [None, None, None]
