"""Voice through the browser: the phone, laptop or PC in your hand listens and
speaks (with the browser's echo cancellation), over a secure home connection."""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import stat
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from fastapi.testclient import TestClient
from test_app import HOST, _client, _services, _until

from friday.app import tls
from friday.app.server import create_setup_app
from friday.conversation.engine import ConversationEngine
from friday.core.errors import FridayError, VoiceError
from friday.voice.adapters.browser_io import BrowserAudio
from friday.voice.base import AudioChunk


class Page:
    def __init__(self) -> None:
        self.got: list[bytes] = []

    async def send(self, data: bytes) -> None:
        self.got.append(data)


async def test_microphone_frames_flow_only_once_listening() -> None:
    page = Page()
    audio = BrowserAudio(page.send)
    audio.feed(b"\x01\x00" * 320)  # voice mode still starting: not answered later
    frames = audio.frames()
    first = asyncio.ensure_future(frames.__anext__())
    await asyncio.sleep(0)
    audio.feed(b"\x02\x00" * 320)
    frame = await asyncio.wait_for(first, 1)
    assert frame.pcm == b"\x02\x00" * 320 and frame.sample_rate == 16_000
    audio.close()
    with pytest.raises(VoiceError):
        await frames.__anext__()
    await asyncio.sleep(0)
    assert page.got[-1] == b"X"


async def test_speech_goes_to_the_page_and_stopping_reports_what_was_heard() -> None:
    page = Page()
    audio = BrowserAudio(page.send)
    short = AudioChunk(b"\x00\x00" * 2400, 24_000)  # 0.1 s
    assert await audio.play(short) == 1.0
    assert page.got[0][:1] == b"A" and struct.unpack(">I", page.got[0][1:5])[0] == 24_000
    assert page.got[0][5:] == short.pcm
    long = AudioChunk(b"\x00\x00" * 24_000, 24_000)  # 1 s
    playing = asyncio.ensure_future(audio.play(long))
    await asyncio.sleep(0.3)
    audio.stop()
    heard = await playing
    assert 0.1 < heard < 0.5
    await asyncio.sleep(0)
    assert page.got[-1] == b"S"


class BrowserVoice:
    """Voice mode stand-in that keeps the page's audio."""

    def __init__(self) -> None:
        self.state = "off"
        self.conversation_id: str | None = None
        self.audio: Any = None
        self.on_stopped: Callable[[str | None], None] | None = None

    @property
    def available(self) -> bool:
        return True

    async def start(
        self,
        engine: ConversationEngine,
        conversation_id: str,
        on_event: Callable[[object], None],
        audio: Any = None,
    ) -> None:
        self.state, self.conversation_id, self.audio = "on", conversation_id, audio

    async def stop(self) -> None:
        self.state, self.conversation_id = "off", None

    def announce(self, engine: ConversationEngine, text: str, context: str) -> None:
        pass


def test_the_page_that_switches_voice_on_is_the_one_that_listens(tmp_path: Path) -> None:
    voice = BrowserVoice()
    services, _ = _services(tmp_path, voice=voice)
    services.voice_audio_kind = "browser"
    with _client(services) as client:
        with client.websocket_connect("/ws", headers=HOST) as phone:
            assert _until(phone, "status")["voice_audio"] == "browser"
            phone.send_json({"type": "voice", "on": True, "request_id": 1})
            assert "microphone" in _until(phone, "error")["message"]  # no page audio
            phone.send_json({"type": "voice", "on": True, "audio": "browser", "request_id": 2})
            _until(phone, "reply")
            audio = voice.audio
            assert isinstance(audio, BrowserAudio) and services.voice_audio is audio
            audio._listening = True
            phone.send_bytes(b"\x05\x00" * 320)
            _until(phone, "status")  # let the server read the frame
            phone.send_json({"type": "nothing", "request_id": 3})
            _until(phone, "error")
            assert audio._queue.qsize() == 1

            with client.websocket_connect("/ws", headers=HOST) as laptop:
                laptop.send_bytes(b"\x07\x00" * 320)  # not its voice: ignored
                laptop.send_json({"type": "voice", "on": True, "audio": "browser", "request_id": 4})
                _until(laptop, "reply")
                assert audio.closed and voice.audio is not audio  # the laptop took over
                assert audio._queue.qsize() == 2  # only the close marker was added
            # The laptop's page closed: its voice ends.
        assert services.voice_audio is None or services.voice_audio.closed


async def test_browser_voice_needs_a_page(tmp_path: Path) -> None:
    services, _ = _services(tmp_path, voice=BrowserVoice())
    services.voice_audio_kind = "browser"
    with pytest.raises(FridayError):
        await services.set_voice(True, None)


# The home certificate ------------------------------------------------------


def test_the_home_certificate_only_vouches_for_home_addresses(tmp_path: Path) -> None:
    cert = tls.ensure(tmp_path, ["nobara-pc.local", "example.com"], ["192.168.0.110", "8.8.8.8"])
    ca = x509.load_pem_x509_certificate(cert.ca_file.read_bytes())
    limits = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert limits.critical
    permitted = limits.value.permitted_subtrees or []
    assert x509.DNSName("local") in permitted
    assert x509.IPAddress(ipaddress.ip_network("192.168.0.0/16")) in permitted
    assert len(permitted) == 4
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.path_length == 0
    server = x509.load_pem_x509_certificates(cert.cert_file.read_bytes())[0]
    server.verify_directly_issued_by(ca)
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["nobara-pc.local"]  # not example.com
    assert [str(a) for a in san.get_values_for_type(x509.IPAddress)] == ["192.168.0.110"]
    assert server.not_valid_after_utc - dt.datetime.now(dt.UTC) < dt.timedelta(days=398)
    for key in (tmp_path / "friday-home-ca.key", cert.key_file):
        assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_a_new_address_gets_a_new_certificate_from_the_same_authority(tmp_path: Path) -> None:
    first = tls.ensure(tmp_path, ["nobara-pc.local"], ["192.168.0.110"])
    again = tls.ensure(tmp_path, ["nobara-pc.local"], ["192.168.0.110"])
    assert first.cert_file.read_bytes() == again.cert_file.read_bytes()  # kept
    moved = tls.ensure(tmp_path, ["nobara-pc.local"], ["192.168.0.120"])
    assert moved.fingerprint == first.fingerprint  # devices need nothing new
    server = x509.load_pem_x509_certificates(moved.cert_file.read_bytes())[0]
    assert "192.168.0.120" in str(server.extensions)


def test_the_setup_page_serves_the_certificate_and_forwards_to_https(tmp_path: Path) -> None:
    cert = tls.ensure(tmp_path, ["nobara-pc.local"], ["192.168.0.110"])
    app = create_setup_app(8443, cert.ca_file)
    phone = TestClient(
        app,
        base_url="http://192.168.0.110:8765",
        client=("192.168.0.50", 5000),
        follow_redirects=False,
    )
    page = phone.get("/")
    assert page.status_code == 200 and "https://192.168.0.110:8443/" in page.text
    crt = phone.get("/FRIDAY-home.crt")
    assert crt.headers["content-type"] == "application/x-x509-ca-cert"
    assert x509.load_der_x509_certificate(crt.content).subject.rfc4514_string() == "CN=FRIDAY home"
    moved = phone.get("/api/conversations")
    assert moved.status_code == 308
    assert moved.headers["location"] == "https://192.168.0.110:8443/api/conversations"
    assert phone.get("/", headers={"host": "evil.example:8765"}).status_code == 403
    outsider = TestClient(app, base_url="http://192.168.0.110:8765", client=("8.8.8.8", 5000))
    assert outsider.get("/FRIDAY-home.crt").status_code == 403
