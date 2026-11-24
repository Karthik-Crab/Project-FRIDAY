"""Starts, parks and stops the expressive voice service (Chatterbox).

The voice model needs about 3 GB of the graphics card. With ``start_parked``
the service loads it into RAM only, and ``to_card`` moves it onto the card
and back in about half a second (see ``friday.integrations.card``), so the
talking model and picture making have the whole card the rest of the time.

The command comes from the config file, never from anything said or typed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from friday.core.errors import FridayError

log = logging.getLogger(__name__)


class VoiceServiceError(FridayError):
    pass


class VoiceServiceLauncher:
    def __init__(
        self,
        command: Sequence[str],
        health_url: str,
        *,
        env: Mapping[str, str] | None = None,
        log_path: Path | None = None,
        start_timeout_s: float = 120.0,
        poll_s: float = 1.0,
        client: httpx.AsyncClient | None = None,
        label: str = "the voice service",
        start_parked: bool = False,
    ) -> None:
        self.label = label
        self.command = [str(Path(part).expanduser()) if "~" in part else part for part in command]
        self.health_url = health_url.rstrip("/") + "/health"
        self.env = {k: str(Path(v).expanduser()) if "~" in v else v for k, v in (env or {}).items()}
        self.log_path = log_path
        self.start_timeout_s = start_timeout_s
        self._poll_s = poll_s
        self._client = client or httpx.AsyncClient(timeout=3.0)
        self._process: subprocess.Popen[bytes] | None = None
        if start_parked:  # the server waits in RAM until asked onto the card
            self.env["FRIDAY_VOICE_PARKED"] = "1"

    @property
    def started_here(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def is_up(self) -> bool:
        try:
            response = await self._client.get(self.health_url)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _spawn(self) -> subprocess.Popen[bytes]:
        output: Any = subprocess.DEVNULL
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            output = self.log_path.open("ab")
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True
        try:
            return subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env={**os.environ, **self.env},
                **options,
            )
        except OSError as exc:
            raise VoiceServiceError(
                f"could not start {self.label} ({self.command[0]}): {exc}"
            ) from exc
        finally:
            if output is not subprocess.DEVNULL:
                output.close()

    async def start(self) -> bool:
        """Start the service if it is not running. True if this call started it."""
        if await self.is_up():
            return False
        if not self.command:
            raise VoiceServiceError(f"no command to start {self.label} is set")
        if self._process is None or self._process.poll() is not None:
            log.info("starting %s: %s", self.label, " ".join(self.command))
            self._process = self._spawn()
        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_s)
            if await self.is_up():
                return True
            if self._process.poll() is not None:
                where = f" See {self.log_path}." if self.log_path else ""
                raise VoiceServiceError(f"{self.label} stopped while starting.{where}")
        self.stop()
        raise VoiceServiceError(
            f"{self.label} did not start within {self.start_timeout_s:.0f} seconds"
        )

    async def to_card(self, on: bool) -> bool:
        """Move the voice model onto the graphics card (``on``) or into RAM.

        False when the service can't do that (an older one, already on the
        card since it started)."""
        url = self.health_url.removesuffix("/health") + "/card"
        try:
            response = await self._client.post(url, json={"on": on}, timeout=60.0)
        except httpx.HTTPError as exc:
            raise VoiceServiceError(f"{self.label} didn't answer: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise VoiceServiceError(f"{self.label} couldn't move: {response.text[:200]}")
        return True

    def stop(self) -> None:
        """Stop the service if this app started it, which frees its graphics memory."""
        process, self._process = self._process, None
        if process is None or process.poll() is not None:
            return
        log.info("stopping %s", self.label)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    async def aclose(self) -> None:
        self.stop()
        await self._client.aclose()
