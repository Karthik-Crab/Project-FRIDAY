"""The chat app's web server: the page, saved chats, files, and a live channel.

It listens on 127.0.0.1 only, unless home-network access is switched on
(``[app] lan``, see ``friday.app.lan``): then devices on the home network may
connect after entering the passcode, and nothing from the internet may. Requests
whose Host or Origin is not this machine are refused, so a web page on the
internet cannot drive the app through the browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from friday.app import lan as home
from friday.app.services import AppServices
from friday.core.errors import FridayError
from friday.documents import category

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _allowed(
    host_header: str | None, port: int | None, access: home.HomeAccess | None = None
) -> set[str]:
    hosts = set(_LOCAL_HOSTS)
    if port is not None:
        hosts |= {f"{h}:{port}" for h in _LOCAL_HOSTS}
    if access is not None and host_header and home.home_host(host_header):
        hosts.add(host_header)  # this computer's home-network address or .local name
    return hosts


_SAFETY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # "same-origin", not "no-referrer": with no-referrer, Firefox sends "Origin: null"
    # on the passcode form and the Origin check refuses it.
    "Referrer-Policy": "same-origin",
}


def _who(client_ip: str | None, cookie: str | None, access: home.HomeAccess | None) -> str:
    """'ok', 'sign-in' (home device not signed in yet) or 'refuse'."""
    if access is None:
        return "ok"  # home access off: the server listens on 127.0.0.1 only
    if home.is_this_computer(client_ip):
        return "ok"
    if not access.has_passcode or not home.is_home_network(client_ip):
        return "refuse"
    return "ok" if access.signed_in(cookie) else "sign-in"


def _local_origin(origin: str | None, hosts: set[str]) -> bool:
    if origin is None:
        return True  # not from a browser page
    for scheme in ("http://", "https://"):
        if origin.startswith(scheme):
            return origin[len(scheme) :] in hosts
    return False


class _LocalOnly(BaseHTTPMiddleware):
    def __init__(self, app: Any, port: int | None, access: home.HomeAccess | None) -> None:
        super().__init__(app)
        self.port = port
        self.access = access

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await self._check(request, call_next)
        for name, value in _SAFETY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def _check(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        hosts = _allowed(request.headers.get("host"), self.port, self.access)
        if request.headers.get("host") not in hosts:
            return JSONResponse({"error": "not this machine"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not _local_origin(
            request.headers.get("origin"), hosts
        ):
            return JSONResponse({"error": "not this machine"}, status_code=403)
        ip = request.client.host if request.client else None
        who = _who(ip, request.cookies.get(home.COOKIE), self.access)
        if who == "refuse":
            return JSONResponse({"error": "not allowed"}, status_code=403)
        if who == "sign-in" and request.url.path not in ("/login",):
            if request.method == "GET" and not request.url.path.startswith(("/api/", "/ws")):
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "sign in first"}, status_code=401)
        return await call_next(request)


def create_app(
    services: AppServices, *, port: int | None = None, access: home.HomeAccess | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        yield
        await services.aclose()

    app = FastAPI(title="FRIDAY", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(_LocalOnly, port=port, access=access)

    @app.get("/login")
    async def login_form() -> HTMLResponse:
        return HTMLResponse(home.login_page(), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login(request: Request) -> Response:
        from urllib.parse import parse_qs

        ip = request.client.host if request.client else ""
        if access is None or not home.is_home_network(ip):
            return RedirectResponse("/", status_code=303)  # this computer needs no passcode
        body = await request.body()
        passcode = (parse_qs(body[:4096].decode("utf-8", "replace")).get("passcode") or [""])[0]
        wait = access.wait_left(ip)
        token = access.sign_in(ip, passcode) if wait <= 0 else None
        if token is None:
            await asyncio.sleep(home.WRONG_DELAY)  # slows guessing down further
            wait = access.wait_left(ip)
            log.warning("wrong passcode from %s", ip)
            message = (
                f"Too many tries. Wait {int(wait // 60) + 1} minutes."
                if wait > 0
                else "That passcode isn't right."
            )
            page = home.login_page(message)
            return HTMLResponse(page, status_code=401, headers={"Cache-Control": "no-store"})
        log.warning("device signed in from %s", ip)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            home.COOKIE,
            token,
            max_age=home.SESSION_DAYS * 86400,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        if access is not None:
            access.sign_out(request.cookies.get(home.COOKIE))
        response = JSONResponse({"ok": True})
        response.delete_cookie(home.COOKIE, path="/")
        return response

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
    async def new_conversation(request: Request) -> dict[str, Any]:
        body = await request.body()
        incognito = bool(body) and bool(json.loads(body).get("incognito"))
        cid = services.conversation_id(None, incognito=incognito)
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
        hosts = _allowed(ws.headers.get("host"), port, access)
        ip = ws.client.host if ws.client else None
        if (
            ws.headers.get("host") not in hosts
            or not _local_origin(ws.headers.get("origin"), hosts)
            or _who(ip, ws.cookies.get(home.COOKIE), access) != "ok"
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
            think = data.get("think")
            result["conversation_id"] = await services.chat(
                cid,
                str(data.get("text", "")),
                [str(f) for f in files],
                think if isinstance(think, bool) else None,
            )
        elif (
            kind == "make"
            and data.get("proposal_id") is None
            and services.wants_song_change(cid, str(data.get("prompt", "")))
        ):
            # Typed on the Music tab, but it's about the song in this chat.
            result["conversation_id"] = await services.chat(cid, str(data.get("prompt", "")))
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
        elif kind == "stop":
            if cid is None:
                raise FridayError("which conversation?")
            result["stopped"] = services.stop_reply(cid)
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
