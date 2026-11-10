import asyncio
import sys
import types

from friday.voice.base import AudioChunk
from friday.voice.playback import PlaybackBuffer


def test_read_is_exact_size_zero_padded_and_gapless_across_items() -> None:
    buf = PlaybackBuffer()
    done: list[float] = []
    buf.write(b"\x01" * 6, on_done=done.append)
    buf.write(b"\x02" * 6, on_done=done.append)
    assert buf.pending == 12
    assert buf.read(8) == b"\x01" * 6 + b"\x02" * 2  # no silence between sentences
    assert done == [1.0]
    assert buf.read(8) == b"\x02" * 4 + b"\x00" * 4
    assert done == [1.0, 1.0] and buf.pending == 0
    assert buf.read(4) == b"\x00" * 4  # idle: silence


def test_clear_reports_fraction_played_and_bytes_dropped() -> None:
    buf = PlaybackBuffer()
    done: list[float] = []
    buf.write(b"\x01" * 100, on_done=done.append)
    buf.write(b"\x02" * 50, on_done=done.append)
    buf.read(25)
    assert buf.clear() == 125
    assert done == [0.25, 0.0]
    assert buf.read(4) == b"\x00" * 4


class _FakeStream:
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.aborted = False

    def start(self) -> None: ...
    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None: ...

    def pull(self, nbytes: int) -> bytes:
        out = bytearray(nbytes)
        self.callback(out, nbytes // 2, None, None)
        return bytes(out)


async def test_speaker_sink_uses_one_stream_and_stops_mid_chunk(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    streams: list[_FakeStream] = []

    def make(**kwargs):  # type: ignore[no-untyped-def]
        streams.append(_FakeStream(**kwargs))
        return streams[-1]

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(RawOutputStream=make))
    from friday.voice.adapters.sounddevice_io import SpeakerSink

    sink = SpeakerSink()
    first = asyncio.create_task(sink.play(AudioChunk(b"\x01\x00" * 100, 24_000)))
    await asyncio.sleep(0)
    assert streams[0].pull(200) == b"\x01\x00" * 100
    assert await first == 1.0

    second = asyncio.create_task(sink.play(AudioChunk(b"\x02\x00" * 100, 24_000)))
    await asyncio.sleep(0)
    streams[0].pull(50)
    sink.stop()  # barge-in
    assert await second == 0.25
    assert streams[0].pull(20) == b"\x00" * 20  # silent immediately after stop
    assert len(streams) == 1  # same device stream for both sentences
    sink.close()
    assert streams[0].aborted
