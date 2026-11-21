"""Opening the chat app to other devices on the home network, behind a passcode.

Only addresses inside the home network (192.168.x.x, 10.x.x.x, 172.16-31.x.x) may
connect; anything from the internet is refused even if a router forwards it.
This computer itself never needs the passcode. The passcode is stored only as a
salted scrypt hash; signed-in devices hold a random token whose hash is kept.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import time
from pathlib import Path

COOKIE = "friday_session"
SESSION_DAYS = 90
MIN_LENGTH = 8
_TRIES = 5  # wrong passcodes in a row before a device has to wait
_WAIT = 300.0  # seconds
WRONG_DELAY = 1.0  # seconds before answering a wrong passcode
_ALL_TRIES = 20  # wrong passcodes from all devices together ...
_ALL_WINDOW = 3600.0  # ... within an hour lock every sign-in for that hour


def is_this_computer(ip: str | None) -> bool:
    try:
        return ip is not None and ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def is_home_network(ip: str | None) -> bool:
    """A private (home network) address, not the internet."""
    try:
        address = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (address.is_private or address.is_link_local) and not address.is_loopback


def home_host(host_header: str | None) -> bool:
    """A Host header naming this computer by its home-network address or .local name."""
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if not host_header.endswith("]") else host_header
    name = name.strip("[]").lower()
    if name.endswith(".local"):
        return True
    return is_home_network(name) or is_this_computer(name)


def addresses(port: int) -> list[str]:
    """How other devices can reach this computer, best first."""
    out: list[str] = []
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # picks the outgoing interface; nothing is sent
        ip = s.getsockname()[0]
        if is_home_network(ip):
            out.append(f"http://{ip}:{port}/")
    name = socket.gethostname().split(".")[0]
    if name:
        out.append(f"http://{name}.local:{port}/")
    return out


def _hash(passcode: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passcode.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _write_private(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


class HomeAccess:
    """The passcode, the signed-in devices, and a brake on guessing."""

    def __init__(self, folder: Path) -> None:
        self.passcode_file = folder / "lan-passcode.json"
        self.sessions_file = folder / "lan-sessions.json"
        self._failures: dict[str, tuple[int, float]] = {}
        self._all_failures: list[float] = []

    # The passcode -----------------------------------------------------------

    @property
    def has_passcode(self) -> bool:
        return self.passcode_file.is_file()

    def set_passcode(self, passcode: str) -> None:
        if len(passcode) < MIN_LENGTH:
            raise ValueError(f"the passcode needs at least {MIN_LENGTH} characters")
        salt = secrets.token_bytes(16)
        _write_private(
            self.passcode_file, {"salt": salt.hex(), "hash": _hash(passcode, salt).hex()}
        )
        _write_private(self.sessions_file, {})  # a new passcode signs every device out

    def _passcode_ok(self, passcode: str) -> bool:
        try:
            saved = json.loads(self.passcode_file.read_text())
            salt, want = bytes.fromhex(saved["salt"]), bytes.fromhex(saved["hash"])
        except (OSError, ValueError, KeyError):
            return False
        return hmac.compare_digest(_hash(passcode, salt), want)

    # Signing in -------------------------------------------------------------

    def wait_left(self, ip: str) -> float:
        now = time.monotonic()
        self._all_failures = [t for t in self._all_failures if now - t < _ALL_WINDOW]
        if len(self._all_failures) >= _ALL_TRIES:
            return _ALL_WINDOW - (now - self._all_failures[0])
        count, until = self._failures.get(ip, (0, 0.0))
        return max(0.0, until - now) if count >= _TRIES else 0.0

    def sign_in(self, ip: str, passcode: str) -> str | None:
        """A new device token, or None (wrong passcode, or too many tries)."""
        if self.wait_left(ip) > 0:
            return None
        if not self._passcode_ok(passcode):
            count = self._failures.get(ip, (0, 0.0))[0] + 1
            self._failures[ip] = (count, time.monotonic() + _WAIT)
            self._all_failures.append(time.monotonic())
            return None
        self._failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        sessions = self._sessions()
        sessions[self._key(token)] = time.time() + SESSION_DAYS * 86400
        _write_private(self.sessions_file, sessions)
        return token

    def signed_in(self, token: str | None) -> bool:
        if not token or not self.has_passcode:
            return False
        expires = self._sessions().get(self._key(token))
        return expires is not None and expires > time.time()

    def sign_out(self, token: str | None) -> None:
        sessions = self._sessions()
        if token and sessions.pop(self._key(token), None) is not None:
            _write_private(self.sessions_file, sessions)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _sessions(self) -> dict[str, float]:
        try:
            data = json.loads(self.sessions_file.read_text())
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {k: float(v) for k, v in data.items() if float(v) > now}


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRIDAY</title><style>
:root{color-scheme:light dark;--bg:#f6f5f2;--fg:#1d1c1a;--card:#fff;--line:#d9d6cf;--accent:#c96442}
@media (prefers-color-scheme:dark){:root{--bg:#1f1e1c;--fg:#ecebe7;--card:#2a2926;--line:#3b3a36}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px;
width:min(340px,calc(100vw - 32px));box-sizing:border-box}
h1{margin:0 0 4px;font-size:22px}p{margin:0 0 18px;opacity:.75;font-size:14px}
input,button{width:100%;box-sizing:border-box;font:inherit;padding:11px 12px;border-radius:9px}
input{border:1px solid var(--line);background:var(--bg);color:var(--fg);margin-bottom:12px}
button{border:0;background:var(--accent);color:#fff;font-weight:600}
.err{color:#d0473a;opacity:1}</style></head><body>
<form method="post" action="/login"><h1>FRIDAY</h1><p>Enter the passcode to continue.</p>
{message}<input type="password" name="passcode" autocomplete="current-password" autofocus required>
<button type="submit">Open</button></form></body></html>"""


def login_page(message: str = "") -> str:
    return LOGIN_PAGE.replace("{message}", f'<p class="err">{message}</p>' if message else "")
