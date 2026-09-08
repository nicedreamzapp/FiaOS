"""Live screen plumbing for FiaOS — ours, no third-party viewer.

The heavy work lives in screen_worker.py, which captures the display and writes
only the tiles that changed. This module launches it, parses its records, and
turns browser input into events for input_helper.py.
"""
import asyncio
import ctypes
import json

TILE = 128

_dpi_ready = False


def ensure_dpi_aware():
    """Make this process see real pixels. Must run before any screen metric.

    Windows lies to a DPI-unaware process: at 150% scaling GetSystemMetrics
    reports 1280x720 for a 1920x1080 panel, and BitBlt hands back a stretched
    1280x720 image. That is the same disagreement that shredded the M5 picture,
    just with a different cause, so it is shut off at the source in every
    process that touches the screen — server, capture worker and input driver.
    """
    global _dpi_ready
    if _dpi_ready:
        return
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE. Fails harmlessly if already set.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_ready = True


def screen_size():
    """Pixel size of the primary display — the geometry the capture worker sees.

    The worker tiles the real pixel buffer, so the grid has to be sized in the
    same pixels. On the Macs this had to be dug out of the display mode because
    CGDisplayBounds reports points; on Windows, once the process is DPI aware,
    SM_CXSCREEN already is physical pixels.
    """
    ensure_dpi_aware()
    u = ctypes.windll.user32
    return int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))


def screen_points():
    """The coordinate space mouse events are expressed in.

    On macOS this is genuinely a different number from screen_size() — points
    versus backing-store pixels. On Windows there is no second space to get
    wrong: a DPI-aware process addresses SendInput in physical pixels, the same
    ones BitBlt captures. So this returns screen_size() deliberately, not
    accidentally, and server.py asserts the two agree at startup.
    """
    return screen_size()


def geometry(full_w, full_h, want_w):
    """Streamed size and tile grid.

    Must match screen_worker.py exactly: it subsamples by an integer step, which
    is free, instead of resizing, which cost 53 ms a frame.
    """
    step = max(1, round(full_w / want_w))
    w = (full_w + step - 1) // step
    h = (full_h + step - 1) // step
    cols = (w + TILE - 1) // TILE
    rows = (h + TILE - 1) // TILE
    return {"step": step, "w": w, "h": h, "cols": cols, "rows": rows, "tile": TILE}


async def start_worker(python_path, worker_path, want_w, fps, quality):
    return await asyncio.create_subprocess_exec(
        python_path, "-u", worker_path, str(want_w), str(fps), str(quality),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def start_input(python_path, helper_path):
    """The persistent input driver — one process for the whole session."""
    return await asyncio.create_subprocess_exec(
        python_path, "-u", helper_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def tiles(proc):
    """Yield (tile index, jpeg) records as the worker produces them."""
    while True:
        head = await proc.stdout.readexactly(6)
        index = int.from_bytes(head[:2], "big")
        length = int.from_bytes(head[2:], "big")
        jpeg = await proc.stdout.readexactly(length)
        yield index, jpeg


def pack(index, jpeg):
    """Two-byte tile number, then the JPEG — what the browser unpacks."""
    return index.to_bytes(2, "big") + jpeg


def to_screen_coords(ev, sw, sh):
    """Browser sends fractions of the picture; the Mac needs real pixels."""
    if "fx" in ev:
        ev["x"] = round(ev.pop("fx") * sw)
    if "fy" in ev:
        ev["y"] = round(ev.pop("fy") * sh)
    return ev


def encode_event(ev, sw, sh):
    """Validate an incoming input event and turn it into a line for the driver."""
    allowed = {"move", "down", "up", "drag", "click", "dblclick",
               "scroll", "text", "key", "combo"}
    if ev.get("t") not in allowed:
        return None
    return json.dumps(to_screen_coords(ev, sw, sh)) + "\n"


# ── warm worker ────────────────────────────────────────────────────────────
# A phone backgrounds the tab constantly (the UI disconnects on
# visibilitychange), so reconnects are the common case, not the rare one. A
# cold worker costs ~166 ms before the first tile — process spawn plus the
# numpy/PIL/Quartz imports — and that is paid on every single return to the
# tab. Keep the last worker alive briefly and hand it back if the viewer asks
# for the same geometry, so coming back to the tab is network-bound only.
_WARM = {"proc": None, "key": None, "expires": 0.0}
WARM_HOLD = 90.0          # seconds an idle worker is kept


async def _drop_warm():
    p = _WARM.get("proc")
    _WARM.update({"proc": None, "key": None, "expires": 0.0})
    if p is not None and p.returncode is None:
        try:
            p.kill()
            await asyncio.wait_for(p.wait(), timeout=2)
        except Exception:
            pass


async def acquire_worker(python_path, worker_path, want_w, fps, quality):
    """A capture worker for these settings — reused when one is still warm."""
    import time
    key = (want_w, fps, quality)
    p = _WARM.get("proc")
    if p is not None and _WARM["key"] == key and p.returncode is None \
            and time.time() < _WARM["expires"]:
        _WARM.update({"proc": None, "key": None, "expires": 0.0})
        if p.stdin:                      # make it resend every tile for the new viewer
            p.stdin.write(b"R\n")
            await p.stdin.drain()
        return p
    await _drop_warm()
    return await start_worker(python_path, worker_path, want_w, fps, quality)


async def release_worker(proc, want_w, fps, quality):
    """Park a worker instead of killing it, so the next connect is instant."""
    import time
    if proc is None or proc.returncode is not None:
        return
    await _drop_warm()
    _WARM.update({"proc": proc, "key": (want_w, fps, quality),
                  "expires": time.time() + WARM_HOLD})
    asyncio.get_running_loop().call_later(WARM_HOLD + 1, lambda: asyncio.ensure_future(_expire()))


async def _expire():
    import time
    if _WARM.get("proc") is not None and time.time() >= _WARM["expires"]:
        await _drop_warm()
