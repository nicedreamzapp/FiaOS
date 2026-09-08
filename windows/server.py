"""FiaOS — Remote Mac Control Center web server."""

import asyncio
import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
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
MACHINE_KEY = os.environ.get("FIAOS_MACHINE", "mini")  # mini | m5 | pc
PASSWORD = os.environ.get("FIAOS_PASSWORD") or sys.exit(
    "FIAOS_PASSWORD is not set. Export the same password the Macs use before starting FiaOS.")
# Which machine this copy of FiaOS runs on -- the MINI/M5/PC tabs in the UI.
# The cookie is deliberately NOT per-machine: session tokens are signed with the
# shared password, so one login covers every machine and switching tabs never
# asks for it again.
MACHINE = os.environ.get("FIAOS_MACHINE", "mini")
SESSION_COOKIE = "fiaos_session"
SESSION_EXPIRY = 86400  # 24 hours
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 300  # 5 minutes
MAX_IP_BUCKETS = 4096  # ceiling on the rate-limit table
SCREENSHOT_DIR = tempfile.mkdtemp(prefix="fiaos_screenshots_")
SESSION_FILE = FIAOS_DIR / ".sessions.json"
REVOKED_FILE = FIAOS_DIR / ".revoked.json"

# Windows venv layout. The Mac build hardcoded .venv/bin/python3 in three
# places; sys.executable is already the venv interpreter when the server was
# started with it, which is what the service does.
VENV_PYTHON = str(FIAOS_DIR / ".venv" / "Scripts" / "python.exe")
if not os.path.exists(VENV_PYTHON):
    VENV_PYTHON = sys.executable

# System drive, not "/" — psutil.disk_usage("/") raises on Windows.
SYSTEM_DRIVE = os.environ.get("SystemDrive", "C:") + "\\"


def _claude_bin() -> str:
    """Where Claude Code lives on this machine.

    The Mac build hardcoded one absolute path. On Windows it is a .cmd shim
    whose location varies with how it was installed, so look rather than guess.
    """
    found = shutil.which("claude") or shutil.which("claude.cmd")
    if found:
        return found
    for cand in (Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
                 Path.home() / ".local" / "bin" / "claude.exe",
                 Path.home() / "AppData" / "Local" / "Programs" / "claude" / "claude.exe"):
        if cand.exists():
            return str(cand)
    return "claude"        # let the OS resolve it, and report the error honestly


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
        _claude_bin(),
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
    """
    # No Windows equivalent of kern.memorystatus_level, and none is needed:
    # Windows has no compressed-but-counted-as-used class of page, so
    # available/total is already the honest number Activity Monitor approximates.
    m = psutil.virtual_memory()
    return int(round(m.available / m.total * 100))


@require_auth
async def handle_status(request: web.Request):
    # interval=None reads the delta since the last call — non-blocking. With
    # interval=0.5 this slept inside the event loop, stalling the terminal and
    # voice sockets for half a second on every status poll.
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(SYSTEM_DRIVE)
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

# caffeinate's replacement. SetThreadExecutionState is per-thread and only
# holds while that thread lives, so it is pinned to the event loop thread and
# refcounted: two viewers must both leave before the display may sleep again.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
_display_holders = 0


def _display_hold(on: bool):
    """Keep the screen awake while someone is watching, and let it sleep after."""
    global _display_holders
    _display_holders = max(0, _display_holders + (1 if on else -1))
    try:
        if _display_holders:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def _wake_display():
    """Nudge a blanked display on, so a capture is not a black rectangle."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        if not _display_holders:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def _grab_jpeg(path: str, quality: int, max_w: int = 1600) -> bool:
    """One still frame to disk. Same BitBlt the stream worker uses."""
    try:
        import numpy as np
        from PIL import Image
        from screen_worker import Capture
        arr = Capture().grab()
        if arr is None:
            return False
        img = Image.fromarray(np.ascontiguousarray(arr))
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)),
                             Image.BILINEAR)
        img.save(path, "JPEG", quality=max(20, min(90, quality)))
        return True
    except Exception as e:
        print(f"[FiaOS] screenshot failed: {e}")
        return False


@require_auth
async def handle_screenshot(request: web.Request):
    """Capture the screen and return as JPEG."""
    quality = request.query.get("quality", "50")
    # a sleeping display captures black — nudge it awake first
    _wake_display()
    filepath = os.path.join(SCREENSHOT_DIR, "screen.jpg")
    # Remove old screenshot
    if os.path.exists(filepath):
        os.remove(filepath)
    # Downscale here too, or a 1920-wide frame ships hundreds of KB every
    # refresh through the ssh tunnel and starves the terminal socket.
    ok = await asyncio.get_running_loop().run_in_executor(
        None, _grab_jpeg, filepath, int(quality) if str(quality).isdigit() else 50)
    if not ok or not os.path.exists(filepath) or os.path.getsize(filepath) < 100:
        return web.json_response({"error": "Screen capture failed."}, status=500)
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


# ═══════════════════════════════════════
# MOUSE / KEYBOARD CONTROL
# ═══════════════════════════════════════

@require_auth
async def handle_mouse(request: web.Request):
    """Control the mouse. Same SendInput path the live screen view uses."""
    data = await request.json()
    action = data.get("action", "click")  # click, move, doubleclick, rightclick, scroll
    x = int(data.get("x", 0))
    y = int(data.get("y", 0))

    if action == "scroll":
        amount = int(data.get("amount", 3))
        dy = -amount if data.get("direction", "down") == "down" else amount
        ev = {"t": "scroll", "dy": dy * 40, "dx": 0}   # lines -> the pixel deltas the driver expects
    elif action == "move":
        ev = {"t": "move", "x": x, "y": y}
    elif action == "click":
        ev = {"t": "click", "x": x, "y": y, "btn": "left"}
    elif action == "rightclick":
        ev = {"t": "click", "x": x, "y": y, "btn": "right"}
    elif action == "doubleclick":
        ev = {"t": "dblclick", "x": x, "y": y}
    else:
        return web.json_response({"error": "Unknown action"}, status=400)

    try:
        await asyncio.get_running_loop().run_in_executor(None, _input_handle, ev)
    except Exception as e:
        return web.json_response({"error": f"Mouse control failed: {e}"}, status=500)
    return web.json_response({"ok": True, "action": action, "x": x, "y": y})


@require_auth
async def handle_keyboard(request: web.Request):
    """Send keystrokes. Same SendInput path the live screen view uses."""
    data = await request.json()
    action = data.get("action", "type")   # type, keystroke, hotkey
    text = data.get("text", "")
    key = data.get("key", "")
    # The API's vocabulary is macOS's; the driver's is the browser's.
    modmap = {"command": "ctrl", "cmd": "ctrl", "control": "ctrl", "ctrl": "ctrl",
              "option": "alt", "alt": "alt", "shift": "shift"}
    mods = [modmap.get(str(m).lower(), str(m).lower()) for m in data.get("modifiers", [])]

    if action == "type":
        ev = {"t": "text", "s": text}
    elif action in ("keystroke", "hotkey"):
        named = {"return": "Enter", "enter": "Enter", "tab": "Tab", "escape": "Escape",
                 "esc": "Escape", "space": "Space", "delete": "Delete",
                 "backspace": "Backspace", "up": "ArrowUp", "down": "ArrowDown",
                 "left": "ArrowLeft", "right": "ArrowRight", "home": "Home",
                 "end": "End", "pageup": "PageUp", "pagedown": "PageDown"}
        k = key.lower()
        if k in named:
            ev = {"t": "key", "k": named[k], "down": True, "mods": mods}
        elif k.startswith("f") and k[1:].isdigit():
            ev = {"t": "key", "k": "F" + k[1:], "down": True, "mods": mods}
        elif len(key) == 1 and mods:
            ev = {"t": "combo", "k": key, "mods": mods}
        elif len(key) == 1:
            ev = {"t": "text", "s": key}
        else:
            return web.json_response({"error": f"Unknown key: {key}"}, status=400)
    else:
        return web.json_response({"error": "Unknown action"}, status=400)

    try:
        await asyncio.get_running_loop().run_in_executor(None, _input_handle, ev)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


def _input_handle(ev):
    """In-process input injection for the one-shot REST routes.

    The live screen view keeps a persistent driver process because a fresh
    interpreter per click cost ~100 ms. These routes fire rarely, so importing
    the same module and calling it directly is simpler and has no such cost.
    """
    import input_helper
    input_helper.ensure_dpi_aware()
    input_helper.handle(ev)


# ═══════════════════════════════════════
# CLIPBOARD
# ═══════════════════════════════════════
# pbcopy/pbpaste have no Windows twin. PowerShell's Get/Set-Clipboard is the
# closest thing that needs no extra dependency and no window handle.

async def _powershell(script: str, stdin_text: str = None):
    # Windows PowerShell writes a REDIRECTED pipe in the console's OEM code page,
    # not UTF-8, so every non-ASCII character came back as U+FFFD -- silently, the
    # caller still got a string. Pin both ends to UTF-8 before the caller's script
    # runs. Guarded: the encoding setters throw when no console is attached.
    script = ("try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new()}catch{};"
              "try{[Console]::InputEncoding=[Text.UTF8Encoding]::new()}catch{};"
              "$OutputEncoding=[Text.UTF8Encoding]::new();" + script)
    proc = await asyncio.create_subprocess_exec(
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    data = stdin_text.encode("utf-8") if stdin_text is not None else None
    out, err = await proc.communicate(data)
    return proc.returncode, (out or b"").decode("utf-8", "replace")


@require_auth
async def handle_clipboard_get(request: web.Request):
    _, out = await _powershell("Get-Clipboard -Raw")
    return web.json_response({"text": out.rstrip("\r\n")})


@require_auth
async def handle_clipboard_set(request: web.Request):
    data = await request.json()
    text = data.get("text", "")
    # Piped through stdin, never interpolated into the command line: the text is
    # arbitrary and would otherwise be parsed as PowerShell.
    code, _ = await _powershell(
        "$in = [Console]::In.ReadToEnd(); Set-Clipboard -Value $in", text)
    return web.json_response({"ok": code == 0})


# ═══════════════════════════════════════
# VOLUME CONTROL
# ═══════════════════════════════════════
# Windows exposes no scriptable master volume without a COM dependency, so this
# drives the same virtual media keys a keyboard sends. Level is stepped, not set.
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF


def _tap_vk(vk: int, times: int = 1):
    import input_helper
    for _ in range(max(1, times)):
        input_helper._send(input_helper._kb(vk=vk, flags=0),
                           input_helper._kb(vk=vk, flags=input_helper.KEYEVENTF_KEYUP))


@require_auth
async def handle_volume(request: web.Request):
    loop = asyncio.get_running_loop()
    if request.method == "GET":
        # Nothing to read back without COM; say unknown rather than invent it.
        return web.json_response({"volume": None, "muted": None,
                                  "note": "stepped control only on Windows"})
    data = await request.json()
    if "muted" in data:
        await loop.run_in_executor(None, _tap_vk, VK_VOLUME_MUTE, 1)
    if "volume" in data:
        delta = int(data.get("delta", 0)) or (10 if int(data["volume"]) >= 50 else -10)
        vk = VK_VOLUME_UP if delta > 0 else VK_VOLUME_DOWN
        await loop.run_in_executor(None, _tap_vk, vk, min(25, abs(delta) // 2 or 1))
    return web.json_response({"ok": True})


# ═══════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════

@require_auth
async def handle_notification(request: web.Request):
    data = await request.json()
    title = data.get("title", "FiaOS")
    message = data.get("message", "")
    # Balloon tip via WinForms. A real toast needs a registered AppUserModelID,
    # which is far more machinery than this route is worth.
    script = (
        "$t = [Console]::In.ReadToEnd() -split \"`n\", 2;"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.BalloonTipTitle = $t[0]; $n.BalloonTipText = $t[1];"
        "$n.Visible = $true; $n.ShowBalloonTip(5000); Start-Sleep -Seconds 6;"
        "$n.Dispose()")
    asyncio.create_task(_powershell(script, title + "\n" + message))
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
    """Installed applications — Start Menu shortcuts are Windows' /Applications."""
    apps, seen = [], set()
    roots = [
        os.path.join(os.environ.get("ProgramData", "C:\\ProgramData"),
                     "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("APPDATA", ""),
                     "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.expanduser("~/Desktop"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if not f.lower().endswith((".lnk", ".url")):
                    continue
                name = os.path.splitext(f)[0]
                if name.lower() in seen:
                    continue
                seen.add(name.lower())
                apps.append({"name": name, "path": os.path.join(dirpath, f)})
    apps.sort(key=lambda a: a["name"].lower())
    return web.json_response({"apps": apps})


@require_auth
async def handle_open_app(request: web.Request):
    """Open an application by name, or by the path /api/apps handed back."""
    data = await request.json()
    app_name = data.get("name", "")
    if not app_name:
        return web.json_response({"error": "No app"}, status=400)
    path = data.get("path") or app_name
    try:
        # 'start' resolves .lnk/.url and bare executable names alike
        proc = await asyncio.create_subprocess_exec(
            "cmd.exe", "/c", "start", "", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            return web.json_response(
                {"error": err.decode("utf-8", "replace").strip()}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"ok": True})


@require_auth
async def handle_quit_app(request: web.Request):
    """Close an application by image name."""
    data = await request.json()
    app_name = (data.get("name") or "").strip()
    if not app_name:
        return web.json_response({"error": "No app"}, status=400)
    stem = os.path.splitext(os.path.basename(app_name))[0].lower()
    closed = 0
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if os.path.splitext(p.info["name"] or "")[0].lower() == stem:
                p.terminate()
                closed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return web.json_response({"ok": True, "closed": closed})


# ═══════════════════════════════════════
# SLEEP / WAKE / LOCK
# ═══════════════════════════════════════

@require_auth
async def handle_system_action(request: web.Request):
    data = await request.json()
    action = data.get("action", "")
    loop = asyncio.get_running_loop()

    if action == "sleep":
        # Refused on purpose: this machine is a headless server reached only over
        # the network, and sleeping is exactly what made it unreachable before.
        return web.json_response(
            {"error": "Sleep is disabled on the PC - it is a headless server."},
            status=409)
    elif action == "lock":
        await loop.run_in_executor(None, ctypes.windll.user32.LockWorkStation)
    elif action in ("brightness_up", "brightness_down"):
        step = 10 if action == "brightness_up" else -10
        script = (
            "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
            "-ErrorAction SilentlyContinue; if ($m) { "
            "$v = [Math]::Max(0,[Math]::Min(100,$m.CurrentBrightness + (" + str(step) + "))); "
            "Invoke-CimMethod -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
            "-MethodName WmiSetBrightness -Arguments @{Brightness=$v;Timeout=1} }")
        await _powershell(script)
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
    sw, sh = screencast.screen_points()    # the space mouse events are in
    want_w = max(640, min(2560, int(request.query.get("w", 1720))))
    fps = max(2, min(20, int(request.query.get("fps", 10))))
    quality = max(20, min(90, int(request.query.get("q", 55))))
    geo = screencast.geometry(pw, ph, want_w)

    await ws.send_str(json.dumps({"type": "info", "screen": {"w": pw, "h": ph},
                                  "grid": geo, "fps": fps}))

    worker = await screencast.acquire_worker(VENV_PYTHON, str(FIAOS_DIR / "screen_worker.py"),
                                             want_w, fps, quality)
    inp = await screencast.start_input(VENV_PYTHON, str(FIAOS_DIR / "input_helper.py"))
    # Hold the display on for as long as someone is watching. caffeinate's
    # Windows equivalent is a thread execution state, set on the display thread
    # rather than a child process, so there is nothing to kill in the finally.
    _display_hold(True)
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
        # It still dies on its own after WARM_HOLD, and its parent going away
        # stops it regardless, so nothing keeps capturing indefinitely.
        await screencast.release_worker(worker, want_w, fps, quality)
        _display_hold(False)
        if inp.returncode is None:
            try:
                inp.kill()                    # no lingering input driver, ever
                await asyncio.wait_for(inp.wait(), timeout=3)
            except Exception:
                pass
    return ws


# ═══════════════════════════════════════
# MACHINE SWITCHING  (Mini / M5 / PC)
# ═══════════════════════════════════════
# The MINI/M5/PC buttons used to only set a cookie and reload — nothing on this
# side ever read it, so picking M5 left you on the mini. Now the cookie actually
# routes: every request and every websocket is proxied to that machine's own
# FiaOS, which validates the same signed token, so there is no second login.
#
# Order matters. The M5 is wired to the mini by a Thunderbolt cable, which shows
# up as its own subnet on both ends. Measured: 0.57 ms over
# the cable versus 63 ms average and wildly unstable (4-113 ms) over wifi. The
# cable is tried first and wifi is only the fallback, so switching to the M5 from
# a phone is as quick as the mini itself.
#
# Written from the point of view of whichever machine is running: the entry for
# MACHINE_KEY is emptied below, because "this machine" is never something to
# proxy to. Left as the mini's literal table, the PC would proxy its own tab to
# its own address and loop.
# Set FIAOS_PEERS to your own map, e.g.
#   FIAOS_PEERS="mini=10.0.0.5:9000;m5=10.0.0.9:9000,10.0.0.10:9000;pc=10.0.0.12:9000"
# Comma-separated addresses for one machine are tried in order, so put a direct
# cable link ahead of wifi. The defaults below are placeholders, not real hosts.
_DEFAULT_TARGETS = {
    "mini": ["10.0.0.5:9000"],
    "m5":   ["10.0.0.9:9000", "10.0.0.10:9000"],   # fast link first, wifi second
    "pc":   ["10.0.0.12:9000"],
}


def _parse_peers(spec: str) -> dict:
    out = {}
    for part in (spec or "").split(";"):
        if "=" not in part:
            continue
        key, addrs = part.split("=", 1)
        hosts = [a.strip() for a in addrs.split(",") if a.strip()]
        if hosts:
            out[key.strip()] = hosts
    return out


_ALL_TARGETS = _parse_peers(os.environ.get("FIAOS_PEERS", "")) or _DEFAULT_TARGETS
TARGETS = {k: ([] if k == MACHINE_KEY else v) for k, v in _ALL_TARGETS.items()}
# Never proxied: without these you could not log in, switch back, or sign out.
LOCAL_ONLY = ("/login", "/api/login", "/logout", "/machines/", "/static/")
_reachable: dict = {}          # "host:port" -> (ok, checked_at)


async def _alive(hostport: str, ttl: float = 10.0) -> bool:
    ok, when = _reachable.get(hostport, (False, 0.0))
    if time.time() - when < ttl:
        return ok
    host, _, port = hostport.partition(":")
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=1.5)
        w.close()
        ok = True
    except Exception:
        ok = False
    _reachable[hostport] = (ok, time.time())
    return ok


async def pick_target(key: str):
    """First reachable address for a machine — cable before wifi."""
    for hostport in TARGETS.get(key, []):
        if await _alive(hostport):
            return hostport
    return None


async def handle_machine_probe(request: web.Request):
    key = request.match_info.get("key", "")
    if key == MACHINE_KEY:
        return web.json_response({"up": True, "via": "local"})
    hostport = await pick_target(key)
    if not hostport:
        return web.json_response({"up": False}, status=503)
    return web.json_response({"up": True, "via": hostport})


@web.middleware
async def machine_proxy(request: web.Request, handler):
    """Send everything to the chosen machine, websockets included."""
    key = request.cookies.get("fia_target", MACHINE_KEY)
    if (key == MACHINE_KEY or key not in TARGETS
            or request.path.startswith(LOCAL_ONLY)):
        return await handler(request)
    hostport = await pick_target(key)
    if not hostport:
        return await handler(request)      # that machine is off — stay here

    upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
    cookies = {k: v for k, v in request.cookies.items() if k != "fia_target"}

    if upgrade:
        client_ws = web.WebSocketResponse(max_msg_size=0, heartbeat=30)
        await client_ws.prepare(request)
        url = f"ws://{hostport}{request.rel_url}"
        try:
            async with aiohttp.ClientSession(cookies=cookies) as sess:
                async with sess.ws_connect(url, max_msg_size=0, heartbeat=30) as up_ws:
                    async def down():
                        async for m in up_ws:
                            if m.type == aiohttp.WSMsgType.BINARY:
                                await client_ws.send_bytes(m.data)
                            elif m.type == aiohttp.WSMsgType.TEXT:
                                await client_ws.send_str(m.data)
                            else:
                                break
                    pump = asyncio.create_task(down())
                    try:
                        async for m in client_ws:
                            if m.type == aiohttp.WSMsgType.BINARY:
                                await up_ws.send_bytes(m.data)
                            elif m.type == aiohttp.WSMsgType.TEXT:
                                await up_ws.send_str(m.data)
                            else:
                                break
                    finally:
                        pump.cancel()
        except Exception as e:
            print(f"[FiaOS] ws proxy to {key} ({hostport}) failed: {e}")
        return client_ws

    body = await request.read()
    hop = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade",
           "proxy-authenticate", "proxy-authorization", "te", "trailers",
           "content-length", "cookie"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in hop}
    try:
        async with aiohttp.ClientSession(cookies=cookies) as sess:
            async with sess.request(request.method, f"http://{hostport}{request.rel_url}",
                                    headers=headers, data=body or None,
                                    allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=120)) as up:
                raw = await up.read()
                out = web.Response(status=up.status, body=raw)
                for k, v in up.headers.items():
                    if k.lower() not in hop and k.lower() != "content-encoding":
                        out.headers[k] = v
                return out
    except Exception as e:
        return web.Response(status=502, text=f"{key} unreachable via {hostport}: {e}")


# ═══════════════════════════════════════
# VNC BRIDGE  (the only way to reach the macOS login/lock screen)
# ═══════════════════════════════════════
# FiaOS's own screen view drives the Mac with synthetic CGEvents. macOS turns on
# Secure Input at the lock screen precisely to block those, and before login this
# agent is not running at all — so that view can never get past a password
# prompt. Apple's own Screen Sharing runs privileged and CAN, so this bridges
# noVNC in the browser to it: a WebSocket carrying raw RFB straight to
# 127.0.0.1:5900. No websockify process, no extra port exposed; the bridge lives
# behind the same FiaOS login as everything else, and 5900 stays on loopback.
VNC_HOST, VNC_PORT = "127.0.0.1", 5900


@require_auth
async def handle_vnc_ws(request: web.Request):
    ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0, heartbeat=30)
    await ws.prepare(request)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(VNC_HOST, VNC_PORT), timeout=8)
    except (OSError, asyncio.TimeoutError) as e:
        await ws.close(code=1011, message=str(e).encode()[:120])
        return ws

    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass

    pump = asyncio.create_task(tcp_to_ws())
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        pump.cancel()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return ws


@require_auth
async def handle_vnc_page(request: web.Request):
    """noVNC, pre-pointed at our bridge. Its own UI handles soft keyboards and
    touch properly, which is the whole reason it is here."""
    return web.HTTPFound("/static/novnc/vnc.html?path=api/vnc&autoconnect=1"
                         "&resize=scale&reconnect=1&show_dot=1")


# ═══════════════════════════════════════
# TERMINAL WEBSOCKET
# ═══════════════════════════════════════

# Commands that would kill FiaOS itself — blocked in terminal.
# Windows spellings of the same footguns: the launchctl/pkill forms are kept so
# a command pasted from a Mac is still caught rather than silently obeyed.
_PROTECTED_PATTERNS = [
    r"launchctl\s+(unload|remove|stop).*fiaos",
    r"pkill.*(server\.py|fiaos|personaplex)",
    r"kill.*(server\.py|fiaos)",
    r"killall.*[Pp]ython",
    r"Stop-Process.*(python|server\.py|fiaos)",
    r"taskkill.*(python|fiaos)",
    r"Stop-ScheduledTask.*Fia",
    r"Unregister-ScheduledTask.*Fia",
    r"schtasks.*/(end|delete).*Fia",
    r"Stop-Service.*[Ff]ia",
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

    # Spawn PowerShell inside a ConPTY. Same idea as the Mac's openpty + zsh:
    # a real console device, so anything that draws a TUI works.
    import winpty

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    home = os.path.expanduser("~")
    loop = asyncio.get_running_loop()

    try:
        pty_proc = await loop.run_in_executor(None, lambda: winpty.PtyProcess.spawn(
            ["powershell.exe", "-NoLogo", "-NoExit"],
            cwd=home, env=env, dimensions=(30, 100)))
    except Exception as e:
        await ws.send_str(f"[shell spawn failed: {e}]\n")
        return ws

    closed = False

    async def pty_to_ws():
        """Forward ConPTY output -> WebSocket as binary chunks.

        pywinpty's read is blocking with no selectable handle, so it runs in a
        worker thread. The browser side is unchanged: it still receives raw
        binary exactly as it did from the Mac's file descriptor.
        """
        while not closed:
            try:
                data = await loop.run_in_executor(None, _pty_read, pty_proc)
            except asyncio.CancelledError:
                break
            if data is None:
                break
            if not data:
                await asyncio.sleep(0.01)
                continue
            try:
                await ws.send_bytes(data)
            except (ConnectionResetError, RuntimeError):
                break

    pty_task = asyncio.create_task(pty_to_ws())

    def _write(text: str):
        try:
            pty_proc.write(text)
            return True
        except Exception:
            return False

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
                            _write(d.get("data", ""))
                            handled = True
                        elif kind == "resize":
                            rows = int(d.get("rows", 24)); cols = int(d.get("cols", 80))
                            try:
                                pty_proc.setwinsize(rows, cols)
                            except Exception:
                                pass
                            handled = True
                    except (json.JSONDecodeError, ValueError, KeyError, OSError):
                        handled = False
                if not handled:
                    # Legacy line-mode: append newline so the shell runs the command
                    if _is_self_destructive(payload):
                        await ws.send_str("\n[BLOCKED] Can't kill FiaOS services from remote terminal.\n")
                    else:
                        _write(payload + "\r")
            elif msg.type == aiohttp.WSMsgType.BINARY:
                if not _write(msg.data.decode("utf-8", "replace")):
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    finally:
        closed = True
        pty_task.cancel()
        try:
            if pty_proc.isalive():
                pty_proc.terminate(force=True)
        except Exception:
            pass
    return ws


def _pty_read(pty_proc):
    """One blocking read. None means the shell is gone."""
    try:
        if not pty_proc.isalive():
            return None
        out = pty_proc.read(65536)
        if out == "":
            return b""
        return out.encode("utf-8", "replace") if isinstance(out, str) else out
    except EOFError:
        return None
    except Exception:
        return None


# ═══════════════════════════════════════
# APP SETUP
# ═══════════════════════════════════════

def _sweep_strays():
    """Kill screen workers or input drivers orphaned by a previous run.

    They are children of the server; if the server is killed outright they lose
    their parent and would otherwise keep capturing the screen indefinitely.
    """
    me = os.getpid()
    for name in ("screen_worker.py", "input_helper.py"):
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if p.info["pid"] == me:
                    continue
                if any(name in str(a) for a in (p.info["cmdline"] or ())):
                    p.kill()
                    print(f"[FiaOS] cleared stray {name} ({p.info['pid']})")
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                pass


def create_app() -> web.Application:
    _sweep_strays()
    app = web.Application(client_max_size=100 * 1024 * 1024,  # 100MB upload limit
                          middlewares=[machine_proxy])

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
    app.router.add_get("/api/vnc", handle_vnc_ws)
    app.router.add_get("/vnc", handle_vnc_page)
    app.router.add_get("/vnc/", handle_vnc_page)
    app.router.add_get("/machines/probe/{key}", handle_machine_probe)

    # Static
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")

    return app


def _preflight():
    """Fail loudly at startup rather than quietly serving a broken picture."""
    screencast.ensure_dpi_aware()

    # 1. The geometry check the M5 taught us. The server sizes the tile grid
    # from screen_size(); the worker tiles the array grab() returns. If those
    # two disagree the tiles land in the wrong slots and the screen looks
    # doubled. Verified against a real capture, not against another metric.
    from screen_worker import Capture
    sw, sh = screencast.screen_size()
    cap = Capture()
    arr = cap.grab()
    if arr is None:
        raise SystemExit("[FiaOS] preflight: screen capture returned nothing")
    ah, aw, _ = arr.shape
    if (aw, ah) != (sw, sh):
        raise SystemExit(
            f"[FiaOS] preflight FAILED: screen_size() says {sw}x{sh} but the "
            f"capture is {aw}x{ah}. The tile grid would not match the tiles. "
            "This is the DPI-awareness trap — fix it before serving.")
    cap._release()
    px, py = screencast.screen_points()
    print(f"[FiaOS] display: {sw}x{sh} pixels, input space {px}x{py} — grid matches capture")

    # 2. The self-proxy loop. FIAOS_MACHINE defaults to "mini"; left at the
    # default here, a request carrying fia_target=pc is not recognised as local,
    # the middleware looks up "pc", finds this machine's own address and proxies
    # to itself forever.
    own = {a for addrs in __import__("psutil").net_if_addrs().values()
           for a in (x.address for x in addrs)}
    for hostport in TARGETS.get(MACHINE_KEY, []):
        if hostport.split(":")[0] in own:
            raise SystemExit(
                f"[FiaOS] preflight FAILED: FIAOS_MACHINE={MACHINE_KEY} but "
                f"TARGETS[{MACHINE_KEY}] contains this machine's own address "
                f"({hostport}). Requests would proxy to themselves in a loop.")
    print(f"[FiaOS] machine: {MACHINE_KEY}")


if __name__ == "__main__":
    print(f"[FiaOS] Starting on port {PORT}")
    print(f"[FiaOS] Dashboard: http://localhost:{PORT}")
    _preflight()
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
