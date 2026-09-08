#!/usr/bin/env python3
"""Screen capture worker for FiaOS — Windows.

Captures the display with BitBlt into a reused DIB section (about 4 ms, no
subprocess and no per-frame allocation), splits it into tiles, and writes only
the tiles that changed to stdout.

Record format on stdout, repeated:
    2 bytes  tile index, big endian
    4 bytes  jpeg length, big endian
    n bytes  jpeg

stdin accepts single characters:
    R   forget what the far end has, so the next pass resends every tile

Usage: screen_worker.py <streamed_width> <fps> <jpeg_quality>
"""
import ctypes
import hashlib
import io
import os
import sys
import threading
import time
from ctypes import wintypes

import numpy as np
from PIL import Image

from screencast import ensure_dpi_aware

TILE = 128

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0

# 64-bit safety: every one of these returns or takes a handle. Left as the
# default c_int, ctypes truncates the pointer and the capture crashes or hands
# back garbage on the first call.
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]


class Capture:
    """A screen DC and a DIB kept alive across frames.

    Rebuilding these every grab cost more than the blit itself, and at 10 fps
    that is 10 leaked GDI objects a second if any cleanup is ever missed.
    """

    def __init__(self):
        self.w = self.h = 0
        self.src = self.mem = self.bmp = self.old = None
        self.arr = None

    def _release(self):
        try:
            if self.mem and self.old:
                gdi32.SelectObject(self.mem, self.old)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            if self.mem:
                gdi32.DeleteDC(self.mem)
            if self.src:
                user32.ReleaseDC(None, self.src)
        except Exception:
            pass
        self.src = self.mem = self.bmp = self.old = None
        self.arr = None
        self.w = self.h = 0

    def _setup(self, w, h):
        self._release()
        self.src = user32.GetDC(None)
        self.mem = gdi32.CreateCompatibleDC(self.src)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # negative = top-down, no flip later
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        self.bmp = gdi32.CreateDIBSection(self.mem, ctypes.byref(bmi),
                                          DIB_RGB_COLORS, ctypes.byref(bits),
                                          None, 0)
        if not self.bmp or not bits.value:
            self._release()
            raise OSError("CreateDIBSection failed")
        self.old = gdi32.SelectObject(self.mem, self.bmp)
        buf = (ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)
        self.arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        self.w, self.h = w, h

    def grab(self):
        """The whole primary screen as an RGB array, at true pixel size."""
        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if w <= 0 or h <= 0:
            return None
        if (w, h) != (self.w, self.h):
            self._setup(w, h)
        if not gdi32.BitBlt(self.mem, 0, 0, w, h, self.src, 0, 0, SRCCOPY):
            # Happens across a lock/unlock or a resolution change: rebuild once.
            self._setup(w, h)
            if not gdi32.BitBlt(self.mem, 0, 0, w, h, self.src, 0, 0, SRCCOPY):
                return None
        return self.arr[:, :, 2::-1]         # BGRA -> RGB, a view, no copy


# ── parent liveness ────────────────────────────────────────────────────────
# The Unix build checked getppid() == 1. Windows has no reparent-to-init, so
# ask the OS directly whether the server that launched us is still alive; a
# still screen writes nothing, so a broken stdout alone would not be noticed.
_PPID = os.getppid()
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def parent_gone():
    h = kernel32.OpenProcess(SYNCHRONIZE, False, _PPID)
    if not h:
        return True
    try:
        return kernel32.WaitForSingleObject(h, 0) == WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(h)


# ── stdin ──────────────────────────────────────────────────────────────────
# select() on Windows only accepts sockets, never a pipe or a console handle,
# so the non-blocking poll the Mac build used cannot work here. A daemon thread
# blocking on readline costs nothing and says the same thing.
# Commands: "P" means no viewer is attached -- stop capturing entirely; anything
# else means resend every tile. Parked used to mean "keep grabbing the screen at
# full fps into a pipe nobody is reading," which burned CPU on a machine nobody
# was looking at.
_stdin = {"resend": False, "eof": False, "paused": False}
_wake = threading.Event()


def _stdin_reader():
    try:
        for line in sys.stdin:
            if line.startswith("P"):
                _stdin["paused"] = True
            else:
                _stdin["paused"] = False
                _stdin["resend"] = True
            _wake.set()
    except Exception:
        pass
    _stdin["eof"] = True
    _wake.set()


def main():
    ensure_dpi_aware()
    want_w = int(sys.argv[1]) if len(sys.argv) > 1 else 1720
    fps = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    quality = int(sys.argv[3]) if len(sys.argv) > 3 else 55

    threading.Thread(target=_stdin_reader, daemon=True).start()

    cap = Capture()
    first = cap.grab()
    if first is None:
        sys.exit("no screen")
    full_h, full_w, _ = first.shape
    # Integer subsampling only: it costs nothing, where a real resize cost 53 ms.
    step = max(1, round(full_w / want_w))
    out = sys.stdout.buffer
    hashes: dict[int, bytes] = {}
    interval = 1.0 / fps

    while True:
        started = time.monotonic()

        if _stdin["eof"] or parent_gone():
            return

        # Paused: capture nothing at all until the server sends a command.
        # 5 s so an orphaned worker still notices its server went away.
        while _stdin["paused"]:
            if _stdin["eof"] or parent_gone():
                return
            _wake.wait(5.0)
            _wake.clear()

        if _stdin["resend"]:
            _stdin["resend"] = False
            hashes.clear()

        frame = cap.grab()
        if frame is None:
            time.sleep(interval)
            continue
        small = np.ascontiguousarray(frame[::step, ::step])
        h, w, _ = small.shape
        cols = (w + TILE - 1) // TILE

        for y in range(0, h, TILE):
            for x in range(0, w, TILE):
                tile = np.ascontiguousarray(small[y:min(y + TILE, h), x:min(x + TILE, w)])
                digest = hashlib.blake2b(tile, digest_size=8).digest()
                index = (y // TILE) * cols + (x // TILE)
                if hashes.get(index) == digest:
                    continue
                hashes[index] = digest
                buf = io.BytesIO()
                Image.fromarray(tile).save(buf, "JPEG", quality=quality, optimize=False)
                jpeg = buf.getvalue()
                out.write(index.to_bytes(2, "big") + len(jpeg).to_bytes(4, "big") + jpeg)
        out.flush()

        # hold the requested pace; a still screen costs one capture and nothing else
        rest = interval - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt, OSError):
        os._exit(0)
