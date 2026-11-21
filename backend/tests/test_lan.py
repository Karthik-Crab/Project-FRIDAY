"""Phones and laptops at home: the passcode gate, tried the way an intruder would."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_incognito import _services

from friday.app import lan
from friday.app.lan import COOKIE, HomeAccess, home_host, is_home_network
from friday.app.server import create_app

SECRET = "correct horse battery"
PC = "http://192.168.0.110:8765"


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lan, "WRONG_DELAY", 0.0)


def _access(tmp_path: Path, passcode: str | None = SECRET) -> HomeAccess:
    access = HomeAccess(tmp_path / "cfg")
    if passcode:
        access.set_passcode(passcode)
    return access


def _device(tmp_path: Path, access: HomeAccess, ip: str = "192.168.0.50") -> TestClient:
    services, _ = _services(tmp_path)
    app = create_app(services, port=8765, access=access)
    return TestClient(app, base_url=PC, client=(ip, 50000), follow_redirects=False)


def _sign_in(client: TestClient, passcode: str = SECRET, origin: str = PC):  # type: ignore[no-untyped-def]
    return client.post(
        "/login",
        content=f"passcode={passcode.replace(' ', '+')}",
        headers={"origin": origin, "content-type": "application/x-www-form-urlencoded"},
    )


def test_addresses_and_names() -> None:
    assert is_home_network("192.168.0.50") and is_home_network("10.1.2.3")
    assert is_home_network("172.20.0.9") and is_home_network("fe80::1")
    assert not is_home_network("8.8.8.8") and not is_home_network("127.0.0.1")
    assert not is_home_network("not-an-ip")
    assert home_host("192.168.0.110:8765") and home_host("nobara-pc.local:8765")
    assert not home_host("evil.example:8765") and not home_host("8.8.8.8:8765")


def test_a_home_device_sees_nothing_before_the_passcode(tmp_path: Path) -> None:
    with _device(tmp_path, _access(tmp_path)) as phone:
        assert phone.get("/").headers["location"] == "/login"
        assert phone.get("/login").status_code == 200
        for path in ("/api/conversations", "/api/status"):
            assert phone.get(path).status_code == 401, path
        for path in ("/files/1", "/uploads/x", "/static/app.js"):
            assert phone.get(path).headers["location"] == "/login", path
        assert phone.post("/api/conversations", json={}, headers={"origin": PC}).status_code == 401
        with (
            pytest.raises(WebSocketDisconnect),
            phone.websocket_connect("/ws", headers={"origin": PC}) as ws,
        ):
            ws.receive_json()


def test_the_right_passcode_lets_the_device_in(tmp_path: Path) -> None:
    with _device(tmp_path, _access(tmp_path)) as phone:
        assert _sign_in(phone, "wrong guess 1").status_code == 401
        done = _sign_in(phone)
        assert done.status_code == 303
        cookie = done.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert phone.get("/api/conversations").status_code == 200
        cookie = f"{COOKIE}={phone.cookies[COOKIE]}"  # ws: the test client sends neither
        live = {"origin": PC, "cookie": cookie, "host": "192.168.0.110:8765"}
        with phone.websocket_connect("/ws", headers=live) as ws:
            assert ws.receive_json()["type"] == "status"
        phone.post("/logout", headers={"origin": PC})
        assert phone.get("/api/conversations").status_code == 401


def test_this_computer_needs_no_passcode(tmp_path: Path) -> None:
    with _device(tmp_path, _access(tmp_path), ip="127.0.0.1") as pc:
        pc.base_url = "http://127.0.0.1:8765"  # type: ignore[assignment]
        assert pc.get("/api/conversations").status_code == 200


def test_the_internet_is_refused_even_with_a_stolen_cookie(tmp_path: Path) -> None:
    access = _access(tmp_path)
    token = access.sign_in("192.168.0.50", SECRET)
    with _device(tmp_path, access, ip="8.8.8.8") as outsider:
        outsider.cookies.set(COOKIE, token or "")
        assert outsider.get("/api/conversations").status_code == 403
        assert _sign_in(outsider).status_code == 403
        assert "set-cookie" not in _sign_in(outsider).headers


def test_other_websites_cannot_use_a_signed_in_browser(tmp_path: Path) -> None:
    access = _access(tmp_path)
    with _device(tmp_path, access) as phone:
        _sign_in(phone)
        evil = {"origin": "http://evil.example"}
        assert phone.post("/api/conversations", json={}, headers=evil).status_code == 403
        assert _sign_in(phone, origin="http://evil.example").status_code == 403
        # DNS rebinding: an outside name pointing at this computer.
        assert (
            phone.get("/api/conversations", headers={"host": "evil.example:8765"}).status_code
            == 403
        )
        cookie = f"{COOKIE}={phone.cookies[COOKIE]}"
        evil_live = {**evil, "cookie": cookie, "host": "192.168.0.110:8765"}
        with (
            pytest.raises(WebSocketDisconnect),
            phone.websocket_connect("/ws", headers=evil_live) as ws,
        ):
            ws.receive_json()


def test_guessing_is_stopped(tmp_path: Path) -> None:
    access = _access(tmp_path)
    with _device(tmp_path, access) as phone:
        for n in range(5):
            assert _sign_in(phone, f"guess number {n}").status_code == 401
        refused = _sign_in(phone)  # even the right one, for now
        assert refused.status_code == 401 and "Too many tries" in refused.text
    assert access.sign_in("192.168.0.51", SECRET)  # another device is not locked out
    for n in range(20):  # many devices guessing together lock everyone for an hour
        access.sign_in(f"192.168.1.{n}", "nope nope nope")
    assert access.sign_in("192.168.0.52", SECRET) is None


def test_a_new_passcode_signs_every_device_out(tmp_path: Path) -> None:
    access = _access(tmp_path)
    token = access.sign_in("192.168.0.50", SECRET)
    assert access.signed_in(token)
    access.set_passcode("a brand new passcode")
    assert not access.signed_in(token)


def test_the_passcode_is_never_stored(tmp_path: Path) -> None:
    access = _access(tmp_path)
    token = access.sign_in("192.168.0.50", SECRET) or ""
    for f in (access.passcode_file, access.sessions_file):
        text = f.read_text()
        assert SECRET not in text and token not in text
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        access.set_passcode("short")


def test_without_a_passcode_no_device_gets_in(tmp_path: Path) -> None:
    with _device(tmp_path, _access(tmp_path, passcode=None)) as phone:
        assert phone.get("/api/conversations").status_code == 403
        assert _sign_in(phone, "").status_code in (401, 403)


def test_pages_cannot_be_framed_by_other_sites(tmp_path: Path) -> None:
    with _device(tmp_path, _access(tmp_path)) as phone:
        headers = phone.get("/login").headers
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        # Not "no-referrer": Firefox then sends "Origin: null" and the form is refused.
        assert headers["referrer-policy"] == "same-origin"
