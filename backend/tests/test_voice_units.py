import numpy as np

from friday.voice.adapters.faster_whisper_stt import FasterWhisperSTT, pcm16_to_float32
from friday.voice.adapters.kokoro_tts import KokoroTTS, float_to_pcm16
from friday.voice.base import AudioFrame
from friday.voice.chunker import SentenceChunker
from friday.voice.control import ControlIntent, classify
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
    assert out[0] == "The thing about octopuses,"  # early clause break for fast first audio
    # after the first chunk, clauses are not split
    assert "which most people never realise, is how clever they are." in out
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


# ---- adapter format conversion (models stubbed) -----------------------------


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _StubWhisper:
    def __init__(self) -> None:
        self.audio = None

    def transcribe(self, audio, **kwargs):  # type: ignore[no-untyped-def]
        self.audio = audio
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


async def test_whisper_adapter_drops_low_confidence_noise_segments() -> None:
    class Noisy:
        def transcribe(self, audio, **kwargs):  # type: ignore[no-untyped-def]
            def seg(text: str, no_speech: float, logprob: float):  # type: ignore[no-untyped-def]
                return type(
                    "S", (), {"text": text, "no_speech_prob": no_speech, "avg_logprob": logprob}
                )()

            segments = [seg(" Thank you.", 0.9, -1.4), seg(" Open the report.", 0.1, -0.3)]
            return iter(segments), type("I", (), {"language": "en"})()

    result = await FasterWhisperSTT(model=Noisy()).transcribe(tone(100), 16_000)
    assert result.text == "Open the report."
