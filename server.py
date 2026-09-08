"""FiaOS — Remote Mac Control Center web server."""

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import os
import pty
import secrets
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import psutil

from executor import execute_command
import screencast

# --- Config ---
PORT = 9000
FIAOS_DIR = Path(__file__).parent
STATIC_DIR = FIAOS_DIR / "static"
def _abort_no_password():
    sys.exit(
        "FIAOS_PASSWORD env var is not set. Set one in your LaunchAgent plist "
        "(examples/com.fiaos.server.plist) before starting FiaOS -- it is the "
        "only thing standing between the open internet and your desktop.")


PASSWORD = os.environ.get("FIAOS_PASSWORD") or _abort_no_password()
# Which machine this copy of FiaOS runs on -- the MINI/M5/PC tabs in the UI.
# The cookie is deliberately NOT per-machine: session tokens are signed with the
# shared password, so one login covers every machine and switching tabs never
# asks for it again.
MACHINE = os.environ.get("FIAOS_MACHINE", "mini")
CLAUDE_BIN = (shutil.which("claude") or os.path.expanduser("~/.local/bin/claude"))
SESSION_COOKIE = "fiaos_session"
SESSION_EXPIRY = 86400  # 24 hours
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 300  # 5 minutes
MAX_IP_BUCKETS = 4096  # ceiling on the rate-limit table
SCREENSHOT_DIR = tempfile.mkdtemp(prefix="fiaos_screenshots_")
SESSION_FILE = FIAOS_DIR / ".sessions.json"
REVOKED_FILE = FIAOS_DIR / ".revoked.json"


# --- Session store (persisted to disk) ---
login_attempts: dict[str, list[float]] = {}  # ip -> [timestamps]


def _load_sessions() -> dict[str, float]:
    try:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text())
            now = time.time()
            return {k: v for k, v in data.items() if v > now}
    except Exception:
        pass
    return {}


def _write_json_atomic(path: Path, obj) -> bool:
    """Write via a temp file and rename, so a crash cannot truncate the real one.

    write_text() opens with O_TRUNC: the old contents are gone the instant it
    starts. Being killed mid-write therefore left a half-written .sessions.json,
    _load_sessions() caught the JSON error and returned {}, and every session on
    the machine was silently gone. Not hypothetical -- the mini's FiaOS has been
    SIGKILLed before (forge_guard reclaiming memory). os.replace is atomic on
    the same filesystem, so readers see either the whole old file or the whole
    new one, never a stump.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(obj))
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False


def _save_sessions():
    _write_json_atomic(SESSION_FILE, sessions)


def _load_revoked() -> dict[str, float]:
    """Tokens killed by an explicit logout, kept until they would have expired."""
    try:
        if REVOKED_FILE.exists():
            data = json.loads(REVOKED_FILE.read_text())
            now = time.time()
            return {k: v for k, v in data.items() if v > now}
    except Exception:
        pass
    return {}


def _save_revoked():
    _write_json_atomic(REVOKED_FILE, revoked)


sessions: dict[str, float] = _load_sessions()
revoked: dict[str, float] = _load_revoked()

def _sweep_sessions() -> int:
    """Drop tokens whose expiry has passed. Returns how many went.

    .sessions.json only ever GREW: _load_sessions() prunes at startup, but
    create_session() appended a new key per login (each token carries its own
    expiry, so no login ever reuses a key) and nothing removed the dead ones
    until the next restart. Found 2026-09-07: 9 tokens piled up on the M5 in a
    single day, all still live then, all dead weight 24 h later.

    Deliberately swept on write rather than on a timer: the file can only grow
    at login, so sweeping there bounds it exactly, and an idle machine keeps
    doing no work at all -- same rule the screen worker follows.
    """
    now = time.time()
    dead = [k for k, v in sessions.items() if v <= now]
    for k in dead:
        sessions.pop(k, None)
    return len(dead)



def _token_expiry(token: str) -> int | None:
    """Expiry stamped inside a signed token, or None if it is not one."""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _sweep_revoked() -> int:
    """Forget revocations for tokens that have expired on their own."""
    now = time.time()
    dead = [k for k, v in revoked.items() if v <= now]
    for k in dead:
        revoked.pop(k, None)
    return len(dead)


def revoke_session(token: str) -> bool:
    """Really kill a token, not just forget we issued it.

    Tokens are SIGNED (v1.<expiry>.<hmac>) so that one login covers all three
    machines without them talking to each other. The cost of that: dropping a
    token from .sessions.json revoked nothing, because valid_session() fell
    through to the signature check and happily re-validated it. Logout deleted
    the cookie and left the token itself working until its 24 h ran out.

    So logout now records the token here and valid_session() checks this FIRST.
    Bounded by construction: an entry is only worth keeping until the token's
    own expiry, and _sweep_revoked() drops it after that.

    Limit worth knowing: this list is per machine. Logging out on the M5 does
    not revoke the token on the mini -- they share a password, not state. The
    global kill switch is rotating FIAOS_PASSWORD, which invalidates every
    signature on every machine at once.
    """
    exp = _token_expiry(token)
    if exp is None:
        # Legacy random token: it only ever validated by being in `sessions`,
        # so removing it there is already a real revocation.
        return sessions.pop(token, None) is not None
    if exp <= time.time():
        return False                      # already dead, nothing to remember
    _sweep_revoked()
    revoked[token] = exp
    _save_revoked()
    return True


def client_ip(request: web.Request) -> str:
    """The real caller, not the tunnel.

    Every remote request arrives from 127.0.0.1: nginx on the VPS proxies into
    an SSH tunnel, so request.remote is identical for the entire internet. Rate
    limiting on that gave everybody ONE shared bucket -- ten wrong passwords
    from any stranger locked Matt out of his own machines for five minutes.

    nginx sets X-Real-IP from $remote_addr, OVERWRITING whatever the client
    sent, so it is trustworthy -- but only on a request that actually came in
    over loopback, or anyone on the LAN could just claim to be someone else.
    """
    peer = request.remote or "unknown"
    if peer in ("127.0.0.1", "::1"):
        for header in ("CF-Connecting-IP", "X-Real-IP"):
            v = request.headers.get(header, "").strip()
            if v:
                return v[:64]
    return peer


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in login_attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    # Drop the bucket instead of storing an empty list. login_attempts kept one
    # entry per IP for the life of the process and never removed any, so an
    # internet-facing box grew a row per scanner, forever.
    if attempts:
        login_attempts[ip] = attempts
    else:
        login_attempts.pop(ip, None)
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def record_attempt(ip: str):
    now = time.time()
    # Hard ceiling so a spray across many source IPs cannot grow this without
    # bound between sweeps. Evict whoever is closest to ageing out anyway.
    if len(login_attempts) >= MAX_IP_BUCKETS and ip not in login_attempts:
        for stale in sorted(login_attempts, key=lambda k: max(login_attempts[k]))[:64]:
            login_attempts.pop(stale, None)
    login_attempts.setdefault(ip, []).append(now)


def _sign(expiry: int) -> str:
    """Signed session token: v1.<expiry>.<hmac>.

    Signed rather than random so a login travels between machines. Every copy of
    FiaOS shares the same password, so a token minted on the mini verifies on the
    M5 without the two ever talking to each other -- which is the whole point:
    the MINI/M5/PC tabs switch machines without asking for the password again.
    """
    mac = hmac.new(PASSWORD.encode(), f"v1.{expiry}".encode(), hashlib.sha256)
    return f"v1.{expiry}.{mac.hexdigest()}"


def create_session() -> str:
    _sweep_sessions()          # bound the files: prune before we add
    if _sweep_revoked():
        _save_revoked()
    expiry = int(time.time() + SESSION_EXPIRY)
    token = _sign(expiry)
    sessions[token] = expiry
    _save_sessions()
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False

    # Revoked beats everything, including a perfectly good signature.
    if token in revoked:
        return False

    # A token this machine issued itself. Random tokens from before the signed
    # format still live in .sessions.json, so old logins stay valid.
    expiry = sessions.get(token)
    if expiry is not None:
        if time.time() > expiry:
            sessions.pop(token, None)
            return False
        return True

    # A token another machine issued. Same password on every machine, so the
    # signature can be checked here instead of making him log in again.
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        return False
    try:
        expiry = int(parts[1])
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    return hmac.compare_digest(token, _sign(expiry))


def get_token(request: web.Request) -> str:
    """The session token for this request: cookie first, ?token= second.

    Whichever one actually validates wins. Taking the cookie blindly meant a
    stale cookie shadowed a perfectly good ?token=, and the browser bounced back
    to the login page holding a valid session it was never allowed to use.
    """
    cookie = request.cookies.get(SESSION_COOKIE, "")
    query = request.query.get("token", "")
    for candidate in (cookie, query):
        if candidate and valid_session(candidate):
            return candidate
    return cookie or query


def require_auth(handler):
    async def wrapper(request: web.Request):
        token = get_token(request)
        if not valid_session(token):
            raise web.HTTPUnauthorized(text="Not authenticated")
        return await handler(request)
    return wrapper


# ═══════════════════════════════════════
# AUTH
# ═══════════════════════════════════════

async def handle_login_page(request: web.Request):
    token = get_token(request)
    if valid_session(token):
        raise web.HTTPFound("/")
    return web.FileResponse(STATIC_DIR / "login.html",
                            headers={"Cache-Control": "no-store"})


async def handle_login(request: web.Request):
    ip = client_ip(request)
    if check_rate_limit(ip):
        return web.json_response({"error": "Too many attempts. Try again later."}, status=429)
    # The app allows 100 MB bodies for file uploads, and that ceiling applied to
    # this unauthenticated endpoint too. A login is a few dozen bytes; read a
    # bounded amount so nobody can make us buffer a hundred megabytes to be told
    # their password is wrong.
    raw = await request.content.read(4097)
    if len(raw) > 4096:
        record_attempt(ip)
        return web.json_response({"error": "Bad request"}, status=413)
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        record_attempt(ip)
        return web.json_response({"error": "Bad request"}, status=400)
    password = data.get("password", "") if isinstance(data, dict) else ""
    # compare_digest raises on a non-string, and on any str outside ASCII.
    if not isinstance(password, str):
        password = ""
    if not hmac.compare_digest(password.encode("utf-8", "replace"),
                               PASSWORD.encode("utf-8", "replace")):
        record_attempt(ip)
        return web.json_response({"error": "Wrong password"}, status=401)
    token = create_session()
    resp = web.json_response({"ok": True, "token": token})
    # Set cookie — works for both HTTP (local) and HTTPS (remote)
    is_https = request.headers.get("X-Forwarded-Proto") == "https" or request.secure
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_EXPIRY, httponly=False,
                     samesite="None" if is_https else "Lax",
                     secure=is_https)
    return resp


async def handle_logout(request: web.Request):
    token = get_token(request)
    revoke_session(token)
    if sessions.pop(token, None) is not None:
        _save_sessions()
    resp = web.HTTPFound("/login")
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def handle_authcheck(request: web.Request):
    """For nginx auth_request: 200 if this browser has a valid FiaOS session.

    Lets the VNC screen at /vnc/ sit behind the same login as the rest of the
    portal, so there's no second password to type.
    """
    if valid_session(get_token(request)):
        return web.Response(status=200)
    return web.Response(status=401)


async def handle_index(request: web.Request):
    token = get_token(request)
    if not valid_session(token):
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-store"})


# ═══════════════════════════════════════
# COMMAND EXECUTOR
# ═══════════════════════════════════════

@require_auth
async def handle_claude_stream(request: web.Request):
    """Stream `claude -p --output-format stream-json` output as SSE.

    Used by Ohm so the browser sees live progress (tool use, token counts)
    instead of staring at "still working" for 5+ minutes.
    """
    data = await request.json()
    prompt = data.get("prompt", "")
    # Session params (added 2026-05-12) — resume an existing Claude Code session
    # via --resume <uuid>, OR pin a new session to a known UUID via --session-id.
    session_id = data.get("session_id")
    resume_session_id = data.get("resume_session_id")
    if not prompt:
        return web.json_response({"error": "No prompt"}, status=400)

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    cmd_args = [
        CLAUDE_BIN,
        "--dangerously-skip-permissions",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]
    if resume_session_id:
        cmd_args.extend(["--resume", resume_session_id])
    elif session_id:
        cmd_args.extend(["--session-id", session_id])

    proc = await asyncio.create_subprocess_exec(
        *cmd_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            await resp.write(b"data: " + line.rstrip(b"\n") + b"\n\n")
    except (asyncio.CancelledError, ConnectionResetError):
        proc.kill()
    finally:
        try:
            await proc.wait()
        except Exception:
            pass
        try:
            await resp.write(b"event: end\ndata: {}\n\n")
        except Exception:
            pass
    return resp



@require_auth
async def handle_command(request: web.Request):
    data = await request.json()
    user_input = data.get("command", "").strip()
    if not user_input:
        return web.json_response({"error": "No command provided"}, status=400)
    result = await execute_command(user_input)
    return web.json_response(result)


# ═══════════════════════════════════════
# FILE BROWSER + UPLOAD/DOWNLOAD
# ═══════════════════════════════════════

@require_auth
async def handle_files(request: web.Request):
    path = request.query.get("path", os.path.expanduser("~/Desktop"))
    path = os.path.expanduser(path)  # handle ~ in paths
    home = os.path.expanduser("~")
    real_path = os.path.realpath(path)
    if not real_path.startswith(home):
        return web.json_response({"error": "Access denied"}, status=403)
    if not os.path.exists(real_path):
        return web.json_response({"error": "Path not found"}, status=404)
    if os.path.isfile(real_path):
        stat = os.stat(real_path)
        return web.json_response({
            "type": "file", "path": real_path,
            "name": os.path.basename(real_path),
            "size": stat.st_size, "modified": stat.st_mtime,
        })
    entries = []
    try:
        for entry in sorted(os.scandir(real_path), key=lambda e: (not e.is_dir(), e.name.lower())):
            if entry.name.startswith("."):
                continue
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified": stat.st_mtime,
                })
            except OSError:
                continue
    except PermissionError:
        return web.json_response({"error": "Permission denied"}, status=403)
    return web.json_response({
        "type": "directory", "path": real_path,
        "parent": os.path.dirname(real_path) if real_path != home else None,
        "entries": entries,
    })


@require_auth
async def handle_file_download(request: web.Request):
    """Download a file from the Mac."""
    path = request.query.get("path", "")
    home = os.path.expanduser("~")
    real_path = os.path.realpath(path)
    if not real_path.startswith(home):
        return web.json_response({"error": "Access denied"}, status=403)
    if not os.path.isfile(real_path):
        return web.json_response({"error": "Not a file"}, status=404)
    return web.FileResponse(real_path, headers={
        "Content-Disposition": f'attachment; filename="{os.path.basename(real_path)}"'
    })


@require_auth
async def handle_file_upload(request: web.Request):
    """Upload a file to the Mac."""
    home = os.path.expanduser("~")
    reader = await request.multipart()
    dest_dir = home + "/Desktop"  # default
    file_field = None
    async for field in reader:
        if field.name == "dest":
            dest_dir = (await field.text()).strip() or dest_dir
        elif field.name == "file":
            file_field = field
            filename = field.filename
            real_dest = os.path.realpath(dest_dir)
            if not real_dest.startswith(home):
                return web.json_response({"error": "Access denied"}, status=403)
            os.makedirs(real_dest, exist_ok=True)
            filepath = os.path.join(real_dest, filename)
            with open(filepath, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)
            return web.json_response({"ok": True, "path": filepath, "name": filename})
    return web.json_response({"error": "No file provided"}, status=400)


@require_auth
async def handle_file_delete(request: web.Request):
    """Delete a file or empty directory."""
    data = await request.json()
    path = data.get("path", "")
    home = os.path.expanduser("~")
    real_path = os.path.realpath(path)
    if not real_path.startswith(home) or real_path == home:
        return web.json_response({"error": "Access denied"}, status=403)
    if not os.path.exists(real_path):
        return web.json_response({"error": "Not found"}, status=404)
    if os.path.isfile(real_path):
        os.remove(real_path)
    elif os.path.isdir(real_path):
        shutil.rmtree(real_path)
    return web.json_response({"ok": True})


@require_auth
async def handle_file_move(request: web.Request):
    """Move/rename a file."""
    data = await request.json()
    src = data.get("src", "")
    dst = data.get("dst", "")
    home = os.path.expanduser("~")
    if not os.path.realpath(src).startswith(home) or not os.path.realpath(dst).startswith(home):
        return web.json_response({"error": "Access denied"}, status=403)
    shutil.move(src, dst)
    return web.json_response({"ok": True})


# ═══════════════════════════════════════
# SYSTEM STATUS
# ═══════════════════════════════════════

def _mem_free_percent():
    """Percentage of memory the kernel considers genuinely free.

    This is what Activity Monitor's pressure gauge reads. psutil's percent
    counts inactive and compressed pages as used, so a Mac holding models
    resident on purpose reads ~50% forever whether it is healthy or drowning.
    Falls back to psutil's available/total if the sysctl is ever missing.
    """
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"],
                             capture_output=True, text=True, timeout=3)
        v = int(out.stdout.strip())
        if 0 <= v <= 100:
            return v
    except Exception:
        pass
    m = psutil.virtual_memory()
    return int(round(m.available / m.total * 100))


@require_auth
async def handle_status(request: web.Request):
    # interval=None reads the delta since the last call — non-blocking. With
    # interval=0.5 this slept inside the event loop, stalling the terminal and
    # voice sockets for half a second on every status poll.
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    # Battery (laptops)
    battery = psutil.sensors_battery()
    bat_info = None
    if battery:
        bat_info = {"percent": battery.percent, "plugged": battery.power_plugged}
    # Network
    net = psutil.net_io_counters()
    return web.json_response({
        "cpu_percent": cpu_percent,
        "ram_total_gb": round(mem.total / (1024**3), 1),
        "ram_used_gb": round(mem.used / (1024**3), 1),
        "ram_percent": mem.percent,
        # The honest pair: how much is really free, and what that means.
        # ram_percent is kept so nothing that already reads it breaks.
        "ram_free_percent": _mem_free_percent(),
        "ram_available_gb": round(mem.available / (1024**3), 1),
        "mem_state": ("ok" if _mem_free_percent() >= 30 else
                      "tight" if _mem_free_percent() >= 15 else "critical"),
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_percent": round(disk.percent, 1),
        "boot_time": psutil.boot_time(),
        "battery": bat_info,
        "net_sent_gb": round(net.bytes_sent / (1024**3), 2),
        "net_recv_gb": round(net.bytes_recv / (1024**3), 2),
    })


# ═══════════════════════════════════════
# SCREENSHOT / SCREEN VIEWER
# ═══════════════════════════════════════

@require_auth
async def handle_screenshot(request: web.Request):
    """Capture the screen and return as JPEG."""
    quality = request.query.get("quality", "50")
    # a sleeping display captures black — nudge it awake first
    await (await asyncio.create_subprocess_exec("/usr/bin/caffeinate", "-u", "-t", "3")).wait()
    filepath = os.path.join(SCREENSHOT_DIR, "screen.jpg")
    # Remove old screenshot
    if os.path.exists(filepath):
        os.remove(filepath)
    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-x", "-t", "jpg", filepath,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(filepath) or os.path.getsize(filepath) < 100:
        # screencapture failed — likely no Screen Recording permission
        # Generate a placeholder image with error message
        return web.json_response({
            "error": "Screen Recording permission required. Go to System Settings > Privacy & Security > Screen Recording and enable Terminal (or Python).",
        }, status=403)
    # Compress with sips — downscale too, or a 3440-wide frame ships ~425 KB
    # every refresh through the ssh tunnel and starves the terminal socket
    await (await asyncio.create_subprocess_exec(
        "sips", "-s", "formatOptions", quality, "-Z", "1600", filepath,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate()
    # Don't ship a frame the browser already has. A desktop sitting still was
    # costing about 1.4 Mbps through the VPS for no reason; now it costs nothing.
    with open(filepath, "rb") as fh:
        data = fh.read()
    stamp = hashlib.blake2b(data, digest_size=8).hexdigest()
    if request.query.get("last") == stamp:
        return web.Response(status=204, headers={"X-Frame-Hash": stamp})
    return web.Response(body=data, headers={"Content-Type": "image/jpeg",
                                            "Cache-Control": "no-store",
                                            "X-Frame-Hash": stamp})


@require_auth
# MOUSE / KEYBOARD CONTROL
# ═══════════════════════════════════════

@require_auth
async def handle_mouse(request: web.Request):
    """Control mouse via Quartz (CoreGraphics) — no cliclick or Accessibility needed."""
    data = await request.json()
    action = data.get("action", "click")  # click, move, doubleclick, rightclick, scroll
    x = data.get("x", 0)
    y = data.get("y", 0)

    event_map = {
        "click": "kCGEventLeftMouseDown,kCGEventLeftMouseUp,kCGMouseButtonLeft",
        "doubleclick": "kCGEventLeftMouseDown,kCGEventLeftMouseUp,kCGMouseButtonLeft,2",
        "rightclick": "kCGEventRightMouseDown,kCGEventRightMouseUp,kCGMouseButtonRight",
        "move": "kCGEventMouseMoved,None,kCGMouseButtonLeft",
        "scroll": "scroll",
    }

    if action not in event_map:
        return web.json_response({"error": "Unknown action"}, status=400)

    if action == "scroll":
        direction = data.get("direction", "down")
        amount = data.get("amount", 3)
        scroll_val = -amount if direction == "down" else amount
        script = f"""\
import Quartz
e = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, {scroll_val})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
"""
    elif action == "move":
        script = f"""\
import Quartz
e = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, ({x}, {y}), Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
"""
    elif action == "doubleclick":
        script = f"""\
import Quartz, time
pos = ({x}, {y})
for i in range(2):
    down = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseDown, pos, Quartz.kCGMouseButtonLeft)
    down.setIntegerValueField(Quartz.kCGMouseEventClickState, i+1)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventLeftMouseUp, pos, Quartz.kCGMouseButtonLeft)
    up.setIntegerValueField(Quartz.kCGMouseEventClickState, i+1)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
    if i == 0: time.sleep(0.05)
"""
    else:
        # click or rightclick
        down_evt = "kCGEventLeftMouseDown" if action == "click" else "kCGEventRightMouseDown"
        up_evt = "kCGEventLeftMouseUp" if action == "click" else "kCGEventRightMouseUp"
        btn = "kCGMouseButtonLeft" if action == "click" else "kCGMouseButtonRight"
        script = f"""\
import Quartz
pos = ({x}, {y})
down = Quartz.CGEventCreateMouseEvent(None, Quartz.{down_evt}, pos, Quartz.{btn})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
up = Quartz.CGEventCreateMouseEvent(None, Quartz.{up_evt}, pos, Quartz.{btn})
Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
"""

    venv_python = str(FIAOS_DIR / ".venv" / "bin" / "python3")
    proc = await asyncio.create_subprocess_exec(
        venv_python, "-c", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode().strip()
        return web.json_response({"error": f"Mouse control failed: {err}"}, status=500)

    return web.json_response({"ok": True, "action": action, "x": x, "y": y})


@require_auth
async def handle_keyboard(request: web.Request):
    """Send keystrokes via osascript."""
    data = await request.json()
    action = data.get("action", "type")  # type, keystroke, hotkey
    text = data.get("text", "")
    key = data.get("key", "")
    modifiers = data.get("modifiers", [])  # ["command", "shift", "option", "control"]

    if action == "type":
        # Type text
        escaped = text.replace('"', '\\"')
        script = f'''
        tell application "System Events"
            keystroke "{escaped}"
        end tell'''
    elif action == "keystroke":
        # Single key press (e.g., "return", "tab", "escape")
        key_map = {
            "return": "return", "enter": "return", "tab": "tab",
            "escape": "escape", "space": "space", "delete": "delete",
            "backspace": "delete", "up": "up arrow", "down": "down arrow",
            "left": "left arrow", "right": "right arrow",
            "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4",
            "f5": "F5", "f6": "F6", "f7": "F7", "f8": "F8",
            "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
        }
        mapped = key_map.get(key.lower(), key)
        mod_str = ""
        if modifiers:
            mod_parts = [f"{m} down" for m in modifiers]
            mod_str = " using {" + ", ".join(mod_parts) + "}"
        script = f'''
        tell application "System Events"
            key code (key code "{mapped}"){mod_str}
        end tell'''
        # Simpler approach
        if not modifiers:
            script = f'''
            tell application "System Events"
                keystroke "{key}"
            end tell''' if len(key) == 1 else f'''
            tell application "System Events"
                key code {_key_to_code(mapped)}
            end tell'''
        else:
            mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
            if len(key) == 1:
                script = f'''
                tell application "System Events"
                    keystroke "{key}"{mod_str}
                end tell'''
            else:
                script = f'''
                tell application "System Events"
                    key code {_key_to_code(mapped)}{mod_str}
                end tell'''
    elif action == "hotkey":
        # Keyboard shortcut like Cmd+C
        mod_str = " using {" + ", ".join(f"{m} down" for m in modifiers) + "}"
        script = f'''
        tell application "System Events"
            keystroke "{key}"{mod_str}
        end tell'''
    else:
        return web.json_response({"error": "Unknown action"}, status=400)

    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    err = stderr.decode().strip()
    if proc.returncode != 0 and err:
        return web.json_response({"error": err}, status=500)
    return web.json_response({"ok": True})


def _key_to_code(key_name: str) -> int:
    """Map key names to macOS key codes."""
    codes = {
        "return": 36, "tab": 48, "space": 49, "delete": 51,
        "escape": 53, "up arrow": 126, "down arrow": 125,
        "left arrow": 123, "right arrow": 124,
        "F1": 122, "F2": 120, "F3": 99, "F4": 118,
        "F5": 96, "F6": 97, "F7": 98, "F8": 100,
        "F9": 101, "F10": 109, "F11": 103, "F12": 111,
    }
    return codes.get(key_name, 36)


# ═══════════════════════════════════════
# CLIPBOARD
# ═══════════════════════════════════════

@require_auth
async def handle_clipboard_get(request: web.Request):
    proc = await asyncio.create_subprocess_exec(
        "pbpaste", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return web.json_response({"text": stdout.decode(errors="replace")})


@require_auth
async def handle_clipboard_set(request: web.Request):
    data = await request.json()
    text = data.get("text", "")
    proc = await asyncio.create_subprocess_exec(
        "pbcopy", stdin=asyncio.subprocess.PIPE,
    )
    await proc.communicate(text.encode())
    return web.json_response({"ok": True})


# ═══════════════════════════════════════
# VOLUME CONTROL
# ═══════════════════════════════════════

@require_auth
async def handle_volume(request: web.Request):
    if request.method == "GET":
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", "output volume of (get volume settings)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        # Check mute
        proc2 = await asyncio.create_subprocess_exec(
            "osascript", "-e", "output muted of (get volume settings)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await proc2.communicate()
        return web.json_response({
            "volume": int(stdout.decode().strip() or "0"),
            "muted": stdout2.decode().strip() == "true",
        })
    else:
        data = await request.json()
        if "volume" in data:
            vol = max(0, min(100, int(data["volume"])))
            await (await asyncio.create_subprocess_exec(
                "osascript", "-e", f"set volume output volume {vol}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )).communicate()
        if "muted" in data:
            muted = "true" if data["muted"] else "false"
            await (await asyncio.create_subprocess_exec(
                "osascript", "-e", f"set volume output muted {muted}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )).communicate()
        return web.json_response({"ok": True})


# ═══════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════

@require_auth
async def handle_notification(request: web.Request):
    data = await request.json()
    title = data.get("title", "FiaOS")
    message = data.get("message", "")
    escaped_title = title.replace('"', '\\"')
    escaped_msg = message.replace('"', '\\"')
    script = f'display notification "{escaped_msg}" with title "{escaped_title}"'
    await (await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate()
    return web.json_response({"ok": True})


# ═══════════════════════════════════════
# PROCESS MANAGER
# ═══════════════════════════════════════

@require_auth
async def handle_processes(request: web.Request):
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "status"]):
        try:
            info = p.info
            if info["memory_percent"] and info["memory_percent"] > 0.1:
                procs.append({
                    "pid": info["pid"],
                    "name": info["name"],
                    "cpu": round(info["cpu_percent"] or 0, 1),
                    "mem": round(info["memory_percent"] or 0, 1),
                    "status": info["status"],
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda p: p["mem"], reverse=True)
    return web.json_response({"processes": procs[:50]})


@require_auth
async def handle_kill_process(request: web.Request):
    data = await request.json()
    pid = data.get("pid")
    if not pid:
        return web.json_response({"error": "No PID"}, status=400)
    try:
        p = psutil.Process(int(pid))
        p.terminate()
        return web.json_response({"ok": True, "name": p.name()})
    except psutil.NoSuchProcess:
        return web.json_response({"error": "Process not found"}, status=404)
    except psutil.AccessDenied:
        return web.json_response({"error": "Access denied"}, status=403)


# ═══════════════════════════════════════
# APP LAUNCHER
# ═══════════════════════════════════════

@require_auth
async def handle_apps(request: web.Request):
    """List installed applications."""
    apps = []
    for app_dir in ["/Applications", os.path.expanduser("~/Applications"), os.path.expanduser("~/Desktop")]:
        if os.path.isdir(app_dir):
            for item in os.listdir(app_dir):
                if item.endswith(".app"):
                    apps.append({"name": item.replace(".app", ""), "path": os.path.join(app_dir, item)})
    apps.sort(key=lambda a: a["name"].lower())
    return web.json_response({"apps": apps})


@require_auth
async def handle_open_app(request: web.Request):
    """Open an application."""
    data = await request.json()
    app_name = data.get("name", "")
    proc = await asyncio.create_subprocess_exec(
        "open", "-a", app_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        return web.json_response({"error": stderr.decode().strip()}, status=500)
    return web.json_response({"ok": True})


@require_auth
async def handle_quit_app(request: web.Request):
    """Quit an application."""
    data = await request.json()
    app_name = data.get("name", "")
    script = f'tell application "{app_name}" to quit'
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return web.json_response({"ok": True})


# ═══════════════════════════════════════
# SLEEP / WAKE / LOCK
# ═══════════════════════════════════════

@require_auth
async def handle_system_action(request: web.Request):
    data = await request.json()
    action = data.get("action", "")
    if action == "sleep":
        await (await asyncio.create_subprocess_exec(
            "pmset", "sleepnow",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()
    elif action == "lock":
        # Activate screensaver (locks if password required)
        await (await asyncio.create_subprocess_exec(
            "osascript", "-e", 'tell application "System Events" to keystroke "q" using {command down, control down}',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()
    elif action == "brightness_up":
        await (await asyncio.create_subprocess_exec(
            "osascript", "-e", 'tell application "System Events" to key code 144',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()
    elif action == "brightness_down":
        await (await asyncio.create_subprocess_exec(
            "osascript", "-e", 'tell application "System Events" to key code 145',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )).communicate()
    else:
        return web.json_response({"error": "Unknown action"}, status=400)
    return web.json_response({"ok": True, "action": action})


# ═══════════════════════════════════════
# SCREEN WEBSOCKET (our own live view)
# ═══════════════════════════════════════

@require_auth
async def handle_screen_ws(request: web.Request):
    """The live screen: changed tiles down, mouse and keyboard up.

    Two child processes per session, both cleaned up when the socket closes: a
    capture worker that only emits tiles that changed, and the input driver.
    """
    ws = web.WebSocketResponse(heartbeat=25, max_msg_size=1 << 20)
    await ws.prepare(request)

    pw, ph = screencast.screen_size()      # pixels — must match the worker's grid
    sw, sh = screencast.screen_points()    # points — what mouse events are in
    want_w = max(640, min(2560, int(request.query.get("w", 1720))))
    fps = max(2, min(20, int(request.query.get("fps", 10))))
    quality = max(20, min(90, int(request.query.get("q", 55))))
    geo = screencast.geometry(pw, ph, want_w)

    await ws.send_str(json.dumps({"type": "info", "screen": {"w": pw, "h": ph},
                                  "grid": geo, "fps": fps}))

    venv_python = str(FIAOS_DIR / ".venv" / "bin" / "python3")
    worker = await screencast.acquire_worker(venv_python, str(FIAOS_DIR / "screen_worker.py"),
                                             want_w, fps, quality)
    inp = await screencast.start_input(venv_python, str(FIAOS_DIR / "input_helper.py"))
    # The display is allowed to sleep like any Mac; -u wakes it the moment a
    # viewer connects and -d holds it on only for as long as this socket lives.
    awake = await asyncio.create_subprocess_exec("/usr/bin/caffeinate", "-d", "-u")
    sent = {"tiles": 0, "bytes": 0}

    async def pump():
        try:
            async for index, jpeg in screencast.tiles(worker):
                await ws.send_bytes(screencast.pack(index, jpeg))
                sent["tiles"] += 1
                sent["bytes"] += len(jpeg)
        except (asyncio.IncompleteReadError, asyncio.CancelledError,
                ConnectionResetError, RuntimeError):
            pass

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            kind = ev.get("t")
            if kind == "refresh":              # viewer wants the whole screen again
                if worker.stdin:
                    worker.stdin.write(b"R\n")
                    await worker.stdin.drain()
                continue
            if kind == "stats":
                await ws.send_str(json.dumps({"type": "stats", **sent}))
                continue
            line = screencast.encode_event(ev, sw, sh)
            if line and inp.stdin:
                inp.stdin.write(line.encode())
                await inp.stdin.drain()
    finally:
        task.cancel()
        # The capture worker is parked, not killed: a phone backgrounding the
        # tab reconnects constantly and a cold worker costs ~166 ms every time.
        await screencast.release_worker(worker, want_w, fps, quality)
        for proc in (inp, awake):
            if proc.returncode is None:
                try:
                    proc.kill()               # no lingering input driver, ever
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except Exception:
                    pass
    return ws


# ═══════════════════════════════════════
# TERMINAL WEBSOCKET
# ═══════════════════════════════════════

# Commands that would kill FiaOS itself — blocked in terminal
_PROTECTED_PATTERNS = [
    r"launchctl\s+(unload|remove|stop).*fiaos",
    r"launchctl\s+(unload|remove|stop).*caffeinate",
    r"pkill.*(server\.py|fiaos|personaplex|caffeinate)",
    r"kill.*(server\.py|fiaos)",
    r"killall.*[Pp]ython",
]


def _is_self_destructive(cmd: str) -> bool:
    import re
    for pattern in _PROTECTED_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True
    return False


@require_auth
async def handle_terminal_ws(request: web.Request):
    """PTY-backed interactive shell — supports claude, vim, top, persistent cd, etc."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Spawn an interactive login zsh inside a PTY
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["LANG"] = env.get("LANG", "en_US.UTF-8")
    home = os.path.expanduser("~")

    try:
        proc = subprocess.Popen(
            ["/bin/zsh", "-l", "-i"],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=env, cwd=home,
            preexec_fn=os.setsid,
            close_fds=True,
        )
    except Exception as e:
        await ws.send_str(f"[shell spawn failed: {e}]\n")
        os.close(master_fd); os.close(slave_fd)
        return ws

    # Parent doesn't need slave end
    os.close(slave_fd)

    # Make master non-blocking
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    loop = asyncio.get_event_loop()
    closed = False

    async def pty_to_ws():
        """Forward PTY output -> WebSocket as binary chunks."""
        while not closed:
            try:
                # Wait until master_fd is readable
                ready = asyncio.Event()
                def _on_readable():
                    if not ready.is_set():
                        ready.set()
                loop.add_reader(master_fd, _on_readable)
                try:
                    await ready.wait()
                finally:
                    try:
                        loop.remove_reader(master_fd)
                    except Exception:
                        pass
                # Drain whatever is available
                try:
                    data = os.read(master_fd, 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    break
                if not data:
                    break
                try:
                    await ws.send_bytes(data)
                except (ConnectionResetError, RuntimeError):
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                break

    pty_task = asyncio.create_task(pty_to_ws())

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                # Two flavors: JSON control messages, or plain text input
                payload = msg.data
                handled = False
                if payload.startswith("{"):
                    try:
                        d = json.loads(payload)
                        kind = d.get("type")
                        if kind == "input":
                            os.write(master_fd, d.get("data", "").encode("utf-8"))
                            handled = True
                        elif kind == "resize":
                            rows = int(d.get("rows", 24)); cols = int(d.get("cols", 80))
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
                            handled = True
                    except (json.JSONDecodeError, ValueError, KeyError, OSError):
                        handled = False
                if not handled:
                    # Legacy line-mode: append newline so the shell runs the command
                    if _is_self_destructive(payload):
                        await ws.send_str("\n[BLOCKED] Can't kill FiaOS services from remote terminal.\n")
                    else:
                        os.write(master_fd, (payload + "\n").encode("utf-8"))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                try:
                    os.write(master_fd, msg.data)
                except OSError:
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    finally:
        closed = True
        pty_task.cancel()
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGHUP)
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try: proc.kill()
                except Exception: pass
    return ws


# ═══════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════

def _sweep_strays():
    """Kill screen workers or input drivers orphaned by a previous run.

    They are children of the server; if the server is killed outright they lose
    their parent and would otherwise keep capturing the screen indefinitely.
    """
    for name in ("screen_worker.py", "input_helper.py"):
        try:
            out = subprocess.run(["/usr/bin/pgrep", "-f", name],
                                 capture_output=True, text=True).stdout.split()
            for pid in out:
                if int(pid) != os.getpid():
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"[FiaOS] cleared stray {name} ({pid})")
        except Exception:
            pass


def create_app() -> web.Application:
    _sweep_strays()
    app = web.Application(client_max_size=100 * 1024 * 1024)  # 100MB upload limit

    # Auth
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/login", handle_login)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/api/authcheck", handle_authcheck)
    app.router.add_get("/", handle_index)

    # Command
    app.router.add_post("/api/command", handle_command)
    app.router.add_post("/api/claude_stream", handle_claude_stream)

    # Files
    app.router.add_get("/api/files", handle_files)
    app.router.add_get("/api/files/download", handle_file_download)
    app.router.add_post("/api/files/upload", handle_file_upload)
    app.router.add_post("/api/files/delete", handle_file_delete)
    app.router.add_post("/api/files/move", handle_file_move)

    # System
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/processes", handle_processes)
    app.router.add_post("/api/processes/kill", handle_kill_process)
    app.router.add_post("/api/system", handle_system_action)

    # Screen
    app.router.add_get("/api/screenshot", handle_screenshot)

    # Input
    app.router.add_post("/api/mouse", handle_mouse)
    app.router.add_post("/api/keyboard", handle_keyboard)

    # Clipboard
    app.router.add_get("/api/clipboard", handle_clipboard_get)
    app.router.add_post("/api/clipboard", handle_clipboard_set)

    # Volume
    app.router.add_get("/api/volume", handle_volume)
    app.router.add_post("/api/volume", handle_volume)

    # Apps
    app.router.add_get("/api/apps", handle_apps)
    app.router.add_post("/api/apps/open", handle_open_app)
    app.router.add_post("/api/apps/quit", handle_quit_app)

    # Notifications
    app.router.add_post("/api/notification", handle_notification)

    # WebSockets
    app.router.add_get("/api/screen", handle_screen_ws)
    app.router.add_get("/api/terminal", handle_terminal_ws)

    # Static
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")

    return app


if __name__ == "__main__":
    print(f"[FiaOS] Starting on port {PORT}")
    print(f"[FiaOS] Dashboard: http://localhost:{PORT}")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
