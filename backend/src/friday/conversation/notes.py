"""Notes that outlive a session, kept as plain files the owner can read and edit.

FRIDAY's voice model starts every session blank; it only knows what is put in
its prompt. So memory here is deliberately simple and shared:

* ``memory.md``: one line per thing the owner asked her to remember ("Friday,
  remember that ..."). Stored word for word by fixed logic, never by the
  language model, so nothing is invented or silently reworded.
* ``worklog.md``: one line per task passed to Claude Code, with its outcome.

Both are shown to the voice model at the start of each session and passed to
Claude Code with a task, so the two share one memory. Other files (for example
a Claude Code ``CLAUDE.md``) can be read in as well.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_SEP = r"[\s,.:;!?-]+"
_LEAD = rf"^\W*(?:(?:hey|ok|okay){_SEP})?(?:friday{_SEP})?(?:please{_SEP})?"
_REMEMBER = re.compile(
    rf"{_LEAD}(?:(?:can|could|would){_SEP}you{_SEP})?(?:please{_SEP})?"
    rf"(?:remember{_SEP}(?:that|this)|make{_SEP}a{_SEP}note(?:{_SEP}(?:that|of{_SEP}this))?"
    rf"|note{_SEP}(?:that|this(?:{_SEP}down)?)|keep{_SEP}in{_SEP}mind{_SEP}that)"
    rf"{_SEP}(?P<fact>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_RECALL = re.compile(
    rf"{_LEAD}(?:what{_SEP}(?:do|did){_SEP}you{_SEP}(?:remember|have{_SEP}noted)"
    rf"|what(?:{_SEP}is|{_SEP}s| s|'s){_SEP}in{_SEP}your{_SEP}(?:memory|notes)"
    rf"|read{_SEP}(?:me{_SEP})?(?:back{_SEP})?your{_SEP}(?:memory|notes))"
    rf"(?:{_SEP}about{_SEP}me)?\W*$",
    re.IGNORECASE,
)
_FORGET = re.compile(
    rf"{_LEAD}(?:forget|delete|remove|scrap){_SEP}"
    rf"(?:that|(?:the|that|your){_SEP}last{_SEP}(?:note|one|thing))(?:{_SEP}(?:note|one))?\W*$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_STAMP = re.compile(r"^\[\d{4}-\d{2}-\d{2}\]\s*")

MAX_NOTE_CHARS = 300


def _one_line(text: str, limit: int = MAX_NOTE_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rsplit(" ", 1)[0] + " ..."


def _newest_that_fit(lines: list[str], budget: int) -> list[str]:
    kept: list[str] = []
    for line in reversed(lines):
        budget -= len(line) + 3
        if budget < 0:
            break
        kept.append(line)
    return kept[::-1]


class NoteBook:
    def __init__(
        self,
        folder: Path,
        *,
        user_name: str = "the owner",
        max_chars: int = 1200,
        also_read: tuple[Path, ...] = (),
        work_lines: int = 6,
        share: bool = True,
    ) -> None:
        self.folder = folder
        self.user_name = user_name
        self.max_chars = max_chars
        self.also_read = also_read
        self.work_lines = work_lines
        self.share = share  # pass the notes on to Claude Code with a task
        self.memory_path = folder / "memory.md"
        self.worklog_path = folder / "worklog.md"

    # ---- reading ---------------------------------------------------------

    def _bullets(self, path: Path) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return [m.group(1) for line in text.splitlines() if (m := _BULLET.match(line))]

    def facts(self) -> list[str]:
        return self._bullets(self.memory_path)

    def work(self) -> list[str]:
        return self._bullets(self.worklog_path)[-self.work_lines :]

    def _sections(self, work: bool = True) -> list[str]:
        parts: list[str] = []
        facts = _newest_that_fit(self.facts(), self.max_chars * 2 // 3)
        if facts:
            parts.append(
                f"Things {self.user_name} asked to have remembered, in their own words:\n"
                + "\n".join(f"- {f}" for f in facts)
            )
        recent = _newest_that_fit(self.work(), self.max_chars // 3) if work else []
        if recent:
            parts.append(
                "Recent tasks passed to Claude Code, oldest first:\n"
                + "\n".join(f"- {w}" for w in recent)
            )
        return parts

    def _files(self) -> list[str]:
        parts: list[str] = []
        for path in self.also_read:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                log.warning("memory file %s could not be read: %s", path, exc)
                continue
            if text:
                parts.append(f"From the file {path.name}:\n{text[: self.max_chars].rstrip()}")
        return parts

    def context(self) -> str | None:
        """What the voice model is told about earlier sessions."""
        parts = self._sections() + self._files()
        if not parts:
            return None
        return (
            "Notes kept from earlier sessions. They are all you know about anything "
            "before this conversation: use them naturally when they are relevant, do not "
            "recite them unasked, and never add to them from imagination.\n\n" + "\n\n".join(parts)
        )

    def for_agent(self, fresh: bool = True) -> str:
        """The same notes, worded for Claude Code. Empty if there are none.

        A chat that is being continued already knows its own earlier tasks, so
        the work log is only included for a ``fresh`` one.
        """
        parts = self._sections(work=fresh) if self.share else []
        if not parts:
            return ""
        return (
            "\n\n(Background from the voice assistant's memory file, for context only; "
            "it is not part of the task.\n" + "\n".join(parts) + ")"
        )

    # ---- writing ---------------------------------------------------------

    def _append(self, path: Path, title: str, line: str) -> bool:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            with path.open("a", encoding="utf-8") as handle:
                if new:
                    handle.write(f"# {title}\n\nOne note per line. Edit or delete freely.\n\n")
                handle.write(f"- [{date.today().isoformat()}] {line}\n")
            return True
        except OSError as exc:
            log.warning("could not write %s: %s", path, exc)
            return False

    def remember(self, fact: str) -> bool:
        return self._append(self.memory_path, "FRIDAY's memory", _one_line(fact))

    def log_work(self, task: str, outcome: str, ok: bool = True) -> None:
        result = _one_line(outcome, 200) or ("done" if ok else "failed")
        prefix = "" if ok else "FAILED: "
        self._append(
            self.worklog_path,
            "Tasks FRIDAY passed to Claude Code",
            f'"{_one_line(task, 160)}" -> {prefix}{result}',
        )

    def forget_last(self) -> str | None:
        try:
            lines = self.memory_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for index in range(len(lines) - 1, -1, -1):
            match = _BULLET.match(lines[index])
            if match:
                del lines[index]
                try:
                    self.memory_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                except OSError as exc:
                    log.warning("could not update %s: %s", self.memory_path, exc)
                    return None
                return _STAMP.sub("", match.group(1))
        return None

    # ---- spoken commands -------------------------------------------------

    def concerns(self, text: str) -> bool:
        return self._fact(text) is not None or bool(_RECALL.match(text) or _FORGET.match(text))

    @staticmethod
    def _fact(text: str) -> str | None:
        match = _REMEMBER.match(text.strip())
        if not match:
            return None
        fact = match.group("fact").strip()
        # "Remember that film we saw?" is a question, not something to store.
        return None if fact.endswith("?") else fact

    def handle(self, text: str) -> str | None:
        """FRIDAY's reply if this is a memory command, else None."""
        fact = self._fact(text)
        if fact is not None:
            if self.remember(fact):
                return f"Okay, I'll remember that. {_one_line(fact)}"
            return "Sorry, I couldn't save that note."
        if _FORGET.match(text):
            gone = self.forget_last()
            if gone is None:
                return "There's no note to forget."
            return f"Okay, forgotten. {gone}"
        if _RECALL.match(text):
            facts = [_STAMP.sub("", f).rstrip(".") for f in self.facts()]
            if not facts:
                return "I don't have any notes yet. Say remember that, and then the thing."
            latest = facts[-5:]
            lead = (
                f"I have {len(facts)} notes. The latest five."
                if len(facts) > 5
                else "Here's what I've noted."
            )
            return f"{lead} {'. '.join(latest)}."
        return None
