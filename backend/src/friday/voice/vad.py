"""Built-in energy-based VAD with an adaptive noise floor.

Good enough for a headset in a quiet room and fully deterministic for tests.
A neural VAD (Silero) is the planned upgrade for noisy rooms and speakers;
it plugs in behind the same ``VoiceActivityDetector`` protocol.
"""

from __future__ import annotations

import math
from collections import deque

import numpy as np

from friday.voice.base import AudioFrame


def frame_dbfs(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if samples.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms <= 0.0:
        return -100.0
    return max(-100.0, 20.0 * math.log10(rms / 32768.0))


class EnergyVAD:
    """Speech probability from loudness relative to an estimated noise floor.

    The floor uses *minimum statistics*: the quietest frame level seen in the
    last ``floor_window_s``. Speech has natural gaps, so the minimum tracks the
    background; a new constant noise (a fan switching on) becomes the floor
    after one window instead of being mistaken for endless speech.
    """

    def __init__(
        self,
        margin_db: float = 12.0,
        softness_db: float = 6.0,
        initial_floor_db: float = -60.0,
        min_floor_db: float = -90.0,
        floor_window_s: float = 3.0,
        min_speech_db: float | None = None,
    ) -> None:
        self._margin = margin_db
        self._soft = softness_db
        self._initial = initial_floor_db
        self._min_floor = min_floor_db
        self._window_s = floor_window_s
        # Absolute gate. Some microphone drivers mute silence completely, so
        # the floor is near digital zero and *any* sound, however faint, looks
        # loud next to it. Frames below this level are never speech.
        self._min_speech = min_speech_db
        self._levels: deque[float] | None = None
        self.noise_floor_db = initial_floor_db

    def speech_probability(self, frame: AudioFrame) -> float:
        if self._levels is None:
            frames = max(1, round(self._window_s * 1000 / max(frame.duration_ms, 1.0)))
            self._levels = deque(maxlen=frames)
        db = frame_dbfs(frame.pcm)
        self._levels.append(db)
        lowest = min(self._levels)
        if len(self._levels) < (self._levels.maxlen or 0):
            lowest = min(lowest, self._initial)  # not enough history yet
        self.noise_floor_db = max(self._min_floor, lowest)
        if self._min_speech is not None and db < self._min_speech:
            return 0.0
        snr = db - self.noise_floor_db
        return min(1.0, max(0.0, (snr - self._margin) / (2 * self._soft) + 0.5))

    def reset(self) -> None:
        self._levels = None
        self.noise_floor_db = self._initial
