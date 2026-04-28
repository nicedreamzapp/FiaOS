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

# --- Config ---
PORT = 9000
VOICE_SAMPLE_RATE = 24000
PERSONAPLEX_WS = "ws://localhost:8998/api/chat"
FIAOS_DIR = Path(__file__).parent
STATIC_DIR = FIAOS_DIR / "static"
def _abort_no_password():
    """Refuse to run without an explicit password — never ship a hardcoded default."""
    raise SystemExit(
        "FIAOS_PASSWORD env var is not set. Set one in your LaunchAgent plist "
        "or shell env, then restart. Refusing to start with no password."
    )


PASSWORD = os.environ.get("FIAOS_PASSWORD") or _abort_no_password()
SESSION_EXPIRY = 86400  # 24 hours
MAX_LOGIN_ATTEMPTS = 10
LOGIN_WINDOW = 300  # 5 minutes
SCREENSHOT_DIR = tempfile.mkdtemp(prefix="fiaos_screenshots_")
SESSION_FILE = FIAOS_DIR / ".sessions.json"
PERSONAPLEX_IDLE_TIMEOUT = 60  # kill after 1 min idle


# --- On-demand PersonaPlex manager (saves RAM when idle) ---
class PersonaPlexManager:
    """Starts PersonaPlex only when voice is needed, kills it after idle timeout."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._last_active: float = 0  # timestamp of last voice activity
        self._lock = asyncio.Lock()
        self._watchdog_running = False

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def ensure_running(self) -> bool:
        """Start PersonaPlex if not running. Returns True when ready."""
        async with self._lock:
            self._last_active = time.time()

            if self.running:
                return True

            print("[FiaOS] PersonaPlex: starting on-demand (loading model)...")
            self._proc = subprocess.Popen(
                [
                    str(FIAOS_DIR / ".venv" / "bin" / "python3"), "-u",
                    "-m", "personaplex_mlx.local_web",
                    "-q", "4", "--no-browser",
                    "--voice", "NATF0",
                    "--text-prompt", os.environ.get("FIA_PROMPT", "You are a helpful local voice assistant. Keep replies short."),
                    "--text-temp", "0.1",
                    "--audio-temp", "0.5",
                ],
                stdout=open("/tmp/personaplex.log", "a"),
                stderr=subprocess.STDOUT,
                cwd=str(FIAOS_DIR),
            )

            # Wait for it to become ready (up to 30s for model load)
            for i in range(30):
                await asyncio.sleep(1)
                if self._proc.poll() is not None:
                    print("[FiaOS] PersonaPlex: process died during startup")
                    self._proc = None
                    return False
                try:
                    test_session = aiohttp.ClientSession()
                    test_ws = await asyncio.wait_for(
                        test_session.ws_connect(PERSONAPLEX_WS), timeout=2
                    )
                    msg = await asyncio.wait_for(test_ws.receive(), timeout=5)
                    await test_ws.close()
                    await test_session.close()
                    if msg.type == aiohttp.WSMsgType.BINARY and msg.data == b"\x00":
                        print(f"[FiaOS] PersonaPlex: ready ({i+1}s)")
                        # Start the idle watchdog
                        if not self._watchdog_running:
                            asyncio.ensure_future(self._idle_watchdog())
                        return True
                except Exception:
                    try:
                        await test_session.close()
                    except Exception:
                        pass

            print("[FiaOS] PersonaPlex: startup timed out")
            self._kill()
            return False

    def session_ended(self):
        """Called when a voice session ends. Updates last active time."""
        self._last_active = time.time()
        print(f"[FiaOS] PersonaPlex: session ended, idle timer starts ({PERSONAPLEX_IDLE_TIMEOUT}s)")

    async def _idle_watchdog(self):
        """Background loop: checks every 30s if PersonaPlex has been idle too long."""
        self._watchdog_running = True
        try:
            while True:
                await asyncio.sleep(30)
                if not self.running:
                    break
                idle_time = time.time() - self._last_active
                if idle_time >= PERSONAPLEX_IDLE_TIMEOUT:
                    print(f"[FiaOS] PersonaPlex: idle for {idle_time:.0f}s, shutting down to free RAM")
                    self._kill()
                    break
        finally:
            self._watchdog_running = False

    def _kill(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


pp_manager = PersonaPlexManager()

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


def _save_sessions():
    try:
        SESSION_FILE.write_text(json.dumps(sessions))
    except Exception:
        pass


sessions: dict[str, float] = _load_sessions()


def check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = login_attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW]
    login_attempts[ip] = attempts
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def record_attempt(ip: str):
    login_attempts.setdefault(ip, []).append(time.time())


def create_session() -> str:
    token = secrets.token_hex(32)
    sessions[token] = time.time() + SESSION_EXPIRY
    _save_sessions()
    return token


def valid_session(token: str) -> bool:
    if not token:
        return False
    expiry = sessions.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        sessions.pop(token, None)
        return False
    return True


def get_token(request: web.Request) -> str:
    token = request.cookies.get("fiaos_session", "")
    if not token:
        token = request.query.get("token", "")
    return token


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
    return web.FileResponse(STATIC_DIR / "login.html")


async def handle_login(request: web.Request):
    ip = request.remote or "unknown"
    if check_rate_limit(ip):
        return web.json_response({"error": "Too many attempts. Try again later."}, status=429)
    data = await request.json()
    password = data.get("password", "")
    if not hmac.compare_digest(password, PASSWORD):
        record_attempt(ip)
        return web.json_response({"error": "Wrong password"}, status=401)
    token = create_session()
    resp = web.json_response({"ok": True, "token": token})
    # Set cookie — works for both HTTP (local) and HTTPS (remote)
    is_https = request.headers.get("X-Forwarded-Proto") == "https" or request.secure
    resp.set_cookie("fiaos_session", token, max_age=SESSION_EXPIRY, httponly=False,
                     samesite="None" if is_https else "Lax",
                     secure=is_https)
    return resp


async def handle_logout(request: web.Request):
    token = get_token(request)
    sessions.pop(token, None)
    resp = web.HTTPFound("/login")
    resp.del_cookie("fiaos_session")
    return resp


async def handle_index(request: web.Request):
    token = get_token(request)
    if not valid_session(token):
        raise web.HTTPFound("/login")
    return web.FileResponse(STATIC_DIR / "index.html")


# ═══════════════════════════════════════
# COMMAND EXECUTOR
# ═══════════════════════════════════════

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

@require_auth
async def handle_status(request: web.Request):
    cpu_percent = psutil.cpu_percent(interval=0.5)
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
    # Compress with sips
    await (await asyncio.create_subprocess_exec(
        "sips", "-s", "formatOptions", quality, filepath,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )).communicate()
    return web.FileResponse(filepath, headers={"Content-Type": "image/jpeg", "Cache-Control": "no-cache"})


@require_auth
async def handle_screenshot_stream(request: web.Request):
    """Stream screenshots as multipart JPEG (MJPEG-like)."""
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-cache",
    })
    await resp.prepare(request)
    filepath = os.path.join(SCREENSHOT_DIR, "stream.jpg")
    try:
        while True:
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", "-t", "jpg", filepath,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if os.path.exists(filepath):
                # Compress
                await (await asyncio.create_subprocess_exec(
                    "sips", "-s", "formatOptions", "30", "-Z", "1920", filepath,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )).communicate()
                with open(filepath, "rb") as f:
                    data = f.read()
                await resp.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    + data + b"\r\n"
                )
            await asyncio.sleep(0.5)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    return resp


# ═══════════════════════════════════════
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
# WEBSOCKET PROXY (PersonaPlex Voice)
# ═══════════════════════════════════════

# Single voice session management — PersonaPlex can only handle one at a time
_voice_lock = asyncio.Lock()
_active_voice: dict = {"ws_in": None, "ws_out": None, "session": None}


async def _cleanup_voice():
    """Cleanly close any existing voice session."""
    v = _active_voice
    # Close PersonaPlex WS first (triggers model lock release)
    if v["ws_in"] and not v["ws_in"].closed:
        try:
            await asyncio.wait_for(v["ws_in"].close(), timeout=3)
        except Exception:
            pass
    # Close browser WS
    if v["ws_out"] and not v["ws_out"].closed:
        try:
            await v["ws_out"].close()
        except Exception:
            pass
    # Close HTTP session
    if v["session"] and not v["session"].closed:
        try:
            await v["session"].close()
        except Exception:
            pass
    v["ws_in"] = v["ws_out"] = v["session"] = None


@require_auth
async def handle_voice_ws(request: web.Request):
    """Voice WebSocket proxy: PCM from browser <-> Opus to PersonaPlex.

    Only one voice session at a time. New connections cleanly replace old ones.
    PersonaPlex starts on-demand and auto-shuts down when idle to free RAM.
    """
    voice = request.query.get("voice", "")
    print(f"[FiaOS] Voice: new connection from {request.remote} (voice={voice or 'default'})")
    ws_out = web.WebSocketResponse()
    await ws_out.prepare(request)

    # Start PersonaPlex on-demand if not running
    if not await pp_manager.ensure_running():
        print("[FiaOS] Voice: PersonaPlex failed to start")
        if not ws_out.closed:
            await ws_out.close()
        return ws_out

    async with _voice_lock:
        await _cleanup_voice()
        await asyncio.sleep(1.0)

    session = aiohttp.ClientSession()
    _active_voice["ws_out"] = ws_out
    _active_voice["session"] = session

    try:
        # Connect to PersonaPlex and wait for ready signal
        pp_url = PERSONAPLEX_WS + (f"?voice_prompt={voice}" if voice else "")
        ws_in = await asyncio.wait_for(session.ws_connect(pp_url), timeout=10)
        ready_msg = await asyncio.wait_for(ws_in.receive(), timeout=12)

        if not (ready_msg.type == aiohttp.WSMsgType.BINARY and ready_msg.data == b"\x00"):
            raise asyncio.TimeoutError("No ready signal")

    except (asyncio.TimeoutError, Exception):
        # PersonaPlex is stuck — kill it, restart through manager
        try:
            await ws_in.close()
        except Exception:
            pass
        await session.close()

        print("[FiaOS] Voice: PersonaPlex stuck, restarting via manager...")
        pp_manager._kill()
        await asyncio.sleep(2)
        pp_manager._last_active = time.time()

        if not await pp_manager.ensure_running():
            if not ws_out.closed:
                await ws_out.close()
            pp_manager.session_ended()
            return ws_out

        # Reconnect after restart
        session = aiohttp.ClientSession()
        _active_voice["session"] = session
        try:
            ws_in = await asyncio.wait_for(session.ws_connect(pp_url), timeout=10)
            ready_msg = await asyncio.wait_for(ws_in.receive(), timeout=12)
            if not (ready_msg.type == aiohttp.WSMsgType.BINARY and ready_msg.data == b"\x00"):
                raise Exception("Still no ready signal after restart")
        except Exception as e:
            print(f"[FiaOS] Voice: Failed after restart: {e}")
            await session.close()
            if not ws_out.closed:
                await ws_out.close()
            pp_manager.session_ended()
            return ws_out

    _active_voice["ws_in"] = ws_in
    # Send ready signal to browser
    await ws_out.send_bytes(b"\x00")
    print("[FiaOS] Voice: streaming...")

    # Voice conversation log
    _voice_log = {"frames_out": 0, "frames_in": 0, "fia_text": [], "start": time.time()}

    try:
        async def forward_to_client():
            """PersonaPlex -> Browser: pass through directly (no re-encoding)."""
            try:
                async for msg in ws_in:
                    if ws_out.closed:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        if not data:
                            continue
                        if data[0] == 0x01:
                            _voice_log["frames_out"] += 1
                            await ws_out.send_bytes(data)
                        elif data[0] == 0x02:
                            text = data[1:].decode("utf-8", errors="replace")
                            _voice_log["fia_text"].append(text)
                            await ws_out.send_bytes(data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        async def forward_to_server():
            """Browser -> PersonaPlex: pass through directly (no re-encoding)."""
            try:
                async for msg in ws_out:
                    if ws_in.closed:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        if not data:
                            continue
                        if data[0] == 0x01:
                            _voice_log["frames_in"] += 1
                            await ws_in.send_bytes(data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
            except (ConnectionResetError, asyncio.CancelledError):
                pass

        await asyncio.gather(forward_to_client(), forward_to_server())
    except Exception as e:
        print(f"[FiaOS] Voice proxy error: {e}")
    finally:
        elapsed = time.time() - _voice_log["start"]
        full_text = "".join(_voice_log["fia_text"])
        print(f"[FiaOS] Voice session ended ({elapsed:.0f}s, {_voice_log['frames_in']} in, {_voice_log['frames_out']} out)")
        if not ws_out.closed:
            await ws_out.close()
        async with _voice_lock:
            await _cleanup_voice()
        pp_manager.session_ended()
    return ws_out


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

def create_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)  # 100MB upload limit

    # Auth
    app.router.add_get("/login", handle_login_page)
    app.router.add_post("/api/login", handle_login)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/", handle_index)

    # Command
    app.router.add_post("/api/command", handle_command)

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
    app.router.add_get("/api/screenshot/stream", handle_screenshot_stream)

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
    app.router.add_get("/voice", handle_voice_ws)
    app.router.add_get("/api/terminal", handle_terminal_ws)

    # Static
    app.router.add_static("/static/", path=str(STATIC_DIR), name="static")

    return app


if __name__ == "__main__":
    print(f"[FiaOS] Starting on port {PORT}")
    print(f"[FiaOS] Dashboard: http://localhost:{PORT}")
    print(f"[FiaOS] Voice: on-demand (starts when needed, idles out after {PERSONAPLEX_IDLE_TIMEOUT}s)")
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
