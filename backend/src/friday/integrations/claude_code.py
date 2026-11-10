"""Hand a task to Claude Code and get its answer back.

FRIDAY does none of the work herself: she passes the user's words to the
``claude`` command-line program in non-interactive mode and reports what it
says. Claude Code uses its own login on this machine, so no API key is stored
or seen here, and its own permission settings decide what it may do.

The task text is sent on standard input, never as a command-line argument, so
dictated text can never be interpreted by a shell.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from friday.core.errors import FridayError

# Told to Claude Code with every task, so the result can be read aloud.
RELAY_NOTE = (
    "\n\n(This request was dictated by voice and passed on by a voice assistant, so "
    "names may be misspelt; use your judgement. When you have finished, end your reply "
    "with one final line that starts with 'SPOKEN:' followed by one or two short plain "
    "sentences, without markdown, that say what the outcome is. That line is read aloud.)"
)

_SESSION_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_SPOKEN = re.compile(r"^\W*SPOKEN:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class CodeAgentError(FridayError):
    """Claude Code could not be started or did not answer."""


@dataclass(frozen=True, slots=True)
class CodeResult:
    text: str
    session_id: str | None = None
    ok: bool = True
    denied: int = 0  # actions Claude Code was not permitted to take


class CodeAgent(Protocol):
    async def run(self, task: str, resume: str | None = None) -> CodeResult: ...


def spoken_summary(text: str, limit: int = 300) -> str:
    """The line meant for the voice, or the start of the reply if there is none."""
    found = _SPOKEN.findall(text)
    if found:
        return str(found[-1]).strip()
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + " ... the rest is in the log."


def parse_result(stdout: str, returncode: int, stderr: str = "") -> CodeResult:
    try:
        data = json.loads(stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        detail = (stderr or stdout).strip()[-500:] or f"exit code {returncode}"
        raise CodeAgentError(f"Claude Code gave no usable answer: {detail}")
    session = data.get("session_id")
    denials = data.get("permission_denials")
    return CodeResult(
        text=str(data.get("result") or "").strip(),
        session_id=session if isinstance(session, str) and _SESSION_ID.match(session) else None,
        ok=returncode == 0 and not data.get("is_error", False),
        denied=len(denials) if isinstance(denials, list) else 0,
    )


class ClaudeCodeCLI:
    def __init__(
        self,
        project_dir: Path,
        command: str = "claude",
        permission_mode: str = "",
        timeout_s: float = 1800.0,
    ) -> None:
        self.project_dir = project_dir
        self._command = command
        self._mode = permission_mode
        self._timeout = timeout_s

    def arguments(self, resume: str | None = None) -> list[str]:
        """Fixed arguments only: nothing the user said ends up in here."""
        args = ["-p", "--output-format", "json"]
        if self._mode:
            args += ["--permission-mode", self._mode]
        if resume and _SESSION_ID.match(resume):
            args += ["--resume", resume]
        return args

    async def run(self, task: str, resume: str | None = None) -> CodeResult:
        exe = shutil.which(self._command)
        if exe is None:
            raise CodeAgentError(f"'{self._command}' is not installed or not on the PATH")
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *self.arguments(resume),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_dir,
            )
        except OSError as exc:
            raise CodeAgentError(f"could not start Claude Code: {exc}") from exc
        try:
            out, err = await asyncio.wait_for(
                proc.communicate((task + RELAY_NOTE).encode("utf-8")), self._timeout
            )
        except TimeoutError as exc:
            raise CodeAgentError(f"Claude Code did not finish in {self._timeout:.0f} s") from exc
        finally:
            if proc.returncode is None:  # timed out or cancelled: do not leave it running
                proc.kill()
        return parse_result(
            out.decode("utf-8", "replace"), proc.returncode or 0, err.decode("utf-8", "replace")
        )
