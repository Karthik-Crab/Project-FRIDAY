"""Taking the singing out of a song (Demucs), without running Demucs."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from friday.integrations.vocals import (
    KEEP,
    REMOVE,
    VocalError,
    VocalSplitter,
    output_name,
    parse_vocal_task,
)

# Stands in for Demucs: writes both parts where Demucs would.
FAKE_DEMUCS = """
import sys
from pathlib import Path
args = sys.argv[1:]
if "fail" in args[-1]:
    sys.exit("CUDA out of memory")
out = Path(args[args.index("-o") + 1]) / args[args.index("-n") + 1] / Path(args[-1]).stem
out.mkdir(parents=True)
(out / "vocals.mp3").write_text("voice")
(out / "no_vocals.mp3").write_text("music")
"""


@pytest.mark.parametrize(
    ("text", "mode"),
    [
        ("can you remove the vocals only?", REMOVE),
        ("remove the vocals", REMOVE),
        ("make it karaoke", REMOVE),
        ("I want the instrumental", REMOVE),
        ("the song without the singing", REMOVE),
        ("take out the voice", REMOVE),
        ("vocals only", KEEP),
        ("give me only the voice", KEEP),
        ("keep the vocals and remove the music", KEEP),
        ("extract the vocals", KEEP),
        ("acapella version please", KEEP),
        ("remove the noise", None),
        ("add bass", None),
        ("who is the singer?", None),
    ],
)
def test_reading_the_request(text: str, mode: str | None) -> None:
    assert parse_vocal_task(text) == mode


def test_names_do_not_overwrite(tmp_path: Path) -> None:
    first = output_name(tmp_path / "Old Song.mp3", REMOVE, tmp_path)
    assert first.name == "Old Song (no vocals 1).mp3"
    first.write_text("x")
    assert output_name(tmp_path / "Old Song.mp3", REMOVE, tmp_path).name == (
        "Old Song (no vocals 2).mp3"
    )
    assert output_name(tmp_path / "a.wav", KEEP, tmp_path).name == "a (vocals only 1).mp3"


def _splitter(tmp_path: Path) -> VocalSplitter:
    script = tmp_path / "fake_demucs.py"
    script.write_text(FAKE_DEMUCS)
    return VocalSplitter([sys.executable, str(script)])


def test_keeps_the_part_asked_for_and_tidies_up(tmp_path: Path) -> None:
    song = tmp_path / "song.mp3"
    song.write_text("song")
    out = tmp_path / "out"
    out.mkdir()
    splitter = _splitter(tmp_path)
    music = asyncio.run(splitter.run(song, REMOVE, out / "a.mp3"))
    voice = asyncio.run(splitter.run(song, KEEP, out / "b.mp3"))
    assert music.read_text() == "music" and voice.read_text() == "voice"
    assert sorted(p.name for p in out.iterdir()) == ["a.mp3", "b.mp3"]  # no leftovers


def test_a_failure_says_why(tmp_path: Path) -> None:
    song = tmp_path / "fail.mp3"
    song.write_text("song")
    with pytest.raises(VocalError, match="CUDA out of memory"):
        asyncio.run(_splitter(tmp_path).run(song, REMOVE, tmp_path / "x.mp3"))
    assert not (tmp_path / "x.mp3").exists()
