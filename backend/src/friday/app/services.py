"""What the chat app does, without the web parts: chats, pictures, music, voice.

The web server (``server.py``) turns browser messages into calls here, and
sends everything ``emit`` produces back to the browser.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from friday.app.store import NEW_TITLE, ConversationStore, StoredMessage
from friday.conversation.delegation import Notice
from friday.conversation.engine import ConversationEngine
from friday.conversation.studio import Kind, Request, StudioDelegate, parse_request
from friday.core.errors import FridayError
from friday.core.messages import Role
from friday.documents import DocumentError, category, extract_text, for_model
from friday.integrations.audio_tools import (
    AudioEdit,
    apply_edit,
    output_name,
    parse_audio_edit,
    wants_new_part,
)
from friday.integrations.song_ai import SongEditor, SongTask, parse_song_task
from friday.integrations.song_ai import output_path as song_output_path

log = logging.getLogger(__name__)

Event = dict[str, Any]
_SPOKEN = {"image": "picture", "music": "music", "audio_edit": "song"}
_MAX_ENGINES = 20  # conversations kept in memory; older ones are rebuilt from the store
_PICTURE_WORD = re.compile(r"\b(?:pictures?|images?|photos?|drawings?|paintings?)\b", re.I)
_SONG_MEMORY = 12  # a song attached within this many messages is "the song"
# Talk about the song that needs a true answer, not the talking model's guess.
_SONG_TALK = re.compile(
    r"\b(?:again|retry|redo|done|ready|finished|saved?|where|file|folder|location|"
    r"difference|different|changed?|work(?:ed|ing)?|lying|not\s+removed|still)\b",
    re.I,
)
_UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD_BYTES = 300 * 1024 * 1024
_NEW_PART_REPLY = (
    "Playing a new instrument part on top of a song isn't set up on this computer. "
    'I can clean it or change the bass: say "remove the noise", "add some bass" or "less bass".'
)
_SONG_REPLY = (
    "I've got the song. I can take out hiss and crackle, or make its bass stronger or "
    'softer: say "remove the noise", "add some bass", "a lot more bass" or "less bass".'
)
_SONG_AI_REPLY = (
    "I've got the song. What should I do with it? For example:\n"
    '- "remove the noise", "add some bass" or "less bass" (quick, a few seconds)\n'
    '- "add a funky bass line", "add drums", "add a piano part" (a new part played by AI)\n'
    '- "give me just the vocals" (pull one instrument out)\n'
    '- "make it a jazz version" (a new style)\n'
    '- "redo 0:30 to 0:45 with a guitar solo" (remake one stretch)\n'
    "The AI ones take a few minutes."
)


@dataclass(frozen=True, slots=True)
class Upload:
    id: str
    name: str
    category: str  # image, audio, document
    path: Path

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "category": self.category}


class VoiceControl(Protocol):
    """Voice mode (see ``voice.py``); a stand-in in tests."""

    state: str
    conversation_id: str | None

    @property
    def available(self) -> bool: ...

    async def start(
        self,
        engine: ConversationEngine,
        conversation_id: str,
        on_event: Callable[[object], None],
    ) -> None: ...

    async def stop(self) -> None: ...

    def announce(self, engine: ConversationEngine, text: str, context: str) -> None: ...


class AppServices:
    def __init__(
        self,
        store: ConversationStore,
        make_engine: Callable[[str], ConversationEngine],
        *,
        studio: StudioDelegate | None = None,
        voice: VoiceControl | None = None,
        models: list[dict[str, Any]] | None = None,
        uploads_dir: Path | None = None,
        audio_dir: Path | None = None,
        ffmpeg: str | None = None,
        can_see: bool = False,
        document_chars: int = 14_000,
        can_think: bool = False,
        song_ai: SongEditor | None = None,
    ) -> None:
        self.store = store
        self.uploads_dir = uploads_dir
        self.audio_dir = audio_dir or (uploads_dir.parent / "edited" if uploads_dir else None)
        self.ffmpeg = ffmpeg
        self.can_see = can_see
        self.can_think = can_think
        self.song_ai = song_ai
        self.document_chars = document_chars
        self._make_engine = make_engine
        self.studio = studio
        self.voice = voice
        self.models = models or []
        self._engines: dict[str, ConversationEngine] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[int, asyncio.Task[None]] = {}
        self._replies: dict[str, asyncio.Task[None]] = {}  # answers being written
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._voice_reply: list[str] = []
        self._voice_user_id: int | None = None
        if studio is not None:
            studio.open_results = False  # shown in the app instead
        if voice is not None and hasattr(voice, "on_stopped"):
            voice.on_stopped = self._voice_stopped
        for job in store.unfinished_jobs():
            store.update(
                job.id, meta={"status": "failed", "error": "The app was closed before it finished."}
            )

    # Events to the browser

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def emit(self, event: Event) -> None:
        for queue in self._subscribers:
            queue.put_nowait(event)

    def _emit_message(self, message: StoredMessage, event: str = "message") -> None:
        self.emit({"type": event, "message": message.to_json()})

    def status(self) -> Event:
        studio = self.studio
        return {
            "type": "status",
            "models": self.models,
            "can_make": sorted(k.value for k in studio.workflows) if studio else [],
            "voice_available": bool(self.voice and self.voice.available),
            "voice": self.voice.state if self.voice else "off",
            "voice_conversation": self.voice.conversation_id if self.voice else None,
            "can_attach": self.uploads_dir is not None,
            "can_think": self.can_think,
            "can_read": {
                "image": self.can_see,
                "document": True,
                "audio": bool(self.audio_dir and (self.ffmpeg or self.song_ai)),
            },
        }

    # Conversations

    def conversation_id(self, conversation_id: str | None) -> str:
        """The conversation to use: the given one, or a new one."""
        if conversation_id and self.store.get(conversation_id):
            return conversation_id
        conv = self.store.create()
        self.emit({"type": "conversation", "conversation": conv.to_json()})
        return conv.id

    def delete_conversation(self, conversation_id: str) -> None:
        self._engines.pop(conversation_id, None)
        self.store.delete(conversation_id)
        self.emit({"type": "conversation_deleted", "id": conversation_id})

    def rename(self, conversation_id: str, title: str) -> None:
        title = " ".join(title.split())[:80] or NEW_TITLE
        self.store.rename(conversation_id, title)
        conv = self.store.get(conversation_id)
        if conv:
            self.emit({"type": "conversation", "conversation": conv.to_json()})

    def _title_from(self, conversation_id: str, text: str) -> None:
        conv = self.store.get(conversation_id)
        if conv and conv.title == NEW_TITLE and text.strip():
            words = " ".join(text.split())
            self.rename(conversation_id, words if len(words) <= 50 else words[:47] + "...")

    def engine(self, conversation_id: str) -> ConversationEngine:
        engine = self._engines.pop(conversation_id, None)
        if engine is None:
            engine = self._make_engine(conversation_id)
            engine.app_note = self._files_note()
            _load_history(engine, self.store.messages(conversation_id), self._history_text)
        self._engines[conversation_id] = engine  # newest last
        while len(self._engines) > _MAX_ENGINES:
            oldest = next(iter(self._engines))
            if self.voice and self.voice.conversation_id == oldest:
                break
            del self._engines[oldest]
        return engine

    def _files_note(self) -> str:
        where = []
        if self.studio is not None:
            where.append(f"pictures it makes are saved in {_home(self.studio.output_dir)}")
        if self.audio_dir is not None:
            where.append(f"music and changed songs are saved in {_home(self.audio_dir)}")
        return (
            "You cannot open, change, clean, convert or save files yourself. Songs and "
            "pictures are changed only by the app's own tools, which show a card with the "
            "new file in this chat. Never say you are processing, trying again, have "
            "changed or have saved a file, and never guess a file's name or folder: "
            "nothing happened unless a card shows it. If they want a song changed, "
            'tell them to attach it and say what to change ("remove the noise", '
            '"more bass", "add drums"). '
            + (f"On this computer, {'; '.join(where)}." if where else "")
        )

    def _recent_song(self, cid: str) -> Upload | None:
        """The latest version of the song in this chat (within the last few turns):
        the newest changed copy, or else the song as attached. So "remove the
        noise" after "more bass" keeps the bass."""
        for message in reversed(self.store.messages(cid)[-_SONG_MEMORY:]):
            if (
                message.kind == "job"
                and message.meta.get("kind") == "audio_edit"
                and message.meta.get("status") == "done"
                and message.file
                and Path(message.file).is_file()
            ):
                path = Path(message.file)
                return Upload(f"job{message.id}", path.name, "audio", path)
            for f in reversed(message.meta.get("files") or []):
                if f.get("category") == "audio":
                    try:
                        return self.upload(str(f["id"]))
                    except FridayError:
                        return None
        return None

    def _song_status(self, cid: str) -> str:
        """A true answer to "is it done?" / "where is it?" about the song."""
        jobs = [
            m
            for m in self.store.messages(cid)
            if m.kind == "job" and m.meta.get("kind") == "audio_edit"
        ]
        last = jobs[-1] if jobs else None
        how = (
            'Say what to change, for example "remove the noise", "a lot more bass", '
            '"add drums" or "make it a jazz version", and I\'ll use the same song.'
        )
        if last is None:
            return f"I haven't changed the song yet. {how}"
        status = last.meta.get("status")
        if status in ("running", "waiting"):
            return f"I'm still working on it ({last.content}). The card above shows when it's done."
        if status == "done" and last.file:
            return (
                f"Nothing new has been changed since the last card. The last changed song "
                f"({last.content}) is saved here:\n\n`{_home(Path(last.file))}`\n\n"
                f"You can also play or download it from its card above. {how}"
            )
        return f"The last change didn't work: {last.meta.get('error') or 'no reason given'}. {how}"

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._turn_locks.setdefault(conversation_id, asyncio.Lock())

    # Chat

    async def chat(
        self,
        conversation_id: str | None,
        text: str,
        files: Sequence[str] = (),
        think: bool | None = None,
    ) -> str:
        """``think``: let the talking model think before it answers (None: as set up)."""
        text = text.strip()
        uploads = [self.upload(f) for f in files]
        cid = self.conversation_id(conversation_id)
        if not text and not uploads:
            return cid
        if uploads:
            return await self._chat_with_files(cid, text, uploads, think)
        song = self._recent_song(cid)
        if song is not None and _is_song_change(text, self.song_ai is not None):
            # "remove the noise", "add drums" after a song: the same song again.
            return await self._chat_with_files(cid, text, [song], think)
        if song is not None and _SONG_TALK.search(text) and not _PICTURE_WORD.search(text):
            # "is it done?", "where is it saved?", "try again": answered from what
            # really happened, not by the talking model (which once made it up).
            self._title_from(cid, text)
            self._emit_message(self.store.add(cid, "user", "text", text))
            self._say(cid, text, self._song_status(cid))
            return cid
        self._title_from(cid, text)
        self._emit_message(self.store.add(cid, "user", "text", text))
        request = parse_request(text) if self.studio else None
        if request is not None and self.studio and request.kind in self.studio.workflows:
            # Offer it with buttons; nothing starts until the owner presses Make.
            offer = self.store.add(
                cid,
                "assistant",
                "proposal",
                request.prompt,
                meta={"kind": request.kind.value, "status": "pending"},
            )
            self._emit_message(offer)
            self._remember(cid, text, f"(offered to make the {_SPOKEN[request.kind.value]})")
            return cid
        await self._stream_reply(cid, text, think=think)
        return cid

    async def _stream_reply(
        self, cid: str, text: str, images: Sequence[str] = (), think: bool | None = None
    ) -> None:
        async with self._lock(cid):
            engine = self.engine(cid)
            engine.think = think if self.can_think else None
            draft = f"d{uuid.uuid4().hex[:12]}"
            self.emit({"type": "stream_start", "conversation_id": cid, "draft": draft})

            async def write() -> None:
                async with contextlib.aclosing(engine.respond(text, images)) as deltas:
                    async for delta in deltas:
                        self.emit({"type": "delta", "draft": draft, "text": delta})

            writing = asyncio.get_running_loop().create_task(write())
            self._replies[cid] = writing
            stopped = False
            try:
                await writing
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # the app itself is stopping, not the Stop button
                stopped = True
            finally:
                self._replies.pop(cid, None)
            turn = engine.last_turn
            if stopped and not turn.full_text:
                turn.full_text = "(stopped)"
            meta: dict[str, Any] = {"mode": turn.mode.value}
            if stopped:
                meta["stopped"] = True
            if turn.provider is not None:
                meta["provider"] = turn.provider.name
                meta["model"] = turn.provider.model
            if turn.failed:
                meta["failed"] = True
            reply = self.store.add(cid, "assistant", "text", turn.full_text, meta=meta)
            self.emit({"type": "stream_end", "draft": draft, "message": reply.to_json()})

    def _say(self, cid: str, said: str, reply: str) -> None:
        """A fixed reply, without asking the talking model."""
        self._emit_message(self.store.add(cid, "assistant", "text", reply))
        self._remember(cid, said, reply)

    async def _chat_with_files(
        self, cid: str, text: str, uploads: list[Upload], think: bool | None = None
    ) -> str:
        kinds = {u.category for u in uploads}
        if not text:
            text = (
                "What's in this picture?"
                if kinds == {"image"}
                else "Summarise this document."
                if kinds == {"document"}
                else ""
            )
        self._title_from(cid, text or uploads[0].name)
        # A changed copy from an earlier card is not an upload: it is named, not attached.
        attached = [u.to_json() for u in uploads if not u.id.startswith("job")]
        message = self.store.add(cid, "user", "text", text, meta={"files": attached})
        self._emit_message(message)
        said = self._history_text(message, read_documents=False)

        songs = [u for u in uploads if u.category == "audio"]
        if songs:
            edit = parse_audio_edit(text)
            task = None if edit else parse_song_task(text)
            if task is not None and self.song_ai is not None and self.audio_dir:
                for song in songs:
                    self._start_song_job(cid, song, task)
                self._remember(cid, said, f"(changing the song with AI: {task.describe})")
            elif edit is None:
                if self.song_ai is not None:
                    reply = _SONG_AI_REPLY
                elif task is not None or wants_new_part(text):
                    reply = _NEW_PART_REPLY
                else:
                    reply = _SONG_REPLY
                self._say(cid, said, reply)
            elif not (self.ffmpeg and self.audio_dir):
                self._say(cid, said, "Changing songs needs ffmpeg, and it isn't installed.")
            else:
                for song in songs:
                    self._start_audio_job(cid, song, edit)
                self._remember(cid, said, f"(changing the song: {edit.describe})")
            return cid

        try:
            said = await asyncio.to_thread(self._history_text, message)
        except DocumentError as exc:
            self._say(cid, said, str(exc))
            return cid
        images: list[str] = []
        for picture in (u for u in uploads if u.category == "image"):
            data = await asyncio.to_thread(picture.path.read_bytes)
            images.append(base64.b64encode(data).decode("ascii"))
        await self._stream_reply(cid, said, images, think)
        return cid

    def notice(self, conversation_id: str, text: str, context: str) -> None:
        """FRIDAY says something herself (Claude Code finished a task)."""
        if self.store.get(conversation_id) is None:
            return
        self._emit_message(self.store.add(conversation_id, "assistant", "text", text))
        self._remember(conversation_id, context, text)

    def _remember(self, conversation_id: str, said: str, answer: str) -> None:
        engine = self._engines.get(conversation_id)
        if engine is not None:
            engine.record_notice(answer, said)

    # Pictures and music

    def dismiss(self, message_id: int) -> None:
        message = self.store.message(message_id)
        if message and message.kind == "proposal" and message.meta.get("status") == "pending":
            self._emit_message(
                self.store.update(message_id, meta={"status": "dismissed"}), "message_update"
            )

    def make(
        self,
        conversation_id: str | None,
        kind: str,
        prompt: str,
        proposal_id: int | None = None,
    ) -> int:
        """Start making a picture or music. Returns the job's message id."""
        studio = self.studio
        prompt = " ".join(prompt.split())
        if studio is None or Kind(kind) not in studio.workflows:
            raise FridayError(f"making {_SPOKEN.get(kind, kind)} is not set up")
        if not prompt:
            raise FridayError(f"say what the {_SPOKEN[kind]} should be")
        cid = self.conversation_id(conversation_id)
        if proposal_id is not None:
            offer = self.store.message(proposal_id)
            if offer is None or offer.kind != "proposal" or offer.conversation_id != cid:
                raise FridayError("that offer is not in this conversation")
            if offer.meta.get("status") != "pending":
                raise FridayError("that offer was already answered")
            self._emit_message(
                self.store.update(proposal_id, content=prompt, meta={"status": "accepted"}),
                "message_update",
            )
        else:
            self._title_from(cid, prompt)
            self._emit_message(self.store.add(cid, "user", "text", prompt, meta={"make": kind}))
        job = self.store.add(
            cid,
            "assistant",
            "job",
            prompt,
            meta={
                "kind": kind,
                "status": "waiting" if studio.queued_or_running else "running",
            },
        )
        self._emit_message(job)
        task = asyncio.get_running_loop().create_task(self._run_job(job.id, Kind(kind), prompt))
        self._jobs[job.id] = task
        task.add_done_callback(lambda _: self._jobs.pop(job.id, None))
        return job.id

    async def _run_job(self, job_id: int, kind: Kind, prompt: str) -> None:
        assert self.studio is not None
        loop = asyncio.get_running_loop()
        begun = loop.time()

        def started() -> None:
            nonlocal begun
            begun = loop.time()
            self._emit_message(
                self.store.update(job_id, meta={"status": "running"}), "message_update"
            )

        try:
            notice = await self.studio.make(Request(kind, prompt), on_start=started)
        except asyncio.CancelledError:
            self._emit_message(
                self.store.update(job_id, meta={"status": "cancelled"}), "message_update"
            )
            raise
        except Exception as exc:  # never leave a job spinning
            log.exception("job failed")
            notice = Notice(task=prompt, spoken=f"Something went wrong: {exc}", ok=False)
        self._finish_job(job_id, kind, prompt, notice, loop.time() - begun)

    def _finish_job(
        self, job_id: int, kind: Kind, prompt: str, notice: Notice, seconds: float
    ) -> StoredMessage:
        spoken = _SPOKEN[kind.value]
        if notice.ok and notice.log_path is not None:
            done = self.store.update(
                job_id,
                file=str(notice.log_path),
                meta={"status": "done", "seconds": round(seconds)},
            )
            said = f"I made the {spoken}: {prompt}."
        else:
            done = self.store.update(job_id, meta={"status": "failed", "error": notice.spoken})
            said = notice.spoken
        self._emit_message(done, "message_update")
        self._remember(done.conversation_id, f"(asked for {spoken}: {prompt})", said)
        return done

    def stop_reply(self, cid: str) -> bool:
        """The Stop button: end the answer being written now, keeping what's there."""
        writing = self._replies.get(cid)
        if writing is None or writing.done():
            return False
        writing.cancel()
        return True

    def cancel_job(self, job_id: int) -> bool:
        task = self._jobs.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

    # Attached files

    async def save_upload(self, name: str, chunks: AsyncIterator[bytes]) -> Upload:
        """Keep a file the owner attached, until a message uses it."""
        if self.uploads_dir is None:
            raise FridayError("attaching files is not set up")
        name = _safe_name(name)
        kind = category(name)
        if kind is None:
            raise FridayError(
                f"I can't use {Path(name).suffix or 'this kind of'} files. Pictures, songs "
                "(mp3, wav, flac ...) and documents (pdf, docx, xlsx, txt ...) work."
            )
        upload_id = uuid.uuid4().hex
        folder = self.uploads_dir / upload_id
        folder.mkdir(parents=True)
        path = folder / name
        size = 0
        try:
            with path.open("wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise FridayError(
                            f"{name} is too big (over {MAX_UPLOAD_BYTES // 2**20} MB)."
                        )
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise
        if size == 0:
            path.unlink()
            folder.rmdir()
            raise FridayError(f"{name} is empty.")
        return Upload(upload_id, name, kind, path)

    def upload(self, upload_id: str) -> Upload:
        path = self.upload_path(upload_id)
        if path is None:
            raise FridayError("that attached file is gone; please attach it again")
        kind = category(path.name)
        assert kind is not None
        return Upload(upload_id, path.name, kind, path)

    def upload_path(self, upload_id: str) -> Path | None:
        if self.uploads_dir is None or not _UPLOAD_ID.match(upload_id):
            return None
        folder = self.uploads_dir / upload_id
        files = (
            [p for p in folder.iterdir() if p.is_file() and p.name != _TEXT_CACHE]
            if (folder.is_dir())
            else []
        )
        return files[0] if files else None

    def _start_audio_job(self, cid: str, song: Upload, edit: AudioEdit) -> int:
        job = self.store.add(
            cid,
            "assistant",
            "job",
            f"{song.name}: {edit.describe}",
            meta={"kind": "audio_edit", "status": "running", "source": song.id},
        )
        self._emit_message(job)
        task = asyncio.get_running_loop().create_task(self._run_audio_job(job.id, song, edit))
        self._jobs[job.id] = task
        task.add_done_callback(lambda _: self._jobs.pop(job.id, None))
        return job.id

    async def _run_audio_job(self, job_id: int, song: Upload, edit: AudioEdit) -> None:
        assert self.ffmpeg and self.audio_dir
        loop = asyncio.get_running_loop()
        begun = loop.time()
        try:
            target = output_name(song.path, edit, self.audio_dir)
            await apply_edit(self.ffmpeg, song.path, target, edit)
        except asyncio.CancelledError:
            self._emit_message(
                self.store.update(job_id, meta={"status": "cancelled"}), "message_update"
            )
            raise
        except Exception as exc:  # never leave a job spinning
            log.exception("song change failed")
            done = self.store.update(job_id, meta={"status": "failed", "error": str(exc)})
        else:
            done = self.store.update(
                job_id,
                file=str(target),
                meta={"status": "done", "seconds": round(loop.time() - begun)},
            )
        self._emit_message(done, "message_update")
        self._remember(done.conversation_id, "(changing the song)", self._history_text(done))

    def _start_song_job(self, cid: str, song: Upload, task: SongTask) -> int:
        busy = self.studio is not None and self.studio.queued_or_running
        job = self.store.add(
            cid,
            "assistant",
            "job",
            f"{song.name}: {task.describe}",
            meta={
                "kind": "audio_edit",
                "ai": task.task,
                "status": "waiting" if busy else "running",
                "source": song.id,
            },
        )
        self._emit_message(job)
        run = self._run_song_job(job.id, song, task)
        handle = asyncio.get_running_loop().create_task(run)
        self._jobs[job.id] = handle
        handle.add_done_callback(lambda _: self._jobs.pop(job.id, None))
        return job.id

    async def _run_song_job(self, job_id: int, song: Upload, task: SongTask) -> None:
        assert self.song_ai is not None and self.audio_dir is not None
        editor, folder = self.song_ai, self.audio_dir
        loop = asyncio.get_running_loop()
        begun = loop.time()

        def started() -> None:
            nonlocal begun
            begun = loop.time()
            self._emit_message(
                self.store.update(job_id, meta={"status": "running"}), "message_update"
            )

        async def work() -> Path:
            return await editor.run(song.path, task, song_output_path(song.path, task, folder))

        try:
            if self.studio is not None:
                target = await self.studio.exclusive(work, on_start=started)
            else:
                target = await work()
        except asyncio.CancelledError:
            self._emit_message(
                self.store.update(job_id, meta={"status": "cancelled"}), "message_update"
            )
            raise
        except Exception as exc:  # never leave a job spinning
            log.exception("AI song change failed")
            done = self.store.update(job_id, meta={"status": "failed", "error": str(exc)})
        else:
            done = self.store.update(
                job_id,
                file=str(target),
                meta={"status": "done", "seconds": round(loop.time() - begun)},
            )
        self._emit_message(done, "message_update")
        self._remember(done.conversation_id, "(changing the song)", self._history_text(done))

    def _history_text(self, message: StoredMessage, read_documents: bool = True) -> str:
        """How a saved message reads to the talking model."""
        files = message.meta.get("files") if message.role == "user" else None
        if not files:
            return _history_text(message)
        parts = [message.content] if message.content else []
        for item in files:
            name, kind = str(item.get("name")), item.get("category")
            if kind == "image":
                parts.append(f"[picture attached: {name}]")
            elif kind == "audio":
                parts.append(f"[song attached: {name}]")
            elif kind == "document":
                text = self._document_text(str(item.get("id"))) if read_documents else None
                if text is None:
                    parts.append(f"[document attached: {name}]")
                else:
                    parts.append(for_model(name, text, self.document_chars)[0])
        return "\n\n".join(parts)

    def _document_text(self, upload_id: str) -> str | None:
        """The document's text, read once and kept beside it."""
        path = self.upload_path(upload_id)
        if path is None:
            return None
        cache = path.parent / _TEXT_CACHE
        if cache.is_file():
            return cache.read_text(encoding="utf-8")
        text = extract_text(path)
        cache.write_text(text, encoding="utf-8")
        return text

    # Voice

    async def set_voice(self, on: bool, conversation_id: str | None) -> str | None:
        voice = self.voice
        if voice is None or not voice.available:
            raise FridayError("voice mode is not set up")
        if not on:
            await voice.stop()
            if self.studio is not None:
                self.studio.on_done = None
            self.emit(self.status())
            return None
        cid = self.conversation_id(conversation_id)
        if voice.state != "off":
            if voice.conversation_id == cid:
                return cid
            await voice.stop()
        engine = self.engine(cid)
        self.emit({**self.status(), "voice": "starting", "voice_conversation": cid})
        try:
            await voice.start(engine, cid, lambda event: self._on_voice_event(cid, event))
        except Exception:
            self.emit(self.status())
            raise
        if self.studio is not None:
            self.studio.on_done = lambda notice: self._voice_job_done(cid, notice)
        self.emit(self.status())
        return cid

    def _voice_stopped(self, error: str | None) -> None:
        """Voice mode ended by itself (for example, the microphone went away)."""
        if self.studio is not None:
            self.studio.on_done = None
        self.emit(self.status())
        if error:
            self.emit({"type": "error", "message": error})

    def _on_voice_event(self, conversation_id: str, event: object) -> None:
        from friday.voice import session as vs

        if isinstance(event, vs.UserSaid):
            self._title_from(conversation_id, event.text)
            said = self.store.add(conversation_id, "user", "text", event.text, meta={"voice": True})
            self._voice_user_id = said.id
            self._voice_reply = []
            self._emit_message(said)
        elif isinstance(event, vs.AssistantSaid):
            self._voice_reply.append(event.text)
            self.emit({"type": "voice_partial", "text": " ".join(self._voice_reply)})
        elif isinstance(event, vs.Interrupted) and event.reason == "merge":
            # The owner was still talking: the whole thought comes again as one.
            if self._voice_user_id is not None:
                self.store.delete_message(self._voice_user_id)
                self.emit({"type": "message_deleted", "id": self._voice_user_id})
            self._voice_user_id = None
            self._voice_reply = []
        elif isinstance(event, vs.Interrupted):
            heard = event.heard.strip()
            self._voice_reply = [f"{heard} …"] if heard else []
        elif isinstance(event, vs.TurnFinished):
            text = " ".join(self._voice_reply).strip()
            self._voice_reply = []
            self._voice_user_id = None
            if text:
                meta: dict[str, Any] = {"voice": True}
                if event.metrics.provider:
                    meta["provider"] = event.metrics.provider
                self._emit_message(
                    self.store.add(conversation_id, "assistant", "text", text, meta=meta)
                )
            self.emit({"type": "voice_partial", "text": ""})
        elif isinstance(event, vs.StateChanged):
            self.emit({"type": "voice_state", "state": event.state.value})
        elif isinstance(event, vs.VoiceErrorEvent):
            self.emit({"type": "error", "message": event.message})

    def _voice_job_done(self, conversation_id: str, notice: Notice) -> None:
        """A picture or music asked for by voice is finished."""
        kind = Kind.MUSIC if notice.log_path and notice.log_path.suffix in _AUDIO else Kind.IMAGE
        job = self.store.add(
            conversation_id,
            "assistant",
            "job",
            notice.task,
            meta={"kind": kind.value, "status": "running", "voice": True},
        )
        self._finish_job(job.id, kind, notice.task, notice, 0)
        if self.voice is not None:
            engine = self._engines.get(conversation_id)
            if engine is not None:
                self.voice.announce(engine, notice.spoken, f"(asked for: {notice.task})")

    # Files

    def file_for(self, message_id: int) -> Path | None:
        message = self.store.message(message_id)
        if message is None or not message.file:
            return None
        path = Path(message.file)
        return path if path.is_file() else None

    async def aclose(self) -> None:
        for task in list(self._jobs.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.voice is not None:
            with contextlib.suppress(Exception):
                await self.voice.stop()
        if self.studio is not None:
            await self.studio.aclose()
        self.store.close()


_AUDIO = {".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a"}
_TEXT_CACHE = ".text"


def _safe_name(name: str) -> str:
    """A file name that can't point outside its folder."""
    name = Path(name.replace("\\", "/")).name.strip()
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", name).strip(". ")
    return name[:120] or "file"


def _history_text(message: StoredMessage) -> str:
    """How a saved message reads to the talking model."""
    kind = message.meta.get("kind", "image")
    spoken = _SPOKEN.get(kind, "picture")
    if message.kind == "proposal":
        return f"(offered to make the {spoken})"
    if message.kind == "job" and kind == "audio_edit":
        status = message.meta.get("status")
        if status == "done":
            return f"I changed the song ({message.content}). It's in the chat."
        if status == "failed":
            return str(message.meta.get("error") or "I couldn't change the song.")
        return f"(changing the song: {message.content})"
    if message.kind == "job":
        status = message.meta.get("status")
        if status == "done":
            return f"I made the {spoken}: {message.content}."
        if status == "failed":
            return str(message.meta.get("error") or f"I couldn't make the {spoken}.")
        if status == "cancelled":
            return f"(the {spoken} was cancelled)"
        return f"(making the {spoken}: {message.content})"
    return message.content


def _load_history(
    engine: ConversationEngine,
    messages: list[StoredMessage],
    as_text: Callable[[StoredMessage], str] = _history_text,
) -> None:
    """Rebuild a conversation's short-term memory from what was saved."""
    window = engine.window
    for message in messages:
        try:
            text = as_text(message).strip()
        except DocumentError:
            text = message.content.strip()
        if not text:
            continue
        if message.role == "user":
            window.add_user(text)
            continue
        if not window.messages or window.messages[-1].role is not Role.USER:
            window.add_user("(no message)")
        window.add_assistant(text)


def _is_song_change(text: str, ai: bool) -> bool:
    """Words that ask for a change to the song (the quick ones, or the AI ones)."""
    if parse_audio_edit(text) is not None:
        return True
    return ai and parse_song_task(text) is not None


def _home(path: Path) -> str:
    """A path as the owner would type it: ~/Music/FRIDAY rather than /home/name/..."""
    try:
        return "~/" + path.expanduser().relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)
