"""The real aiohttp app over HTTP: login, logout, auth on every route, file API.

Uses aiohttp's in-process test server, so nothing listens on a real port and
no macOS tool (screencapture, osascript, Quartz) is ever invoked.
"""
import json
import os

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from conftest import TEST_PASSWORD, run

# Routes that are reachable without a session, by design.
PUBLIC = {"/login", "/api/login", "/logout", "/api/authcheck", "/",
          "/machines/probe/{key}"}


async def _client(srv):
    client = TestClient(TestServer(srv.create_app()))
    await client.start_server()
    return client


def with_client(fn):
    """Run an async test body against a fresh in-process server."""
    def wrapper(srv, *args, **kwargs):
        async def go():
            client = await _client(srv)
            try:
                await fn(srv, client, *args, **kwargs)
            finally:
                await client.close()
        run(go())
    wrapper.__name__ = fn.__name__
    return wrapper


async def login(client):
    r = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert r.status == 200
    return (await r.json())["token"]


# ── login / logout ────────────────────────────────────────────────────────

@with_client
async def _login_flow(srv, client):
    r = await client.post("/api/login", json={"password": "wrong"})
    assert r.status == 401

    r = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert r.status == 200
    token = (await r.json())["token"]
    assert srv.SESSION_COOKIE in r.cookies
    assert srv.valid_session(token)

    r = await client.get("/", allow_redirects=False)
    assert r.status == 200
    r = await client.get("/api/authcheck")
    assert r.status == 200

    # logout must kill the token itself, not just the cookie
    r = await client.get("/logout", allow_redirects=False)
    assert r.status in (302, 303)
    assert r.headers["Location"] == "/login"
    r = await client.get(f"/api/status?token={token}")
    assert r.status == 401
    r = await client.get(f"/?token={token}", allow_redirects=False)
    assert r.status == 302


def test_login_flow(srv):
    _login_flow(srv)


@with_client
async def _login_page_redirects_when_logged_in(srv, client):
    r = await client.get("/login", allow_redirects=False)
    assert r.status == 200
    await login(client)
    r = await client.get("/login", allow_redirects=False)
    assert r.status == 302
    assert r.headers["Location"] == "/"


def test_login_page_redirects_when_logged_in(srv):
    _login_page_redirects_when_logged_in(srv)


@pytest.mark.parametrize("body,status", [
    (b"x" * 5000, 413),                      # bounded read, not a 100 MB buffer
    (b"not json at all", 400),
    (json.dumps({"password": 12345}).encode(), 401),
    (b"{}", 401),
    (b"[]", 401),
    (b"", 401),
    (json.dumps({"password": "pässwörd"}).encode(), 401),
])
def test_login_bad_bodies_never_500(srv, body, status):
    @with_client
    async def go(srv, client):
        r = await client.post("/api/login", data=body,
                              headers={"Content-Type": "application/json"})
        assert r.status == status
    go(srv)


@with_client
async def _brute_force_locks_out(srv, client):
    codes = []
    for _ in range(srv.MAX_LOGIN_ATTEMPTS + 2):
        codes.append((await client.post("/api/login", json={"password": "wrong"})).status)
    assert codes[:srv.MAX_LOGIN_ATTEMPTS] == [401] * srv.MAX_LOGIN_ATTEMPTS
    assert codes[-1] == 429
    r = await client.post("/api/login", json={"password": TEST_PASSWORD})
    assert r.status == 429


def test_brute_force_locks_out(srv):
    _brute_force_locks_out(srv)


@with_client
async def _https_cookie_is_secure(srv, client):
    r = await client.post("/api/login", json={"password": TEST_PASSWORD},
                          headers={"X-Forwarded-Proto": "https"})
    cookie = r.headers["Set-Cookie"]
    assert "Secure" in cookie
    assert "SameSite=None" in cookie


def test_https_cookie_is_secure(srv):
    _https_cookie_is_secure(srv)


@with_client
async def _query_token_beats_stale_cookie(srv, client):
    token = srv.create_session()
    client.session.cookie_jar.update_cookies({srv.SESSION_COOKIE: "stale-garbage"})
    r = await client.get(f"/api/authcheck?token={token}")
    assert r.status == 200


def test_query_token_beats_stale_cookie(srv):
    _query_token_beats_stale_cookie(srv)


# ── every protected route really is protected ─────────────────────────────

def _protected_routes(srv):
    app = srv.create_app()
    out = []
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter") or info.get("prefix")
        if not path or path in PUBLIC or path.startswith("/static"):
            continue
        out.append((route.method, path))
    return out


def test_every_protected_route_requires_auth(srv):
    routes = _protected_routes(srv)
    assert len(routes) >= 25

    @with_client
    async def go(srv, client):
        for method, path in routes:
            r = await client.request(method, path)
            assert r.status == 401, f"{method} {path} -> {r.status}"
            r = await client.request(method, path + "?token=v1.99999999999.deadbeef")
            assert r.status == 401, f"{method} {path} accepted a forged token"
    go(srv)


# ── status ────────────────────────────────────────────────────────────────

@with_client
async def _status_shape(srv, client):
    await login(client)
    r = await client.get("/api/status")
    assert r.status == 200
    body = await r.json()
    for key in ("cpu_percent", "ram_total_gb", "ram_free_percent", "mem_state",
                "disk_percent", "boot_time", "net_sent_gb"):
        assert key in body
    assert body["mem_state"] in ("ok", "tight", "critical")
    assert 0 <= body["ram_free_percent"] <= 100


def test_status_shape(srv, monkeypatch):
    if hasattr(srv, "SYSTEM_DRIVE"):              # windows/server.py reads the C: drive
        monkeypatch.setattr(srv, "SYSTEM_DRIVE", "/")
    _status_shape(srv)


# ── file API, inside a throwaway home dir ─────────────────────────────────

@with_client
async def _file_browser(srv, client, home):
    await login(client)
    (home / "Desktop" / "b.txt").write_text("hello")
    (home / "Desktop" / "A_dir").mkdir()
    (home / "Desktop" / ".hidden").write_text("x")

    r = await client.get("/api/files", params={"path": str(home / "Desktop")})
    assert r.status == 200
    body = await r.json()
    assert body["type"] == "directory"
    assert [e["name"] for e in body["entries"]] == ["A_dir", "b.txt"]   # dirs first, no dotfiles
    assert body["entries"][1]["size"] == 5

    r = await client.get("/api/files", params={"path": "~/Desktop/b.txt"})
    assert (await r.json())["type"] == "file"

    r = await client.get("/api/files", params={"path": str(home)})
    assert (await r.json())["parent"] is None

    r = await client.get("/api/files", params={"path": str(home / "nope")})
    assert r.status == 404

    r = await client.get("/api/files", params={"path": "/etc"})
    assert r.status == 403

    r = await client.get("/api/files/download", params={"path": str(home / "Desktop" / "b.txt")})
    assert r.status == 200
    assert await r.read() == b"hello"
    assert "attachment" in r.headers["Content-Disposition"]

    r = await client.get("/api/files/download", params={"path": "/etc/hostname"})
    assert r.status == 403

    # symlink out of home is resolved before the check
    os.symlink("/etc", home / "Desktop" / "escape")
    r = await client.get("/api/files", params={"path": str(home / "Desktop" / "escape")})
    assert r.status == 403


def test_file_browser(srv, home):
    _file_browser(srv, home)


@with_client
async def _file_upload_delete_move(srv, client, home):
    await login(client)
    form = FormData()
    form.add_field("dest", str(home / "Desktop" / "inbox"))
    form.add_field("file", b"payload", filename="up.bin")
    r = await client.post("/api/files/upload", data=form)
    assert r.status == 200
    assert (home / "Desktop" / "inbox" / "up.bin").read_bytes() == b"payload"

    form = FormData()
    form.add_field("dest", str(home), content_type="text/plain")   # multipart, but no file
    r = await client.post("/api/files/upload", data=form)
    assert r.status == 400

    form = FormData()
    form.add_field("dest", "/tmp")
    form.add_field("file", b"payload", filename="up.bin")
    r = await client.post("/api/files/upload", data=form)
    assert r.status == 403

    r = await client.post("/api/files/move", json={
        "src": str(home / "Desktop" / "inbox" / "up.bin"),
        "dst": str(home / "Desktop" / "moved.bin")})
    assert r.status == 200
    assert (home / "Desktop" / "moved.bin").exists()

    r = await client.post("/api/files/move", json={
        "src": str(home / "Desktop" / "moved.bin"), "dst": "/tmp/out.bin"})
    assert r.status == 403

    r = await client.post("/api/files/delete", json={"path": str(home)})
    assert r.status == 403                      # never the home dir itself
    r = await client.post("/api/files/delete", json={"path": "/etc/hostname"})
    assert r.status == 403
    r = await client.post("/api/files/delete", json={"path": str(home / "Desktop" / "gone")})
    assert r.status == 404
    r = await client.post("/api/files/delete", json={"path": str(home / "Desktop" / "moved.bin")})
    assert r.status == 200
    assert not (home / "Desktop" / "moved.bin").exists()
    r = await client.post("/api/files/delete", json={"path": str(home / "Desktop" / "inbox")})
    assert r.status == 200
    assert not (home / "Desktop" / "inbox").exists()


def test_file_upload_delete_move(srv, home):
    _file_upload_delete_move(srv, home)


# ── command endpoint (executor mocked: no Claude, no shell) ───────────────

@with_client
async def _command_endpoint(srv, client, monkeypatch):
    await login(client)
    seen = []

    async def fake_execute(text):
        seen.append(text)
        return {"input": text, "command": None, "output": "ok", "ai_mode": "test"}

    monkeypatch.setattr(srv, "execute_command", fake_execute)
    r = await client.post("/api/command", json={"command": "  list my desktop  "})
    assert r.status == 200
    assert (await r.json())["output"] == "ok"
    assert seen == ["list my desktop"]

    r = await client.post("/api/command", json={"command": "   "})
    assert r.status == 400


def test_command_endpoint(srv, monkeypatch):
    _command_endpoint(srv, monkeypatch)


@with_client
async def _non_ascii_token_is_401_not_500(srv, client):
    r = await client.get("/api/status", params={"token": "v1.99999999999.é"})
    assert r.status == 401
    r = await client.get("/", params={"token": "v1.99999999999.é"}, allow_redirects=False)
    assert r.status == 302
    r = await client.get("/api/authcheck", params={"token": "v1.99999999999.é"})
    assert r.status == 401


def test_non_ascii_token_is_401_not_500(srv):
    _non_ascii_token_is_401_not_500(srv)


@with_client
async def _sibling_of_home_is_outside_home(srv, client, home):
    """/home/alice-evil starts with the string /home/alice but is not inside it."""
    await login(client)
    sib = home.parent / (home.name + "-evil")
    sib.mkdir()
    (sib / "secret.txt").write_text("s")
    (home / "Desktop" / "mine.txt").write_text("m")

    r = await client.get("/api/files", params={"path": str(sib)})
    assert r.status == 403
    r = await client.get("/api/files/download", params={"path": str(sib / "secret.txt")})
    assert r.status == 403

    form = FormData()
    form.add_field("dest", str(sib))
    form.add_field("file", b"x", filename="dropped.txt")
    r = await client.post("/api/files/upload", data=form)
    assert r.status == 403
    assert not (sib / "dropped.txt").exists()

    r = await client.post("/api/files/delete", json={"path": str(sib / "secret.txt")})
    assert r.status == 403
    assert (sib / "secret.txt").exists()

    r = await client.post("/api/files/move", json={
        "src": str(home / "Desktop" / "mine.txt"), "dst": str(sib / "mine.txt")})
    assert r.status == 403
    assert (home / "Desktop" / "mine.txt").exists()


def test_sibling_of_home_is_outside_home(srv, home):
    _sibling_of_home_is_outside_home(srv, home)


def _raw_upload(filename: str) -> bytes:
    # Built by hand: aiohttp's own client percent-encodes the filename, a
    # browser or curl does not.
    return (b'--B\r\nContent-Disposition: form-data; name="file"; filename="'
            + filename.encode() + b'"\r\n\r\npayload\r\n--B--\r\n')


@pytest.mark.parametrize("filename", ["../../escaped.txt", "../escaped.txt", "{root}/escaped.txt"],
                         ids=["dotdot2", "dotdot1", "absolute"])
def test_upload_filename_cannot_escape_dest(srv, home, tmp_path, filename):
    filename = filename.format(root=tmp_path)

    @with_client
    async def go(srv, client):
        await login(client)
        r = await client.post("/api/files/upload", data=_raw_upload(filename),
                              headers={"Content-Type": "multipart/form-data; boundary=B"})
        assert r.status == 200
        saved = (await r.json())["path"]
        assert os.path.dirname(saved) == str(home / "Desktop")
        assert (home / "Desktop" / "escaped.txt").read_bytes() == b"payload"
    go(srv)
    assert not (tmp_path / "escaped.txt").exists()
    assert not (home.parent / "escaped.txt").exists()
    assert not (home / "escaped.txt").exists()


@pytest.mark.parametrize("filename", ["", "..", "a/.."], ids=["empty", "dotdot", "trailing-dotdot"])
def test_upload_rejects_nameless_file(srv, home, filename):
    @with_client
    async def go(srv, client):
        await login(client)
        r = await client.post("/api/files/upload", data=_raw_upload(filename),
                              headers={"Content-Type": "multipart/form-data; boundary=B"})
        assert r.status == 400
    go(srv)
