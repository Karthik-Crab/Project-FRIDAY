"""Saved conversations: one SQLite file, so chats survive closing the app.

Each message has a ``kind``:

- ``text``: an ordinary chat message.
- ``proposal``: FRIDAY offers to make a picture or music (shown with buttons);
  ``meta.status`` is ``pending``, ``accepted`` or ``dismissed``.
- ``job``: a picture or music being made, or finished: ``meta.status`` is
  ``waiting``, ``running``, ``done``, ``failed`` or ``cancelled``; ``file`` is
  the saved file when done.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    incognito INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    file TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
"""

NEW_TITLE = "New chat"


@dataclass(slots=True)
class Conversation:
    id: str
    title: str
    created: float
    updated: float
    # Kept out of FRIDAY's memory; deleting it removes every trace (see services).
    incognito: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class StoredMessage:
    id: int
    conversation_id: str
    role: str  # "user" or "assistant"
    kind: str
    content: str
    file: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created: float = 0.0

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("file")
        data["file_name"] = Path(self.file).name if self.file else None
        return data


class ConversationStore:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        # Deleted chats are overwritten with zeros, not just marked free.
        self._db.execute("PRAGMA secure_delete = ON")
        self._db.executescript(_SCHEMA)
        columns = {r[1] for r in self._db.execute("PRAGMA table_info(conversations)")}
        if "incognito" not in columns:  # a database from before incognito chats
            with self._db:
                self._db.execute(
                    "ALTER TABLE conversations ADD COLUMN incognito INTEGER NOT NULL DEFAULT 0"
                )
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # Conversations

    def create(self, title: str = NEW_TITLE, incognito: bool = False) -> Conversation:
        now = time.time()
        conv = Conversation(uuid.uuid4().hex, title, now, now, incognito)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO conversations (id, title, created, updated, incognito)"
                " VALUES (?, ?, ?, ?, ?)",
                (conv.id, conv.title, conv.created, conv.updated, int(incognito)),
            )
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        return _conversation(row) if row else None

    def conversations(self) -> list[Conversation]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM conversations ORDER BY updated DESC").fetchall()
        return [_conversation(r) for r in rows]

    def rename(self, conversation_id: str, title: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
            )

    def delete(self, conversation_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    # Messages

    def add(
        self,
        conversation_id: str,
        role: str,
        kind: str,
        content: str,
        *,
        file: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> StoredMessage:
        now = time.time()
        meta = meta or {}
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO messages (conversation_id, role, kind, content, file, meta, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, role, kind, content, file, json.dumps(meta), now),
            )
            self._db.execute(
                "UPDATE conversations SET updated = ? WHERE id = ?", (now, conversation_id)
            )
        assert cur.lastrowid is not None
        return StoredMessage(cur.lastrowid, conversation_id, role, kind, content, file, meta, now)

    def update(
        self,
        message_id: int,
        *,
        kind: str | None = None,
        content: str | None = None,
        file: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> StoredMessage:
        """Change a message (a job finishing). ``meta`` is merged in."""
        current = self.message(message_id)
        if current is None:
            raise KeyError(message_id)
        if kind is not None:
            current.kind = kind
        if content is not None:
            current.content = content
        if file is not None:
            current.file = file
        if meta:
            current.meta = {**current.meta, **meta}
        with self._lock, self._db:
            self._db.execute(
                "UPDATE messages SET kind = ?, content = ?, file = ?, meta = ? WHERE id = ?",
                (current.kind, current.content, current.file, json.dumps(current.meta), message_id),
            )
        return current

    def delete_message(self, message_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    def message(self, message_id: int) -> StoredMessage | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def messages(self, conversation_id: str) -> list[StoredMessage]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conversation_id,)
            ).fetchall()
        return [_message(r) for r in rows]

    def unfinished_jobs(self) -> list[StoredMessage]:
        """Jobs that were still going when the app last stopped."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE kind = 'job'").fetchall()
        return [m for m in map(_message, rows) if m.meta.get("status") in ("waiting", "running")]


def _message(row: sqlite3.Row) -> StoredMessage:
    data = dict(row)
    data["meta"] = json.loads(data["meta"] or "{}")
    return StoredMessage(**data)


def _conversation(row: sqlite3.Row) -> Conversation:
    data = dict(row)
    data["incognito"] = bool(data.get("incognito"))
    return Conversation(**data)
