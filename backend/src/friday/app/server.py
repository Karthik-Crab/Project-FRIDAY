"""The chat app's web server: the page, saved chats, files, and a live channel.

It listens on 127.0.0.1 only. Requests whose Host or Origin is not this
machine's address are refused, so a web page on the internet cannot drive the
app through the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from friday.app.services import AppServices
from friday.core.errors import FridayError
from friday.documents import category

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _allowed(host_header: str | None, port: int | None) -> set[str]:
    hosts = set(_LOCAL_HOSTS)
    if port is not None:
        hosts |= {f"{h}:{port}" for h in _LOCAL_HOSTS}
    return hosts


def _local_origin(origin: str | None, hosts: set[str]) -> bool:
    if origin is None:
        return True  # not from a browser page
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            return origin[len(scheme) :] in hosts
    return False


class _LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app: Any, port: int | None) -> None:
        super().__init__(app)
        self.port = port

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        hosts = _allowed(request.headers.get("host"), self.port)
        if request.headers.get("host") not in hosts:
            return JSONResponse({"error": "not this machine"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not _local_origin(
            request.headers.get("origin"), hosts
        ):
            return JSONResponse({"error": "not this machine"}, status_code=403)
        return await call_next(request)


def create_app(services: AppServices, *, port: int | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        await services.aclose()

    app = FastAPI(title="FRIDAY", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(_LocalOnly, port=port)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return services.status()

    @app.get("/api/conversations")
    async def conversations() -> list[dict[str, Any]]:
        return [c.to_json() for c in services.store.conversations()]

    @app.post("/api/conversations")
    async def new_conversation() -> dict[str, Any]:
        cid = services.conversation_id(None)
        conv = services.store.get(cid)
        assert conv is not None
        return conv.to_json()

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str) -> dict[str, Any]:
        conv = services.store.get(conversation_id)
        if conv is None:
            raise HTTPException(404, "no such conversation")
        return {
            "conversation": conv.to_json(),
            "messages": [m.to_json() for m in services.store.messages(conversation_id)],
        }

    @app.patch("/api/conversations/{conversation_id}")
    async def rename(conversation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if services.store.get(conversation_id) is None:
            raise HTTPException(404, "no such conversation")
        services.rename(conversation_id, str(body.get("title", "")))
        return {"ok": True}

    @app.delete("/api/conversations/{conversation_id}")
    async def delete(conversation_id: str) -> dict[str, Any]:
        services.delete_conversation(conversation_id)
        return {"ok": True}

    @app.post("/api/upload")
    async def upload(request: Request, name: str) -> dict[str, Any]:
        """A file attached in the chat box. The body is the file itself."""
        try:
            saved = await services.save_upload(name, request.stream())
        except FridayError as exc:
            raise HTTPException(400, str(exc)) from exc
        return saved.to_json()

    @app.get("/uploads/{upload_id}")
    async def uploaded(upload_id: str) -> FileResponse:
        path = services.upload_path(upload_id)
        if path is None:
            raise HTTPException(404, "no such file")
        headers = {"Cache-Control": "max-age=86400", "X-Content-Type-Options": "nosniff"}
        if category(path.name) == "document":
            # Downloaded, never shown as a page here: an HTML file could run scripts.
            return FileResponse(
                path, headers=headers, filename=path.name, content_disposition_type="attachment"
            )
        return FileResponse(path, headers=headers)

    @app.get("/files/{message_id}")
    async def file(message_id: int) -> FileResponse:
        path = services.file_for(message_id)
        if path is None:
            raise HTTPException(404, "no such file")
        return FileResponse(path, headers={"Cache-Control": "max-age=86400"})

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        hosts = _allowed(ws.headers.get("host"), port)
        if ws.headers.get("host") not in hosts or not _local_origin(
            ws.headers.get("origin"), hosts
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        queue = services.subscribe()
        tasks: set[asyncio.Task[None]] = set()

        async def send_events() -> None:
            while True:
                await ws.send_json(await queue.get())

        sender = asyncio.create_task(send_events())
        try:
            await ws.send_json(services.status())
            while True:
                data = await ws.receive_json()
                task = asyncio.create_task(_handle(services, ws, data))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            services.unsubscribe(queue)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            for task in tasks:
                task.cancel()

    return app


async def _handle(services: AppServices, ws: WebSocket, data: Any) -> None:
    """One message from the page. Results reach the page as events."""
    if not isinstance(data, dict):
        return
    kind = data.get("type")
    request_id = data.get("request_id")
    cid = data.get("conversation_id") or None
    try:
        result: dict[str, Any] = {}
        if kind == "chat":
            files = data.get("files") or []
            if not isinstance(files, list):
                raise FridayError("files must be a list")
            result["conversation_id"] = await services.chat(
                cid, str(data.get("text", "")), [str(f) for f in files]
            )
        elif kind == "make":
            proposal = data.get("proposal_id")
            job = services.make(
                cid,
                str(data.get("kind", "")),
                str(data.get("prompt", "")),
                int(proposal) if proposal is not None else None,
            )
            result["job_id"] = job
        elif kind == "dismiss":
            services.dismiss(int(data["message_id"]))
        elif kind == "cancel_job":
            services.cancel_job(int(data["message_id"]))
        elif kind == "voice":
            result["conversation_id"] = await services.set_voice(bool(data.get("on")), cid)
        else:
            raise FridayError(f"unknown request {kind!r}")
        if request_id is not None:
            await ws.send_json({"type": "reply", "request_id": request_id, **result})
    except asyncio.CancelledError:
        raise
    except (FridayError, ValueError, KeyError) as exc:
        await _send_error(ws, request_id, str(exc))
    except Exception as exc:
        log.exception("request failed")
        await _send_error(ws, request_id, f"Something went wrong: {exc}")


async def _send_error(ws: WebSocket, request_id: Any, message: str) -> None:
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "error", "request_id": request_id, "message": message})
