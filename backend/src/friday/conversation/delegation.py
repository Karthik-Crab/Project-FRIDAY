"""Passing a spoken task on to a coding agent (Claude Code).

Everything here is deterministic: a fixed phrase starts it ("ask Claude Code
to ..."), FRIDAY reads the task back, and only a spoken "yes" sends it. The
language model is not involved, so a small local model can never start a coding
agent by mistake, and a misheard sentence is caught before anything runs.

The work itself runs in the background; the conversation carries on, and a
``Notice`` is delivered through ``on_done`` when the agent has finished.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from friday.conversation.notes import NoteBook
from friday.core.errors import FridayError
from friday.integrations.claude_code import CodeAgent, CodeResult, spoken_summary

log = logging.getLogger(__name__)

# Speech recognisers write "Claude Code" in many ways.
_NAME = r"(?:claude|claud|cloud|clod|clawed|claudia)(?:[\s-]*(?:code|coat|coda|cold))?"
_SEP = r"[\s,.:;!?-]+"
_LEAD = rf"^\W*(?:(?:hey|ok|okay){_SEP})?(?:friday{_SEP})?(?:please{_SEP})?"
_DELEGATE = re.compile(
    rf"{_LEAD}(?:(?:can|could|would){_SEP}you{_SEP})?(?:please{_SEP})?"
    rf"(?:(?:ask|tell|get|have){_SEP}{_NAME}"
    rf"|(?:send|pass|give|forward){_SEP}(?:(?:this|that|it){_SEP})?(?:on{_SEP})?to{_SEP}{_NAME})"
    rf"{_SEP}(?:to{_SEP})?(?P<task>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_CANCEL = re.compile(
    rf"{_LEAD}(?:cancel|stop|abort|kill){_SEP}(?:the{_SEP})?{_NAME}(?:{_SEP}(?:task|job))?\W*$",
    re.IGNORECASE,
)
_FRESH = re.compile(
    rf"{_LEAD}(?:start{_SEP})?(?:a{_SEP})?(?:new|fresh){_SEP}{_NAME}{_SEP}(?:session|chat)\W*$",
    re.IGNORECASE,
)
# While a task is waiting for "yes", saying the command again replaces it. The
# recogniser often hears "ask" as "as" there, so that is accepted too.
_AGAIN = re.compile(
    rf"{_LEAD}(?:(?:no|now|so){_SEP})?(?:ask|as|asked|us){_SEP}{_NAME}{_SEP}(?:to{_SEP})?(?P<task>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_YES = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|correct|right|okay|ok|confirm|confirmed|send it|go ahead|"
    r"do it|go|that s right|that s it|yes please|yes send it|please do)$"
)
_NO = re.compile(
    r"^(?:no|nope|nah|wrong|cancel|cancel that|don t|do not|don t send it|forget it|"
    r"scrap that|never ?mind|no thanks|no don t)$"
)


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


def parse_task(text: str) -> str | None:
    match = _DELEGATE.match(text.strip())
    return match.group("task").strip() if match else None


@dataclass(frozen=True, slots=True)
class Notice:
    """What to tell the user when the agent has finished."""

    task: str
    spoken: str
    ok: bool
    log_path: Path | None = None


class CodeDelegate:
    def __init__(
        self,
        agent: CodeAgent,
        log_dir: Path | None = None,
        on_done: Callable[[Notice], None] | None = None,
        name: str = "Claude Code",
        state_path: Path | None = None,
        keep_session: str = "run",
        scope: str = "",
        notes: NoteBook | None = None,
    ) -> None:
        self.agent = agent
        self.log_dir = log_dir
        self.on_done = on_done
        self.name = name
        self.pending: str | None = None  # read back, waiting for "yes"
        self.session_id: str | None = None  # follow-ups continue the same session
        self._job: asyncio.Task[None] | None = None
        # Where the session is remembered between runs of FRIDAY, so one Claude
        # Code chat can last the whole day ("day") or until told otherwise
        # ("always"). ``scope`` is the project folder: a chat belongs to it.
        self.state_path = state_path if keep_session in ("day", "always") else None
        self.keep_session = keep_session
        self.scope = scope
        self._load_session()
        # Shared memory: passed along with the first task of a chat, and again
        # when a note has been added; each finished task goes into its work log.
        self.notes = notes
        self._notes_sent = ""

    @property
    def busy(self) -> bool:
        return self._job is not None and not self._job.done()

    def concerns(self, text: str) -> bool:
        """Would ``handle`` answer this itself (rather than the language model)?"""
        return (
            self.pending is not None
            or parse_task(text) is not None
            or bool(_CANCEL.match(text) or _FRESH.match(text))
        )

    def dismiss(self) -> None:
        """The user said "stop": nothing is waiting to be sent any more."""
        self.pending = None

    def handle(self, text: str) -> str | None:
        """Return FRIDAY's reply if this utterance is about the agent, else None."""
        if _CANCEL.match(text):
            self.pending = None
            if not self.busy:
                return f"{self.name} isn't working on anything."
            assert self._job is not None
            self._job.cancel()
            return f"Okay, I've stopped {self.name}."
        if _FRESH.match(text):
            self.session_id = None
            self._save_session()
            return f"Okay, the next task starts a fresh {self.name} session."
        task = parse_task(text)
        if task is not None:
            if self.busy:
                return (
                    f"{self.name} is still working on the last task. "
                    f"Say cancel {self.name} if you want to stop it."
                )
            self.pending = task
            return self._read_back()
        if self.pending is None:
            return None
        plain = _plain(text)
        if _YES.match(plain):
            task, self.pending = self.pending, None
            self._job = asyncio.get_running_loop().create_task(self._run(task))
            return f"Sent. I'll tell you when {self.name} is done."
        if _NO.match(plain):
            self.pending = None
            return "Okay, I won't send it."
        again = _AGAIN.match(text)
        if again:  # said the whole command again: start over, do not pile up
            self.pending = again.group("task").strip()
            return self._read_back()
        if len(plain.split()) < 3:  # "cool", "I see": not part of a task
            return "Say yes to send it, or no to drop it."
        # Neither yes nor no: they were still dictating.
        self.pending = f"{self.pending} {text.strip()}"
        return self._read_back()

    def _read_back(self) -> str:
        return f"For {self.name}: {self.pending} Shall I send it?"

    async def _run(self, task: str) -> None:
        background = sent = ""
        if self.notes is not None:
            sent = self.notes.for_agent(fresh=False)
            if self.session_id is None:
                background = self.notes.for_agent(fresh=True)
            elif sent != self._notes_sent:
                background = sent
        try:
            result = await self.agent.run(task + background, self.session_id)
        except FridayError as exc:
            log.error("code agent failed: %s", exc)
            result = CodeResult(text=str(exc), ok=False)
        except asyncio.CancelledError:
            log.info("code agent task cancelled")
            raise
        if result.session_id:
            self.session_id = result.session_id
        elif not result.ok and "no conversation found" in result.text.lower():
            self.session_id = None  # the remembered chat is gone: start fresh next time
        self._save_session()
        if result.ok:
            self._notes_sent = sent
        path = self._write_log(task, result)
        summary = spoken_summary(result.text)
        if result.ok:
            spoken = f"{self.name} is done. {summary}"
        else:
            spoken = f"{self.name} ran into a problem. {summary}"
        if self.notes is not None:
            self.notes.log_work(task, summary, result.ok)
        if self.on_done is not None:
            try:
                self.on_done(Notice(task=task, spoken=spoken.strip(), ok=result.ok, log_path=path))
            except Exception:  # a broken listener must not lose the result
                log.exception("code agent listener failed")

    def _load_session(self) -> None:
        if self.state_path is None:
            return
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict) or saved.get("scope") != self.scope:
            return
        if self.keep_session == "day" and saved.get("day") != date.today().isoformat():
            return
        session = saved.get("session_id")
        if isinstance(session, str) and session:
            self.session_id = session

    def _save_session(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            if self.session_id is None:
                self.state_path.unlink(missing_ok=True)
                return
            # "day" is the day the chat started, so it ends at midnight.
            day = date.today().isoformat()
            try:
                old = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(old, dict) and old.get("session_id") == self.session_id:
                    day = str(old.get("day", day))
            except (OSError, ValueError):
                pass
            self.state_path.write_text(
                json.dumps({"session_id": self.session_id, "day": day, "scope": self.scope}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("could not remember the code agent session: %s", exc)

    def _write_log(self, task: str, result: CodeResult) -> Path | None:
        if self.log_dir is None:
            return None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.log_dir / f"{datetime.now():%Y%m%d-%H%M%S}.md"
            path.write_text(
                f"# Task\n\n{task}\n\n# Result ({'ok' if result.ok else 'failed'}, "
                f"{result.denied} actions refused by its permission settings)\n\n"
                f"{result.text}\n",
                encoding="utf-8",
            )
            return path
        except OSError as exc:
            log.warning("could not save the code agent log: %s", exc)
            return None
