"""Talking to ComfyUI, the program that makes pictures and music on this computer.

FRIDAY never builds a workflow herself. A workflow is a JSON file in ComfyUI's
"API format" (Workflow menu, Export (API)), with placeholders where the user's
words go: ``"{{prompt}}"``, ``"{{seed}}"``, ``"{{seconds}}"`` and so on. FRIDAY
fills them in, queues the job, waits for it, and fetches the finished files.

The calls used are ComfyUI's own HTTP routes:

* ``POST /prompt``   ``{"prompt": <workflow>, "client_id": ...}`` ->
  ``{"prompt_id": ..., "number": ..., "node_errors": {}}``; a broken workflow
  gives HTTP 400 with ``{"error": {...}, "node_errors": {...}}``.
* ``GET /history/<prompt_id>`` -> ``{}`` while queued or running, then
  ``{<prompt_id>: {"outputs": {<node>: {"images": [...]}}, "status": {...}}}``.
* ``GET /view?filename=&subfolder=&type=`` -> the file's bytes.
* ``POST /free`` ``{"unload_models": true, "free_memory": true}``.
* ``POST /interrupt`` stops the job that is running.
* ``GET /system_stats`` answers as soon as ComfyUI is up.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from friday.core.errors import FridayError

log = logging.getLogger(__name__)

# Output kinds ComfyUI reports per node: SaveImage -> "images", SaveAudio ->
# "audio", video nodes -> "gifs" or "videos".
_OUTPUT_KINDS = ("images", "audio", "gifs", "videos")
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_MAX_SEED = 2**32 - 1


class ComfyError(FridayError):
    """ComfyUI is not reachable, refused the workflow, or the job failed."""


@dataclass(frozen=True, slots=True)
class OutputFile:
    """A finished file as ComfyUI reports it."""

    filename: str
    subfolder: str = ""
    type: str = "output"


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ComfyError(f"cannot read the workflow {path}: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ComfyError(f"{path} is not a workflow")
    if "nodes" in data and "links" in data:
        raise ComfyError(
            f"{path} is in ComfyUI's editor format. Export it with Workflow > Export (API)."
        )
    return data


def placeholders(workflow: Mapping[str, Any]) -> set[str]:
    """The ``{{name}}`` placeholders a workflow uses."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.update(_PLACEHOLDER.findall(value))
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(workflow)
    return found


def fill_workflow(
    workflow: Mapping[str, Any],
    values: Mapping[str, str | int | float],
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """A copy of ``workflow`` with every ``{{name}}`` replaced.

    A string that is only a placeholder takes the value's own type, so
    ``"{{seconds}}"`` becomes the number 30, not the text "30". ``{{seed}}``
    is a new random number each time unless a value is given: ComfyUI skips a
    job it has already done, and the same seed would give the same picture.
    Inputs named ``seed`` or ``noise_seed`` that hold a plain number are given a
    new random number as well, for the same reason.
    """
    rng = rng or random.Random()
    filled = copy.deepcopy(dict(workflow))
    seed = values.get("seed", rng.randint(0, _MAX_SEED))
    lookup: dict[str, str | int | float] = {**values, "seed": seed}

    def sub(value: Any, key: str = "") -> Any:
        if isinstance(value, str):
            whole = _PLACEHOLDER.fullmatch(value.strip())
            if whole:
                name = whole.group(1)
                if name not in lookup:
                    raise ComfyError(f"the workflow needs a value for {{{{{name}}}}}")
                return lookup[name]

            def text(match: re.Match[str]) -> str:
                name = match.group(1)
                if name not in lookup:
                    raise ComfyError(f"the workflow needs a value for {{{{{name}}}}}")
                return str(lookup[name])

            return _PLACEHOLDER.sub(text, value)
        if isinstance(value, dict):
            return {k: sub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [sub(v) for v in value]
        if key in ("seed", "noise_seed") and isinstance(value, int) and "seed" not in values:
            return rng.randint(0, _MAX_SEED)
        return value

    return {k: sub(v) for k, v in filled.items()}


def _outputs(entry: Mapping[str, Any]) -> list[OutputFile]:
    files: list[OutputFile] = []
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping):
        return files
    for node in outputs.values():
        if not isinstance(node, Mapping):
            continue
        for kind in _OUTPUT_KINDS:
            for item in node.get(kind) or []:
                if not isinstance(item, Mapping) or not item.get("filename"):
                    continue
                if item.get("type", "output") != "output":
                    continue  # previews and temporary files, not the result
                files.append(
                    OutputFile(
                        filename=str(item["filename"]),
                        subfolder=str(item.get("subfolder", "")),
                        type=str(item.get("type", "output")),
                    )
                )
    return files


def _failure(entry: Mapping[str, Any]) -> str | None:
    status = entry.get("status")
    if not isinstance(status, Mapping) or status.get("status_str") != "error":
        return None
    for message in status.get("messages") or []:
        if (
            isinstance(message, list)
            and len(message) == 2
            and message[0] == "execution_error"
            and isinstance(message[1], Mapping)
        ):
            detail = message[1]
            node = detail.get("node_type", "a node")
            return f"{node} failed: {detail.get('exception_message', 'unknown error')}".strip()
    return "the job failed"


def _refusal(body: bytes) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:200].decode("utf-8", "replace")
    parts: list[str] = []
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, Mapping):
        parts.append(str(error.get("message", "")))
    nodes = data.get("node_errors") if isinstance(data, dict) else None
    if isinstance(nodes, Mapping):
        for node in nodes.values():
            for err in (node or {}).get("errors") or []:
                if isinstance(err, Mapping):
                    parts.append(f"{err.get('message', '')}: {err.get('details', '')}")
    return "; ".join(p for p in parts if p.strip()) or "the workflow was refused"


def _save(folder: Path, filename: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    # The name comes from ComfyUI: keep only its last part, never a path.
    name = Path(filename.replace("\\", "/")).name or "output"
    path = folder / name
    stem, suffix, n = path.stem, path.suffix, 1
    while path.exists():
        path = folder / f"{stem}-{n}{suffix}"
        n += 1
    path.write_bytes(data)
    return path


class ComfyClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8188",
        client: httpx.AsyncClient | None = None,
        poll_s: float = 1.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(30.0, connect=3.0)
        )
        self._poll_s = poll_s
        self.client_id = uuid.uuid4().hex

    async def is_up(self) -> bool:
        try:
            response = await self._client.get("/system_stats")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def queue(self, workflow: Mapping[str, Any]) -> str:
        try:
            response = await self._client.post(
                "/prompt", json={"prompt": workflow, "client_id": self.client_id}
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"ComfyUI is not reachable ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise ComfyError(f"ComfyUI refused the workflow: {_refusal(response.content)}")
        data = response.json()
        if data.get("node_errors"):
            raise ComfyError(f"ComfyUI refused the workflow: {_refusal(response.content)}")
        prompt_id = data.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyError("ComfyUI did not return a job id")
        return prompt_id

    async def wait(self, prompt_id: str, timeout_s: float) -> list[OutputFile]:
        """Wait for the job to finish; return its output files."""
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                response = await self._client.get(f"/history/{prompt_id}")
            except httpx.HTTPError as exc:
                raise ComfyError(f"lost contact with ComfyUI ({type(exc).__name__})") from exc
            if response.status_code == 200:
                entry = response.json().get(prompt_id)
                if isinstance(entry, Mapping):
                    failure = _failure(entry)
                    if failure:
                        raise ComfyError(failure)
                    status = entry.get("status")
                    finished = not isinstance(status, Mapping) or status.get("completed", True)
                    if finished:
                        files = _outputs(entry)
                        if not files:
                            raise ComfyError("the job finished but saved no file")
                        return files
            if time.monotonic() >= deadline:
                raise ComfyError(f"the job took longer than {timeout_s:.0f} seconds")
            await asyncio.sleep(self._poll_s)

    async def download(self, item: OutputFile, folder: Path) -> Path:
        params = {"filename": item.filename, "subfolder": item.subfolder, "type": item.type}
        try:
            response = await self._client.get("/view", params=params)
        except httpx.HTTPError as exc:
            raise ComfyError(f"could not fetch {item.filename} ({type(exc).__name__})") from exc
        if response.status_code != 200:
            raise ComfyError(f"could not fetch {item.filename} (HTTP {response.status_code})")
        return await asyncio.to_thread(_save, folder, item.filename, response.content)

    async def free(self) -> None:
        """Ask ComfyUI to give the graphics card back. Best effort."""
        try:
            # Off the graphics card, but kept in RAM (32 GB on the PC), so
            # the next job starts in seconds instead of reading the disk.
            await self._client.post("/free", json={"unload_models": True, "free_memory": False})
        except httpx.HTTPError as exc:
            log.warning("ComfyUI did not free the card: %s", type(exc).__name__)

    async def interrupt(self) -> None:
        try:
            await self._client.post("/interrupt")
        except httpx.HTTPError as exc:
            log.warning("ComfyUI did not stop the job: %s", type(exc).__name__)

    async def aclose(self) -> None:
        await self._client.aclose()


class ComfyLauncher:
    """Starts ComfyUI when FRIDAY needs it and it is not already running.

    The command comes from the config file, never from what was said. ComfyUI
    keeps running afterwards, for the next job and for the owner's own use.
    """

    def __init__(
        self,
        command: Sequence[str],
        log_path: Path | None = None,
        start_timeout_s: float = 180.0,
        poll_s: float = 2.0,
    ) -> None:
        self.command = [str(Path(part).expanduser()) if "~" in part else part for part in command]
        self.log_path = log_path
        self.start_timeout_s = start_timeout_s
        self._poll_s = poll_s
        self._process: subprocess.Popen[bytes] | None = None

    def _spawn(self) -> subprocess.Popen[bytes]:
        output: Any = subprocess.DEVNULL
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            output = self.log_path.open("ab")
        options: dict[str, Any] = {}
        if sys.platform == "win32":
            options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            options["start_new_session"] = True  # not stopped by FRIDAY's Ctrl+C
        try:
            return subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                **options,
            )
        except OSError as exc:
            raise ComfyError(f"could not start ComfyUI ({self.command[0]}): {exc}") from exc
        finally:
            if output is not subprocess.DEVNULL:
                output.close()

    async def ensure_running(self, comfy: ComfyClient) -> bool:
        """Start ComfyUI if needed. True if this call started it."""
        if await comfy.is_up():
            return False
        if self._process is None or self._process.poll() is not None:
            log.info("starting ComfyUI: %s", " ".join(self.command))
            self._process = self._spawn()
        deadline = time.monotonic() + self.start_timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(self._poll_s)
            if await comfy.is_up():
                return True
            if self._process.poll() is not None:
                where = f" See {self.log_path}." if self.log_path else ""
                raise ComfyError(f"ComfyUI stopped while starting.{where}")
        raise ComfyError(f"ComfyUI did not start within {self.start_timeout_s:.0f} seconds")


def open_file(path: Path) -> None:
    """Show a finished file in the program the system uses for it."""
    try:
        if sys.platform == "win32":
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen(
                [opener, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        log.warning("could not open %s: %s", path, exc)
