"""What the chat app does, without the web parts: chats, pictures, music, voice.

The web server (``server.py``) turns browser messages into calls here, and
sends everything ``emit`` produces back to the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from friday.app.store import NEW_TITLE, ConversationStore, StoredMessage
from friday.conversation.delegation import Notice
from friday.conversation.engine import ConversationEngine
from friday.conversation.studio import Kind, Request, StudioDelegate, parse_request
from friday.core.errors import FridayError
from friday.core.messages import Role

log = logging.getLogger(__name__)

Event = dict[str, Any]
_SPOKEN = {"image": "picture", "music": "music"}
_MAX_ENGINES = 20  # conversations kept in memory; older ones are rebuilt from the store


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
    ) -> None:
        self.store = store
        self._make_engine = make_engine
        self.studio = studio
        self.voice = voice
        self.models = models or []
        self._engines: dict[str, ConversationEngine] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[int, asyncio.Task[None]] = {}
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
            _load_history(engine, self.store.messages(conversation_id))
        self._engines[conversation_id] = engine  # newest last
        while len(self._engines) > _MAX_ENGINES:
            oldest = next(iter(self._engines))
            if self.voice and self.voice.conversation_id == oldest:
                break
            del self._engines[oldest]
        return engine

    def _lock(self, conversation_id: str) -> asyncio.Lock:
        return self._turn_locks.setdefault(conversation_id, asyncio.Lock())

    # Chat

    async def chat(self, conversation_id: str | None, text: str) -> str:
        text = text.strip()
        cid = self.conversation_id(conversation_id)
        if not text:
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
        async with self._lock(cid):
            engine = self.engine(cid)
            draft = f"d{id(text)}"
            self.emit({"type": "stream_start", "conversation_id": cid, "draft": draft})
            async for delta in engine.respond(text):
                self.emit({"type": "delta", "draft": draft, "text": delta})
            turn = engine.last_turn
            meta: dict[str, Any] = {"mode": turn.mode.value}
            if turn.provider is not None:
                meta["provider"] = turn.provider.name
                meta["model"] = turn.provider.model
            if turn.failed:
                meta["failed"] = True
            reply = self.store.add(cid, "assistant", "text", turn.full_text, meta=meta)
            self.emit({"type": "stream_end", "draft": draft, "message": reply.to_json()})
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

    def cancel_job(self, job_id: int) -> bool:
        task = self._jobs.get(job_id)
        if task is None:
            return False
        task.cancel()
        return True

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


def _history_text(message: StoredMessage) -> str:
    """How a saved message reads to the talking model."""
    kind = message.meta.get("kind", "image")
    spoken = _SPOKEN.get(kind, "picture")
    if message.kind == "proposal":
        return f"(offered to make the {spoken})"
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


def _load_history(engine: ConversationEngine, messages: list[StoredMessage]) -> None:
    """Rebuild a conversation's short-term memory from what was saved."""
    window = engine.window
    for message in messages:
        text = _history_text(message).strip()
        if not text:
            continue
        if message.role == "user":
            window.add_user(text)
            continue
        if not window.messages or window.messages[-1].role is not Role.USER:
            window.add_user("(no message)")
        window.add_assistant(text)
