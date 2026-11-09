"""Error hierarchy shared across components."""

from __future__ import annotations


class FridayError(Exception):
    """Base class for all FRIDAY errors."""


class ConfigError(FridayError):
    """Invalid or missing configuration."""


class ProviderError(FridayError):
    """A language-model provider failed.

    ``retryable`` tells the router whether falling back to another provider
    (or retrying later) is sensible. Authentication errors are not retryable
    on the same provider but the router may still fall back to a different one.
    """

    def __init__(self, provider: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.retryable = retryable


class ProviderUnavailableError(ProviderError):
    """No provider in the route could serve the request."""


class VoiceError(FridayError):
    """Speech recognition, synthesis or audio device failure."""
