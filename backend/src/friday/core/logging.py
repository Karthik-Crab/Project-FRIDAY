"""Logging setup with secret redaction.

Requirement 10: logs must never expose API keys or sensitive information.
Two layers of defence:

1. Known secret values are registered at load time and masked verbatim.
2. Common API-key shapes are masked by pattern, in case a secret reaches a
   log line without being registered (e.g. echoed back in a provider error).
"""

from __future__ import annotations

import logging
import re
import threading

_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{8,}"),
]
_MASK = "***REDACTED***"


class SecretRegistry:
    """Thread-safe set of secret values that must never be logged."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def register(self, value: str | None) -> None:
        if value and len(value) >= 6:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            text = text.replace(value, _MASK)
        for pattern in _KEY_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + _MASK, text)
        return text


secrets_registry = SecretRegistry()


class RedactingFilter(logging.Filter):
    def __init__(self, registry: SecretRegistry = secrets_registry) -> None:
        super().__init__()
        self._registry = registry

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._registry.redact(record.exc_text)
        record.msg = self._registry.redact(message)
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Per-request lines from the HTTP client are noise at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
