"""Finding past chat lines by meaning, not only by the same words.

A small "embedding" model (run by Ollama on the processor, so it never takes
the graphics card) turns each line into a list of numbers; lines about the
same thing get similar numbers. "My vehicle maintenance" then finds "I need to
give my car for service", and "the guy from the video game" finds Kratos.

Everything stays on this computer: Ollama runs here, and the numbers are kept
in the chats database next to the lines they belong to.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx
import numpy as np
import numpy.typing as npt

log = logging.getLogger(__name__)

Vectors = npt.NDArray[np.float32]

# How each model wants a question worded, and how alike a line must be to
# count as a match. Measured on the owner's own chats (2026-11-24): real
# matches scored 0.44-0.61, unrelated lines up to about 0.42.
_MODELS: dict[str, tuple[str, str, float]] = {
    "qwen3-embedding": (
        "",
        "Instruct: Given a question about a past chat, "
        "find the chat messages it refers to\nQuery: ",
        0.44,
    ),
    "nomic-embed-text": ("search_document: ", "search_query: ", 0.6),
    "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: ", 0.6),
}
MAX_CHARS = 1000  # of each line; the start says what it is about
BATCH = 32


class Meanings:
    """Turns text into vectors with an Ollama embedding model on the processor."""

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434") -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        family = model.split(":")[0].split("/")[-1]
        self._line, self._question, self.close = _MODELS.get(family, ("", "", 0.6))

    async def _embed(self, texts: Sequence[str]) -> Vectors:
        out: list[Vectors] = []
        async with httpx.AsyncClient(timeout=300) as client:
            for start in range(0, len(texts), BATCH):
                reply = await client.post(
                    f"{self.base_url}/api/embed",
                    json={
                        "model": self.model,
                        "input": [t[:MAX_CHARS] for t in texts[start : start + BATCH]],
                        "options": {"num_gpu": 0},  # the processor, never the card
                        "keep_alive": "5m",
                    },
                )
                reply.raise_for_status()
                out.append(np.asarray(reply.json()["embeddings"], dtype=np.float32))
        vectors = np.concatenate(out) if out else np.zeros((0, 0), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return (vectors / np.maximum(norms, 1e-9)).astype(np.float32)

    async def lines(self, texts: Sequence[str]) -> Vectors:
        return await self._embed([self._line + t for t in texts])

    async def question(self, text: str) -> Vectors:
        vectors = await self._embed([self._question + text])
        return np.asarray(vectors[0], dtype=np.float32)
