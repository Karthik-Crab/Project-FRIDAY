import asyncio
from typing import Any, ClassVar

import numpy as np
import pytest

from friday.voice.adapters.faster_whisper_stt import FasterWhisperSTT, pcm16_to_float32
from friday.voice.adapters.kokoro_tts import KokoroTTS, float_to_pcm16
from friday.voice.base import AudioFrame
from friday.voice.chunker import SentenceChunker
from friday.voice.control import ControlIntent, classify, is_filler
from friday.voice.fakes import FRAME_MS, SAMPLE_RATE, noise, tone
from friday.voice.sanitize import SpeechSanitizer
from friday.voice.segmenter import (
    SegmenterConfig,
    SpeechStarted,
    UtteranceDiscarded,
    UtteranceEnded,
    UtteranceSegmenter,
)
from friday.voice.vad import EnergyVAD


def frames(pcm: bytes) -> list[AudioFrame]:
    step = SAMPLE_RATE * FRAME_MS // 1000 * 2
    return [AudioFrame(pcm[i : i + step], SAMPLE_RATE) for i in range(0, len(pcm), step)]


# ---- VAD -------------------------------------------------------------------


def test_energy_vad_separates_speech_from_noise() -> None:
    vad = EnergyVAD()
    quiet = [vad.speech_probability(f) for f in frames(noise(500))]
    loud = [vad.speech_probability(f) for f in frames(tone(200))]
    assert max(quiet) < 0.5
    assert min(loud) > 0.9


def test_energy_vad_adapts_to_louder_background() -> None:
    vad = EnergyVAD()
    for f in frames(noise(3000, amplitude=400)):  # fan noise
        last = vad.speech_probability(f)
    assert last < 0.5
    assert vad.noise_floor_db > -50


# ---- segmenter -------------------------------------------------------------


def run_segmenter(pattern: list[tuple[bool, int]], cfg: SegmenterConfig | None = None):  # type: ignore[no-untyped-def]
    seg = UtteranceSegmenter(cfg)
    events = []
    for is_speech, ms in pattern:
        pcm = tone(ms) if is_speech else noise(ms)
        for f in frames(pcm):
            events += seg.process(f, is_speech)
    return events


def test_segmenter_emits_utterance_with_preroll() -> None:
    events = run_segmenter([(False, 400), (True, 600), (False, 800)])
    assert isinstance(events[0], SpeechStarted)
    end = events[1]
    assert isinstance(end, UtteranceEnded)
    assert end.speech_ms == 600
    audio_ms = len(end.pcm) / 2 / SAMPLE_RATE * 1000
    # 300 ms pre-roll + 600 ms speech + 700 ms trailing silence
    assert 1580 <= audio_ms <= 1620


def test_short_pause_does_not_end_utterance() -> None:
    events = run_segmenter([(True, 500), (False, 400), (True, 500), (False, 800)])
    ends = [e for e in events if isinstance(e, UtteranceEnded)]
    assert len(ends) == 1 and ends[0].speech_ms == 1000


def test_clicks_are_ignored_and_short_blips_discarded() -> None:
    assert run_segmenter([(True, 60), (False, 500)]) == []  # below start_ms
    events = run_segmenter([(True, 160), (False, 800)])
    assert isinstance(events[-1], UtteranceDiscarded)


# ---- chunker ---------------------------------------------------------------


def test_chunker_first_chunk_early_then_sentences() -> None:
    c = SentenceChunker()
    out: list[str] = []
    text = (
        "The thing about octopuses, which most people never realise, is how clever they are. "
        "Octopuses have three hearts. Dr. Smith agrees with that view entirely. Bye"
    )
    for token in text.split(" "):
        out += c.feed(token + " ")
    out += c.flush()
    # whole sentences: a comma is not a reason to stop and start again
    assert out[0] == (
        "The thing about octopuses, which most people never realise, is how clever they are."
    )
    assert not any(chunk.endswith("Dr.") for chunk in out)  # abbreviation not split
    assert out[-1] == "Bye"
    assert " ".join(out).split() == text.split()


def test_chunker_hard_splits_runaway_text() -> None:
    c = SentenceChunker(max_chars=50)
    out = c.feed("word " * 40)
    assert out and all(len(x) <= 50 for x in out)


# ---- sanitizer -------------------------------------------------------------


def test_sanitizer_strips_markdown_and_code() -> None:
    s = SpeechSanitizer()
    cleaned = s.clean("## Plan\n- **First**, open [the docs](https://x.y)")
    assert cleaned == "Plan First, open the docs"
    assert s.clean("Run `pytest` now 🙂") == "Run pytest now"
    out = s.clean("Here you go: ```python\nprint('x')")
    assert "print" not in out and "code on screen" in out
    assert s.clean("more_code()") == ""  # still inside the fence
    assert s.clean("``` That should work.") == "That should work."


# ---- control intents -------------------------------------------------------


def test_control_intents() -> None:
    assert classify("Stop.") is ControlIntent.STOP
    assert classify("Friday, hold on a second") is ControlIntent.STOP
    assert classify("mm-hmm") is ControlIntent.BACKCHANNEL
    assert classify("Yeah, okay") is ControlIntent.BACKCHANNEL
    assert classify("stop the build server") is None
    assert classify("yeah but what about tests?") is None


def test_fillers_are_distinguished_from_answers() -> None:
    for text in ("Hmm.", "mm", "Mm-hmm", "uh", "Um...", "Oh."):
        assert is_filler(text), text
    for text in ("Yes.", "Okay", "Hmm, what about lunch?", "No"):
        assert not is_filler(text), text
    assert classify("Hmm.") is ControlIntent.BACKCHANNEL


# ---- adapter format conversion (models stubbed) -----------------------------


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubWhisper:
    def __init__(self) -> None:
        self.audio = None

    def transcribe(self, audio, **kwargs):  # type: ignore[no-untyped-def]
        self.audio = audio
        self.kwargs = kwargs
        return iter([_Seg(" Hello"), _Seg(" world. ")]), type("I", (), {"language": "en"})()


async def test_whisper_adapter_converts_and_joins() -> None:
    stub = _StubWhisper()
    stt = FasterWhisperSTT(model=stub)
    result = await stt.transcribe(tone(100), 16_000)
    assert result.text == "Hello world."
    assert stub.audio is not None and stub.audio.dtype == np.float32
    assert np.abs(stub.audio).max() <= 1.0


async def test_kokoro_adapter_converts_to_pcm16() -> None:
    class Engine:
        def create(self, text, voice, speed, lang):  # type: ignore[no-untyped-def]
            return np.array([0.0, 0.5, -1.5], dtype=np.float32), 24_000

    tts = KokoroTTS("m", "v", engine=Engine())
    chunk = await tts.synthesize("hi")
    assert chunk.sample_rate == 24_000
    assert np.frombuffer(chunk.pcm, "<i2").tolist() == [0, 16383, -32767]


def test_pcm_roundtrip_helpers() -> None:
    pcm = float_to_pcm16([0.25])
    assert abs(pcm16_to_float32(pcm)[0] - 0.25) < 1e-3


async def test_whisper_adapter_drops_noise_but_never_part_of_a_sentence() -> None:
    def seg(text: str, no_speech: float, logprob: float):  # type: ignore[no-untyped-def]
        return type("S", (), {"text": text, "no_speech_prob": no_speech, "avg_logprob": logprob})()

    class Scored:
        def __init__(self, *segments: object) -> None:
            self.segments = segments

        def transcribe(self, audio, **kwargs):  # type: ignore[no-untyped-def]
            return iter(self.segments), type("I", (), {"language": "en"})()

    noise_only = Scored(seg(" Thank you.", 0.9, -1.4))
    assert (await FasterWhisperSTT(model=noise_only).transcribe(tone(100), 16_000)).text == ""

    # A poor microphone makes the start of a real sentence look doubtful.
    # Losing it changes what was asked, so the sentence is kept whole.
    sentence = Scored(
        seg(" Friday, ask Claude Code", 0.9, -1.2),
        seg(" to list the files in this project.", 0.9, -0.4),
    )
    result = await FasterWhisperSTT(model=sentence).transcribe(tone(100), 16_000)
    assert result.text == "Friday, ask Claude Code to list the files in this project."


def test_too_many_words_for_the_length_of_the_sound_is_noise() -> None:
    from friday.voice.control import is_implausible

    assert is_implausible("Good night.", 300)  # seen on the owner's microphone
    assert is_implausible("All right.", 400)
    assert is_implausible("Thank you.", 250)
    for word in ("Yes.", "No", "Stop!", "Okay.", "Friday"):
        assert not is_implausible(word, 250), word
    assert not is_implausible("Go ahead.", 500)
    assert not is_implausible("Tell me something interesting about octopuses.", 2000)
    assert not is_implausible("", 100)


async def test_whisper_adapter_passes_vocabulary_as_prompt_and_warms_up() -> None:
    stub = _StubWhisper()
    stt = FasterWhisperSTT(model=stub, vocabulary=("Friday", "Alex"))
    await stt.warm_up()
    assert stub.kwargs["initial_prompt"] == "Friday, Alex."
    assert stub.audio is not None and len(stub.audio) == 8000
    plain = _StubWhisper()
    await FasterWhisperSTT(model=plain).transcribe(tone(100), 16_000)
    assert plain.kwargs["initial_prompt"] is None


async def test_kokoro_adapter_reuses_one_worker_thread() -> None:
    import threading

    names: set[str] = set()

    class Engine:
        def create(self, text, voice, speed, lang):  # type: ignore[no-untyped-def]
            names.add(threading.current_thread().name)
            return np.zeros(10, dtype=np.float32), 24_000

    tts = KokoroTTS("m", "v", engine=Engine())
    await tts.warm_up()
    await asyncio.gather(tts.synthesize("one"), tts.synthesize("two"))
    assert len(names) == 1 and next(iter(names)).startswith("friday-tts")


# ---- second hardware run ----------------------------------------------------


def test_short_opening_waits_for_the_sentence_after_it() -> None:
    c = SentenceChunker()
    assert c.feed("Oh, nice. That sounds ") == []
    assert c.feed("like a lovely plan. And ") == ["Oh, nice. That sounds like a lovely plan."]
    c = SentenceChunker()
    assert c.feed("Well, I think that ") == []
    c = SentenceChunker()
    assert [*c.feed("Sure."), *c.flush()] == ["Sure."]  # a short whole reply is still said


def test_only_a_long_first_sentence_is_cut_at_a_clause() -> None:
    c = SentenceChunker()
    start = "When I look at everything you have told me about the trip so far,"
    assert c.feed(start + " it seems ") == [start]


def test_stage_directions_are_not_read_out() -> None:
    s = SpeechSanitizer()
    assert s.clean("*laughs* That is a good one.") == "That is a good one."
    assert s.clean("Oh no (sighs) not again.") == "Oh no not again."
    assert s.clean("That is *really* good (I mean it).") == "That is really good (I mean it)."


def test_sounds_below_the_absolute_level_are_never_speech() -> None:
    from friday.voice.base import AudioFrame
    from friday.voice.fakes import SAMPLE_RATE, tone
    from friday.voice.vad import EnergyVAD, frame_dbfs

    loud = tone(20)
    quiet = (np.frombuffer(loud, dtype="<i2") // 64).astype("<i2").tobytes()  # -36 dB
    level = frame_dbfs(quiet)
    silence = AudioFrame(b"\x00\x00" * (len(loud) // 2), SAMPLE_RATE)

    def score(vad: EnergyVAD, pcm: bytes) -> float:
        for _ in range(200):  # a driver that mutes silence: floor at digital zero
            vad.speech_probability(silence)
        return vad.speech_probability(AudioFrame(pcm, SAMPLE_RATE))

    assert score(EnergyVAD(), quiet) == 1.0  # the problem seen on the laptop
    assert score(EnergyVAD(min_speech_db=level + 6), quiet) == 0.0
    assert score(EnergyVAD(min_speech_db=level + 6), loud) == 1.0


# ---- listen-only command ----------------------------------------------------


async def test_listen_prints_what_was_heard_and_nothing_else() -> None:
    from friday.cli import listen_loop
    from friday.voice.fakes import ScriptedAudioSource, ScriptedSTT, Silence, Speak
    from friday.voice.vad import EnergyVAD

    lines: list[str] = []
    await listen_loop(
        ScriptedAudioSource([Silence(300), Speak(800), Silence(900), Speak(600), Silence(900)]),
        EnergyVAD(),
        ScriptedSTT(["Explain how a rainbow works.", ""]),
        0.5,
        700,
        show=lines.append,
    )
    assert len(lines) == 2
    assert lines[0].startswith("heard> Explain how a rainbow works.")
    assert "s of speech in" in lines[0]
    assert lines[1].startswith("heard> (nothing)")

    lines.clear()
    await listen_loop(
        ScriptedAudioSource([Silence(300), Speak(300), Silence(900)]),
        EnergyVAD(),
        ScriptedSTT(["Good night."]),
        0.5,
        700,
        show=lines.append,
    )
    assert lines[0].startswith("heard> (ignored as noise: Good night.)")


# ---- expressive voice ------------------------------------------------------


def test_stage_directions_become_marks_only_for_a_voice_that_can_make_them() -> None:
    from friday.voice.sanitize import SpeechSanitizer, strip_expression_tags

    text = "Oh no *laughs* that old trick. [sigh] Fine. (giggles) Really [whisper] now."
    plain = SpeechSanitizer().clean(text)
    assert "[" not in plain and "laugh" not in plain

    marked = SpeechSanitizer(("laugh", "sigh")).clean(text)
    assert marked == "Oh no [laugh] that old trick. [sigh] Fine. Really now."
    assert (
        strip_expression_tags(marked, ("laugh", "sigh"))
        == "Oh no that old trick. Fine. Really now."
    )


def test_the_prompt_names_the_marks_only_when_the_voice_has_them() -> None:
    from friday.persona.persona import Channel, Mode, Persona

    plain = Persona().system_prompt(Mode.COMPANION, Channel.VOICE)
    assert "[laugh]" not in plain
    expressive = Persona(expression_tags=("laugh", "sigh"))
    assert "[laugh], [sigh]" in expressive.system_prompt(Mode.COMPANION, Channel.VOICE)
    assert "[laugh]" not in expressive.system_prompt(Mode.COMPANION, Channel.TEXT)


def _voice_server_module() -> Any:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parent.parent / "servers" / "chatterbox_server.py"
    spec = importlib.util.spec_from_file_location("chatterbox_server", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Wav:
    """What Chatterbox hands back: a torch tensor, as far as the service uses it."""

    def __init__(self, samples: Any) -> None:
        self._samples = samples

    def squeeze(self) -> "_Wav":
        return self

    def detach(self) -> "_Wav":
        return self

    def cpu(self) -> "_Wav":
        return self

    def numpy(self) -> Any:
        return self._samples


class _Turbo:
    """Same call signatures as ChatterboxTurboTTS in chatterbox-tts 0.1.7.

    Copied from the released package on purpose: a stand-in that accepts
    anything is how a made-up ``nano`` option once passed every test.
    """

    sr = 24_000
    built_in_voice = True
    loaded_on: ClassVar[list[str]] = []
    prepared: ClassVar[list[str]] = []
    spoken: ClassVar[list[tuple[str, str | None]]] = []

    def __init__(self) -> None:
        self.conds: object | None = object() if self.built_in_voice else None

    @classmethod
    def from_pretrained(cls, device) -> "_Turbo":  # type: ignore[no-untyped-def]
        cls.loaded_on.append(device)
        return cls()

    def prepare_conditionals(  # type: ignore[no-untyped-def]
        self, wav_fpath, exaggeration=0.5, norm_loudness=True
    ) -> None:
        self.prepared.append(wav_fpath)
        self.conds = object()

    def generate(  # type: ignore[no-untyped-def]
        self,
        text,
        repetition_penalty=1.2,
        min_p=0.0,
        top_p=0.95,
        audio_prompt_path=None,
        exaggeration=0.0,
        cfg_weight=0.0,
        temperature=0.8,
        top_k=1000,
        norm_loudness=True,
    ) -> _Wav:
        assert self.conds is not None or audio_prompt_path
        self.spoken.append((text, audio_prompt_path))
        return _Wav(np.array([[0.0, 0.5, -2.0]], dtype=np.float32))


@pytest.fixture
def turbo(monkeypatch: pytest.MonkeyPatch) -> type[_Turbo]:
    import sys
    import types

    class Turbo(_Turbo):
        loaded_on: ClassVar[list[str]] = []
        prepared: ClassVar[list[str]] = []
        spoken: ClassVar[list[tuple[str, str | None]]] = []

    package = types.ModuleType("chatterbox")
    module = types.ModuleType("chatterbox.tts_turbo")
    module.ChatterboxTurboTTS = Turbo  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chatterbox", package)
    monkeypatch.setitem(sys.modules, "chatterbox.tts_turbo", module)
    return Turbo


def test_voice_service_loads_chatterbox_the_way_the_package_expects(
    turbo: type[_Turbo],
) -> None:
    voice = _voice_server_module().ChatterboxVoice("cuda", None)
    assert turbo.loaded_on == ["cuda"]
    assert (voice.name, voice.device, voice.sample_rate) == ("chatterbox-turbo", "cuda", 24_000)

    pcm = voice.speak("Hello there.")
    assert turbo.spoken == [("Hello there.", None)]
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [0, 16383, -32767]  # clipped, 16-bit


def test_voice_service_works_out_a_reference_voice_once(turbo: type[_Turbo]) -> None:
    voice = _voice_server_module().ChatterboxVoice("cpu", "/voices/mine.wav")
    voice.speak("One.")
    voice.speak("Two.")
    assert turbo.prepared == ["/voices/mine.wav"]
    assert turbo.spoken == [("One.", None), ("Two.", None)]


def test_voice_service_stops_clearly_when_the_model_has_no_voice(turbo: type[_Turbo]) -> None:
    turbo.built_in_voice = False
    with pytest.raises(SystemExit, match="--voice"):
        _voice_server_module().ChatterboxVoice("cuda", None)


def test_voice_service_start_up_options(
    turbo: type[_Turbo], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _voice_server_module()

    class Stopped:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            assert address == ("127.0.0.1", 5005)  # this machine only

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(module, "ThreadingHTTPServer", Stopped)
    assert module.main(["--device", "cpu"]) == 0
    assert turbo.loaded_on == ["cpu"]
    assert turbo.spoken == [("Ready.", None)]
    with pytest.raises(SystemExit):
        module.main(["--device", "cpu", "--nano"])  # never existed in Chatterbox


def _voice_service(fail: bool = False) -> tuple[object, list[str]]:
    """The real HTTP handler from servers/chatterbox_server.py with a stand-in model."""
    import threading
    from http.server import ThreadingHTTPServer

    module = _voice_server_module()
    said: list[str] = []

    class StandIn:
        name, device, sample_rate = "stand-in", "cpu", 22_050

        def speak(self, text: str) -> bytes:
            if fail:
                raise RuntimeError("out of memory")
            said.append(text)
            return b"\x01\x00" * 100

    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(StandIn()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, said


async def test_remote_voice_speaks_through_the_local_service() -> None:
    from friday.voice.adapters.remote_tts import RemoteTTS

    server, said = _voice_service()
    try:
        tts = RemoteTTS(f"http://127.0.0.1:{server.server_address[1]}")  # type: ignore[attr-defined]
        assert (await tts.health())["model"] == "stand-in"
        audio = await tts.synthesize("Hello [laugh] there.")
        assert said == ["Hello [laugh] there."]
        assert audio.sample_rate == 22_050 and len(audio.pcm) == 200
    finally:
        server.shutdown()  # type: ignore[attr-defined]


async def test_voice_service_rejects_bad_requests_and_reports_failures() -> None:
    import httpx

    from friday.core.errors import VoiceError
    from friday.voice.adapters.remote_tts import RemoteTTS

    server, _ = _voice_service(fail=True)
    base = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        async with httpx.AsyncClient(base_url=base) as client:
            assert (await client.post("/speak", json={"text": ""})).status_code == 400
            assert (await client.post("/speak", json={"text": "x" * 2000})).status_code == 400
            assert (await client.post("/speak", content=b"not json")).status_code == 400
            assert (await client.get("/elsewhere")).status_code == 404
        with pytest.raises(VoiceError):
            await RemoteTTS(base).synthesize("Hello.")
    finally:
        server.shutdown()  # type: ignore[attr-defined]


async def test_fallback_voice_takes_over_and_the_expressive_one_is_retried_later() -> None:
    from friday.core.errors import VoiceError
    from friday.voice.adapters.remote_tts import FallbackTTS
    from friday.voice.base import AudioChunk
    from friday.voice.fakes import FakeTTS

    class Expressive(FakeTTS):
        expression_tags = ("laugh",)
        up = False

        async def synthesize(self, text: str) -> AudioChunk:
            if not self.up:
                raise VoiceError("card is busy")
            return await super().synthesize(text)

    now = [0.0]
    changes: list[bool] = []
    primary, builtin = Expressive(), FakeTTS()
    tts = FallbackTTS(primary, builtin, 30.0, changes.append, clock=lambda: now[0])
    assert tts.expression_tags == ("laugh",)

    await tts.synthesize("Ha [laugh] nice.")
    assert builtin.spoken == ["Ha nice."]  # the built-in voice never sees the mark
    primary.up = True
    await tts.synthesize("Still waiting.")
    assert primary.spoken == []  # not retried on every sentence

    now[0] = 31.0
    await tts.synthesize("Back [laugh] again.")
    assert primary.spoken == ["Back [laugh] again."]
    assert changes == [False, True]


def test_voice_service_address_must_be_this_machine() -> None:
    from pydantic import ValidationError

    from friday.config import VoiceSettings

    assert VoiceSettings(tts_server_url="http://127.0.0.1:5005").tts_server_url
    for bad in ("http://192.168.1.20:5005", "https://example.com", "ftp://localhost"):
        with pytest.raises(ValidationError):
            VoiceSettings(tts_server_url=bad)


async def test_typed_input_is_read_on_a_thread_that_cannot_block_exit() -> None:
    import threading

    from friday.cli import _read_line

    seen: list[threading.Thread] = []

    def read(prompt: str) -> str:
        seen.append(threading.current_thread())
        return "hello"

    assert await _read_line("you> ", read) == "hello"
    assert seen[0].daemon  # a pool thread would keep the program alive after Ctrl+C

    def closed(prompt: str) -> str:
        raise EOFError

    with pytest.raises(EOFError):
        await _read_line("you> ", closed)
