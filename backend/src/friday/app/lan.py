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


def home_ip() -> str | None:
    """This computer's address on the home network."""
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # picks the outgoing interface; nothing is sent
        ip = str(s.getsockname()[0])
        if is_home_network(ip):
            return ip
    return None


def local_name() -> str | None:
    name = socket.gethostname().split(".")[0].lower()
    return f"{name}.local" if name and name != "localhost" else None


def addresses(port: int, scheme: str = "https") -> list[str]:
    """How other devices can reach this computer, best first."""
    hosts = [h for h in (local_name(), home_ip()) if h]
    return [f"{scheme}://{h}:{port}/" for h in hosts]


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


SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FRIDAY setup</title><style>
:root{color-scheme:light dark;--bg:#f6f5f2;--fg:#1d1c1a;--card:#fff;--line:#d9d6cf;--accent:#c96442}
@media (prefers-color-scheme:dark){:root{--bg:#1f1e1c;--fg:#ecebe7;--card:#2a2926;--line:#3b3a36}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif}
main{max-width:560px;margin:0 auto;padding:24px 16px 40px}
h1{font-size:24px;margin:0 0 6px}h2{font-size:17px;margin:26px 0 8px}
p,li{margin:0 0 8px}ol{padding-left:22px;margin:0}.muted{opacity:.75;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin-top:16px}
a.button{display:block;text-align:center;text-decoration:none;background:var(--accent);color:#fff;
font-weight:600;padding:12px;border-radius:10px;margin:12px 0 4px}
a.plain{color:var(--accent)}details{margin-top:10px}summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<h1>Set up FRIDAY on this device</h1>
<p class="muted" id="checking">Checking whether this device is set up already…</p>
<div class="card">
<p>FRIDAY now uses a secure connection, so this device can use its microphone and
everything you send is encrypted. Do this once per device; it takes about two minutes.</p>
<h2>1. Download FRIDAY's home certificate</h2>
<a class="button" href="/FRIDAY-home.crt" download>Download certificate</a>
<p class="muted">It only works for addresses inside your home, so it can't be misused
for other websites.</p>
<h2>2. Install it (Android)</h2>
<ol>
<li>Open <b>Settings</b> and search for <b>CA certificate</b>.</li>
<li>Tap it, then <b>Install anyway</b>. Unlock the phone if it asks.</li>
<li>Pick <b>FRIDAY-home.crt</b> from Downloads.</li>
</ol>
<p class="muted">If the search finds nothing: Settings → Security &amp; privacy → More security
settings → Encryption &amp; credentials → Install a certificate → CA certificate.</p>
<details><summary>Laptop instead (Windows or Mac)</summary>
<p><b>Windows:</b> open the downloaded file → Install Certificate → Current User →
"Place all certificates in the following store" → Browse → <b>Trusted Root Certification
Authorities</b> → Next → Finish → Yes. Then restart the browser.</p>
<p><b>Mac:</b> open the file (Keychain Access opens) → double-click "FRIDAY home" →
Trust → "When using this certificate: Always Trust". Then restart the browser.</p>
<p><b>Firefox:</b> Settings → search "certificates" → View Certificates → Authorities →
Import → pick the file → tick "Trust this CA to identify websites".</p>
</details>
<h2>3. Open FRIDAY securely</h2>
<a class="button" href="{secure}">Open FRIDAY</a>
<p class="muted">Then enter your passcode. Add that page to your home screen.</p>
</div></main>
<script>
fetch("{secure}login", { mode: "no-cors", cache: "no-store" })
  .then(() => location.replace("{secure}"))
  .catch(() => {
    document.getElementById("checking").textContent = "This device isn't set up yet.";
  });
</script></body></html>"""
