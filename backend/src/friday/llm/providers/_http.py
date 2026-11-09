"""Shared HTTP helpers for HTTP-based provider adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from friday.core.errors import ProviderError

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


def raise_for_status(provider: str, response: httpx.Response, body: bytes) -> None:
    if response.status_code < 400:
        return
    # Never include request headers (they carry the key). Body is truncated.
    detail = body[:300].decode("utf-8", "replace")
    retryable = response.status_code in (408, 409, 429) or response.status_code >= 500
    if response.status_code in (401, 403):
        detail = "authentication failed (check the API key)"
    raise ProviderError(provider, f"HTTP {response.status_code}: {detail}", retryable=retryable)


async def iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield the ``data:`` payloads of a server-sent-events stream."""
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            yield line[5:].strip()


async def iter_ndjson(response: httpx.Response) -> AsyncIterator[dict[str, object]]:
    async for line in response.aiter_lines():
        line = line.strip()
        if line:
            yield json.loads(line)
