"""Thread-safe playback buffer shared by the audio callback and the event loop.

Pure logic (no audio device), so the behaviour that matters for interruption
-- what was played, what was dropped, who gets told -- is unit-testable.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Item:
    data: bytes
    pos: int
    on_done: Callable[[float], None] | None

    @property
    def fraction(self) -> float:
        return self.pos / len(self.data) if self.data else 1.0


class PlaybackBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: deque[_Item] = deque()
        self._pending = 0

    @property
    def pending(self) -> int:
        """Bytes written but not yet handed to the device."""
        return self._pending

    def write(self, data: bytes, on_done: Callable[[float], None] | None = None) -> None:
        """Queue audio. ``on_done(fraction_played)`` fires once, when the item
        has been fully read or dropped by ``clear``. It is called from whichever
        thread finishes it (usually the audio thread): keep it tiny."""
        if not data:
            if on_done:
                on_done(1.0)
            return
        with self._lock:
            self._items.append(_Item(data, 0, on_done))
            self._pending += len(data)

    def read(self, n: int) -> bytes:
        """Called by the audio callback: exactly ``n`` bytes, zero-padded."""
        out = bytearray()
        finished: list[_Item] = []
        with self._lock:
            while len(out) < n and self._items:
                item = self._items[0]
                piece = item.data[item.pos : item.pos + n - len(out)]
                out += piece
                item.pos += len(piece)
                self._pending -= len(piece)
                if item.pos >= len(item.data):
                    finished.append(self._items.popleft())
        for item in finished:
            if item.on_done:
                item.on_done(1.0)
        if len(out) < n:
            out += b"\x00" * (n - len(out))
        return bytes(out)

    def clear(self) -> int:
        """Drop all unplayed audio. Returns the number of bytes dropped."""
        with self._lock:
            items, self._items = list(self._items), deque()
            dropped, self._pending = self._pending, 0
        for item in items:
            if item.on_done:
                item.on_done(item.fraction)
        return dropped
