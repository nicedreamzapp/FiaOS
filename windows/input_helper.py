#!/usr/bin/env python3
"""Persistent input driver for the FiaOS screen view — Windows.

Reads one JSON event per line on stdin and posts it as a real SendInput event.
It stays running for the whole session on purpose: launching a fresh Python
process per click cost about a tenth of a second each and made dragging
impossible.

Events (identical to the macOS build — the browser side is unchanged):
    {"t":"move","x":100,"y":200}
    {"t":"down","x":100,"y":200,"btn":"left"}      # btn: left | right
    {"t":"up","x":100,"y":200,"btn":"left"}
    {"t":"drag","x":100,"y":200,"btn":"left"}      # movement with a button held
    {"t":"click","x":100,"y":200}
    {"t":"dblclick","x":100,"y":200}
    {"t":"scroll","dy":-3,"dx":0}
    {"t":"text","s":"hello"}                       # type literal characters
    {"t":"key","k":"Enter","down":true,"mods":["ctrl"]}
    {"t":"combo","k":"c","mods":["ctrl"]}

Coordinates arrive as physical screen pixels, the same space screen_size()
reports and the capture worker tiles.
"""
import ctypes
import json
import sys
from ctypes import wintypes

from screencast import ensure_dpi_aware

user32 = ctypes.windll.user32

# ── SendInput plumbing ─────────────────────────────────────────────────────
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short


def _send(*items):
    n = len(items)
    arr = (INPUT * n)(*items)
    user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _mouse(flags, dx=0, dy=0, data=0):
    return INPUT(type=INPUT_MOUSE,
                 u=_UNION(mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)))


def _kb(vk=0, scan=0, flags=0):
    return INPUT(type=INPUT_KEYBOARD, u=_UNION(ki=KEYBDINPUT(vk, scan, flags, 0, 0)))


# ── coordinates ────────────────────────────────────────────────────────────
# SendInput's absolute space is 0..65535 across the primary screen, not pixels.
# Screen size is read fresh rather than cached so a resolution change does not
# silently send every click to the wrong place until the process restarts.
def _norm(x, y):
    w = max(1, user32.GetSystemMetrics(0))
    h = max(1, user32.GetSystemMetrics(1))
    nx = int(round(max(0, min(w - 1, x)) * 65535 / max(1, w - 1)))
    ny = int(round(max(0, min(h - 1, y)) * 65535 / max(1, h - 1)))
    return nx, ny


BTN = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# Only the keys a browser can't express as plain text.
VK = {
    "Enter": 0x0D, "Return": 0x0D, "Tab": 0x09, "Space": 0x20, "Backspace": 0x08,
    "Delete": 0x2E, "Escape": 0x1B, "Esc": 0x1B, "Insert": 0x2D,
    "ArrowLeft": 0x25, "ArrowUp": 0x26, "ArrowRight": 0x27, "ArrowDown": 0x28,
    "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

# Keys that live on the grey island / numpad-duplicated block need the extended
# flag or Windows delivers the numpad twin instead — arrows especially.
EXTENDED = {0x25, 0x26, 0x27, 0x28, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E}

MODS = {
    "ctrl": 0x11,      # VK_CONTROL
    "shift": 0x10,     # VK_SHIFT
    "alt": 0x12,       # VK_MENU
    # The browser sends 'cmd' from a Mac keyboard's Command key and from the
    # sticky button on a Mac. Ctrl is what that key means on Windows, so map it
    # there: cmd+c has to copy, not open the Start menu.
    "cmd": 0x11,
    "meta": 0x11,
    "win": 0x5B,       # VK_LWIN, only if something explicitly asks for it
}


def _mod_vks(mods):
    seen, out = set(), []
    for m in mods or ():
        vk = MODS.get(m)
        if vk and vk not in seen:
            seen.add(vk)
            out.append(vk)
    return out


def _key_events(vk, down):
    flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0
    if not down:
        flags |= KEYEVENTF_KEYUP
    return _kb(vk=vk, flags=flags)


def _tap_with_mods(vk, mods, extra_shift=False):
    vks = _mod_vks(mods)
    if extra_shift and 0x10 not in vks:
        vks = vks + [0x10]
    seq = [_key_events(m, True) for m in vks]
    seq += [_key_events(vk, True), _key_events(vk, False)]
    seq += [_key_events(m, False) for m in reversed(vks)]
    _send(*seq)


def _type_text(s):
    """Unicode injection — types any character without a keycode table."""
    seq = []
    for ch in s:
        # Anything outside the BMP arrives as a surrogate pair — two units, and
        # both have to be injected or the character is dropped.
        raw = ch.encode("utf-16-le")
        for i in range(0, len(raw), 2):
            u = int.from_bytes(raw[i:i + 2], "little")
            seq.append(_kb(scan=u, flags=KEYEVENTF_UNICODE))
            seq.append(_kb(scan=u, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if len(seq) >= 64:               # keep each SendInput batch small
            _send(*seq)
            seq = []
    if seq:
        _send(*seq)


def handle(ev):
    t = ev.get("t")
    x, y = ev.get("x", 0), ev.get("y", 0)
    mods = ev.get("mods") or []

    if t in ("move", "down", "up", "drag", "click", "dblclick"):
        nx, ny = _norm(x, y)
        move = _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny)
        btn = ev.get("btn", "left")
        dn, up = BTN.get(btn, BTN["left"])
        held = _mod_vks(mods)
        pre = [_key_events(m, True) for m in held]
        post = [_key_events(m, False) for m in reversed(held)]

        if t in ("move", "drag"):
            _send(move)                      # a held button keeps dragging
        elif t == "down":
            _send(*pre, move, _mouse(dn))
            # Modifiers stay down until the matching 'up', so shift-click and
            # ctrl-drag behave; the up event releases them.
        elif t == "up":
            _send(move, _mouse(up), *post)
        elif t == "click":
            _send(*pre, move, _mouse(dn), _mouse(up), *post)
        elif t == "dblclick":
            _send(*pre, move, _mouse(dn), _mouse(up), _mouse(dn), _mouse(up), *post)

    elif t == "scroll":
        # The browser sends pixel deltas; Windows counts notches of 120. Apps
        # accept fractions of a notch, so scale rather than quantise or a slow
        # trackpad drag would move nothing at all.
        dy = int(round(float(ev.get("dy", 0)) * 1.2))
        dx = int(round(float(ev.get("dx", 0)) * 1.2))
        seq = []
        if dy:
            seq.append(_mouse(MOUSEEVENTF_WHEEL, data=ctypes.c_long(dy).value))
        if dx:
            seq.append(_mouse(MOUSEEVENTF_HWHEEL, data=ctypes.c_long(-dx).value))
        if seq:
            _send(*seq)

    elif t == "text":
        _type_text(ev.get("s", ""))

    elif t == "key":
        vk = VK.get(ev.get("k"))
        if vk is None:
            return
        if ev.get("down", True):
            _tap_with_mods(vk, mods)         # one tap, not a half press

    elif t == "combo":
        # A shortcut like ctrl+c. VkKeyScanW resolves the character on the
        # user's actual layout instead of a hardcoded US table.
        ch = (ev.get("k") or "")[:1]
        if not ch:
            return
        res = user32.VkKeyScanW(ch)
        if res == -1:
            return
        vk = res & 0xFF
        needs_shift = bool((res >> 8) & 1)
        _tap_with_mods(vk, mods or ["ctrl"], extra_shift=needs_shift)


def main():
    ensure_dpi_aware()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:  # never die on one bad event
            print(f"input error: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
