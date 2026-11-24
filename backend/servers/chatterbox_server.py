"""A small local speech service around Chatterbox (expressive text-to-speech).

Runs in its own Python environment, apart from FRIDAY's, because Chatterbox
pins its own versions of PyTorch and NumPy. FRIDAY talks to it over HTTP on
this machine only. Stopping this process frees the graphics card.

    python chatterbox_server.py --device cuda --port 5005

Status: the HTTP layer and the calls into Chatterbox are covered by tests with
a stand-in that has the same call signatures as chatterbox-tts 0.1.7. It has
never been run with the real Chatterbox model.

Endpoints
    GET  /health  -> {"ok": true, "model": ..., "device": ..., "sample_rate": ...,
                      "on_card": true|false}
    POST /speak   {"text": "..."} -> raw 16-bit mono PCM, rate in X-Sample-Rate
                  (503 while parked: FRIDAY then uses her built-in voice)
    POST /card    {"on": true|false} -> move the model onto the graphics card or
                  back into RAM (about half a second either way)

With FRIDAY_VOICE_PARKED=1 the model loads into RAM only and waits there
("parked") until FRIDAY moves it onto the card for voice mode.

This file imports nothing from FRIDAY, so it can be copied and run alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

MAX_TEXT_CHARS = 1000
MAX_BODY_BYTES = 16_384


class Voice(Protocol):
    name: str
    device: str
    sample_rate: int

    def speak(self, text: str) -> bytes: ...


class Parked(Exception):
    """The model is in RAM, not on the card."""


class ChatterboxVoice:
    """The real model. Imported lazily so tests need neither torch nor a GPU."""

    def __init__(self, device: str, reference: str | None, parked: bool = False) -> None:
        from chatterbox.tts_turbo import ChatterboxTurboTTS

        self.name = "chatterbox-turbo"
        self.target = device  # where it runs when it is on the card
        self.device = "cpu" if parked else device
        self.parked = parked and device != "cpu"
        self._warm = False
        # Chatterbox 0.1.7 takes the device and nothing else here.
        self._model: Any = ChatterboxTurboTTS.from_pretrained(self.device)
        self.sample_rate = int(self._model.sr)
        self._reference: str | None = None
        if reference:
            prepare = getattr(self._model, "prepare_conditionals", None)
            if prepare is not None:
                prepare(reference)  # work out the voice once, not on every sentence
            else:
                self._reference = reference
        elif getattr(self._model, "conds", None) is None:
            raise SystemExit(
                "This model has no built-in voice. Start again with --voice and a "
                "clean recording (longer than 5 seconds) of a voice you may use."
            )

    def move(self, on_card: bool) -> None:
        """Onto the graphics card, or back into RAM (freeing the card)."""
        device = self.target if on_card else "cpu"
        if device != self.device:
            import torch

            try:
                self._place(device)
            except Exception:
                if device != "cpu":  # e.g. the card was full: back to RAM, whole
                    self._place("cpu")
                    self.parked = True
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                raise
            if device == "cpu" and torch.cuda.is_available():
                torch.cuda.empty_cache()
        self.parked = not on_card and self.target != "cpu"
        if on_card and not self._warm:
            self._warm = True
            self.speak("Ready.")  # the first sentence on the card is slow

    def _place(self, device: str) -> None:
        m = self._model
        m.t3.to(device)
        m.s3gen.to(device)
        m.ve.to(device)
        if m.conds is not None:
            m.conds = m.conds.to(device)
        m.device = device
        self.device = device

    def speak(self, text: str) -> bytes:
        import numpy as np

        if self.parked:
            raise Parked
        if self._reference:
            wav = self._model.generate(text, audio_prompt_path=self._reference)
        else:
            wav = self._model.generate(text)
        samples = np.clip(wav.squeeze().detach().cpu().numpy(), -1.0, 1.0)
        return bytes((samples * 32767.0).astype("<i2").tobytes())


def make_handler(voice: Voice) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()  # one sentence at a time on the card

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path != "/health":
                self._json(404, {"error": "not found"})
                return
            self._json(
                200,
                {
                    "ok": True,
                    "model": voice.name,
                    "device": voice.device,
                    "sample_rate": voice.sample_rate,
                    "on_card": not getattr(voice, "parked", False),
                },
            )

        def do_POST(self) -> None:
            if self.path == "/card" and hasattr(voice, "move"):
                self._card()
                return
            if self.path != "/speak":
                self._json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._json(400, {"error": "bad request size"})
                return
            try:
                text = json.loads(self.rfile.read(length))["text"]
            except (ValueError, KeyError, TypeError):
                self._json(400, {"error": 'expected {"text": "..."}'})
                return
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                self._json(400, {"error": f"text must be 1 to {MAX_TEXT_CHARS} characters"})
                return
            try:
                with lock:
                    pcm = voice.speak(text)
            except Parked:
                self._json(503, {"error": "the voice is parked in RAM"})
                return
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("X-Sample-Rate", str(voice.sample_rate))
            self.send_header("Content-Length", str(len(pcm)))
            self.end_headers()
            self.wfile.write(pcm)

        def _card(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                on = json.loads(self.rfile.read(length) if 0 < length <= 1024 else b"")["on"]
            except (ValueError, KeyError, TypeError):
                self._json(400, {"error": 'expected {"on": true or false}'})
                return
            if not isinstance(on, bool):
                self._json(400, {"error": 'expected {"on": true or false}'})
                return
            try:
                with lock:
                    voice.move(on)  # type: ignore[attr-defined]
            except Exception as exc:  # e.g. not enough room on the card
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(200, {"ok": True, "on_card": on, "device": voice.device})

        def log_message(self, format: str, *args: Any) -> None:
            return  # never log what was said

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5005)
    parser.add_argument("--device", default="cuda", help="cuda or cpu")
    parser.add_argument("--voice", default=None, help="reference recording of the voice to use")
    args = parser.parse_args(argv)

    parked = os.environ.get("FRIDAY_VOICE_PARKED") == "1"
    where = "RAM (parked)" if parked else args.device
    print(f"Loading the voice model into {where}...", flush=True)
    voice = ChatterboxVoice(args.device, args.voice, parked=parked)
    if not parked:
        voice.move(True)  # pay the start-up cost now
    # 127.0.0.1 only: there is no login, so it must not be reachable from the network.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(voice))
    print(f"Voice service ready on http://127.0.0.1:{args.port}  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
