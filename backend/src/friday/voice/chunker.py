"""Split a streaming LLM reply into speakable chunks.

Time-to-first-audio matters more than anything else for perceived latency, so
the first chunk may break early at a clause boundary; later chunks prefer whole
sentences because TTS prosody is better with complete sentences.
"""

from __future__ import annotations

import re

_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "jr", "sr", "prof"}
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s)")
_CLAUSE_END = re.compile("[,;:\u2013\u2014](?=\\s)")  # incl. en/em dash


class SentenceChunker:
    def __init__(
        self,
        first_min_chars: int = 4,
        min_chars: int = 40,
        max_chars: int = 250,
        first_clause_min_chars: int = 10,
    ) -> None:
        # A short opening sentence ("Oh, nice.") is spoken at once; a clause
        # must be a little longer, or a lone "Well," gets its own odd pause.
        self._first_min = first_min_chars
        self._first_clause_min = first_clause_min_chars
        self._min = min_chars
        self._max = max_chars
        self._buf = ""
        self._emitted_any = False

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while True:
            chunk = self._take()
            if chunk is None:
                return out
            out.append(chunk)

    def flush(self) -> list[str]:
        rest = self._buf.strip()
        self._buf = ""
        return [rest] if rest else []

    def _cut(self, index: int) -> str:
        chunk, self._buf = self._buf[:index].strip(), self._buf[index:].lstrip()
        self._emitted_any = True
        return chunk

    def _take(self) -> str | None:
        min_chars = self._min if self._emitted_any else self._first_min
        for match in _SENTENCE_END.finditer(self._buf):
            end = match.end()
            if end < min_chars:
                continue
            word = self._buf[: match.start()].rsplit(None, 1)
            if word and word[-1].lower().rstrip(".") in _ABBREVIATIONS and match.group() == ".":
                continue
            return self._cut(end)
        if not self._emitted_any:
            for match in _CLAUSE_END.finditer(self._buf):
                if match.end() >= self._first_clause_min:
                    return self._cut(match.end())
        if len(self._buf) > self._max:
            space = self._buf.rfind(" ", 0, self._max)
            return self._cut(space if space > 0 else self._max)
        return None
