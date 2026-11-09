"""API key storage.

Keys never live in the config file. Lookup order:

1. OS credential store via ``keyring`` (Windows Credential Manager on the
   target platform) -- the intended production path.
2. Environment variable ``FRIDAY_<NAME>_API_KEY`` -- for development and CI.

Every key that is read is registered with the log redactor.
"""

from __future__ import annotations

import os
from typing import Protocol

from friday.core.logging import secrets_registry

SERVICE_NAME = "friday"


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


def _env_var(name: str) -> str:
    return f"FRIDAY_{name.upper().replace('-', '_')}_API_KEY"


class KeyringEnvSecretStore:
    def __init__(self, use_keyring: bool = True) -> None:
        self._keyring = None
        if use_keyring:
            try:
                import keyring

                self._keyring = keyring
            except ImportError:
                self._keyring = None

    def get(self, name: str) -> str | None:
        value: str | None = None
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE_NAME, name)
            except Exception:  # backend unavailable (e.g. headless Linux CI)
                value = None
        if not value:
            value = os.environ.get(_env_var(name)) or None
        secrets_registry.register(value)
        return value

    def set(self, name: str, value: str) -> None:
        if self._keyring is None:
            raise RuntimeError("keyring is not installed; install friday[secrets]")
        secrets_registry.register(value)
        self._keyring.set_password(SERVICE_NAME, name, value)

    def delete(self, name: str) -> None:
        if self._keyring is None:
            raise RuntimeError("keyring is not installed; install friday[secrets]")
        self._keyring.delete_password(SERVICE_NAME, name)


class InMemorySecretStore:
    """For tests."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})
        for v in self._values.values():
            secrets_registry.register(v)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        secrets_registry.register(value)
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)
