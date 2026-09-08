#!/usr/bin/env python3
"""Persistent input driver for the FiaOS screen view.

Reads one JSON event per line on stdin and posts it to the Mac as a real
CoreGraphics event. It stays running for the whole session on purpose: the old
screen tab launched a fresh Python process for every single click, which cost
about a tenth of a second each and made dragging impossible.

Events:
    {"t":"move","x":100,"y":200}
    {"t":"down","x":100,"y":200,"btn":"left"}      # btn: left | right
    {"t":"up","x":100,"y":200,"btn":"left"}
    {"t":"drag","x":100,"y":200,"btn":"left"}      # movement with a button held
    {"t":"dblclick","x":100,"y":200}
    {"t":"scroll","dy":-3,"dx":0}
    {"t":"text","s":"hello"}                       # type literal characters
    {"t":"key","k":"Enter","down":true,"mods":["cmd"]}
"""
import json
import sys

import Quartz

TAP = Quartz.kCGHIDEventTap

BTN = {
    "left": (Quartz.kCGMouseButtonLeft, Quartz.kCGEventLeftMouseDown,
             Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged),
    "right": (Quartz.kCGMouseButtonRight, Quartz.kCGEventRightMouseDown,
              Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged),
}

# Only the keys a browser can't express as plain text.
KEYCODE = {
    "Enter": 36, "Return": 36, "Tab": 48, "Space": 49, "Backspace": 51,
    "Delete": 117, "Escape": 53, "ArrowLeft": 123, "ArrowRight": 124,
    "ArrowDown": 125, "ArrowUp": 126, "Home": 115, "End": 119,
    "PageUp": 116, "PageDown": 121,
    "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
    "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
}

MODS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
}


def flags_for(mods):
    f = 0
    for m in mods or ():
        f |= MODS.get(m, 0)
    return f


def mouse(kind, x, y, btn="left", clicks=1, flags=0):
    button, down, up, dragged = BTN.get(btn, BTN["left"])
    kinds = {"down": down, "up": up, "drag": dragged,
             "move": Quartz.kCGEventMouseMoved}
    e = Quartz.CGEventCreateMouseEvent(None, kinds[kind], (x, y), button)
    if clicks > 1:
        Quartz.CGEventSetIntegerValueField(e, Quartz.kCGMouseEventClickState, clicks)
    if flags:
        Quartz.CGEventSetFlags(e, flags)
    Quartz.CGEventPost(TAP, e)


def handle(ev):
    t = ev.get("t")
    x, y = ev.get("x", 0), ev.get("y", 0)
    flags = flags_for(ev.get("mods"))

    if t in ("move", "down", "up", "drag"):
        mouse(t, x, y, ev.get("btn", "left"), 1, flags)

    elif t == "click":
        mouse("down", x, y, ev.get("btn", "left"), 1, flags)
        mouse("up", x, y, ev.get("btn", "left"), 1, flags)

    elif t == "dblclick":
        for n in (1, 2):
            mouse("down", x, y, "left", n, flags)
            mouse("up", x, y, "left", n, flags)

    elif t == "scroll":
        # Pixel units so trackpad-style deltas from the browser feel right.
        e = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitPixel, 2,
            int(ev.get("dy", 0)), int(ev.get("dx", 0)))
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(TAP, e)

    elif t == "text":
        # Unicode string events type any character without keycode tables.
        for ch in ev.get("s", ""):
            for is_down in (True, False):
                e = Quartz.CGEventCreateKeyboardEvent(None, 0, is_down)
                Quartz.CGEventKeyboardSetUnicodeString(e, len(ch), ch)
                Quartz.CGEventPost(TAP, e)

    elif t == "key":
        code = KEYCODE.get(ev.get("k"))
        if code is None:
            return
        e = Quartz.CGEventCreateKeyboardEvent(None, code, bool(ev.get("down", True)))
        if flags:
            Quartz.CGEventSetFlags(e, flags)
        Quartz.CGEventPost(TAP, e)

    elif t == "combo":
        # A shortcut like cmd+c: press and release a character key with modifiers.
        ch = (ev.get("k") or "").lower()
        code = {"a": 0, "c": 8, "v": 9, "x": 7, "z": 6, "s": 1, "w": 13, "t": 17,
                "r": 15, "l": 37, "f": 3, "q": 12, "n": 45, "o": 31,
                "k": 40, "u": 32, "d": 2, "e": 14, "p": 35}.get(ch)
        if code is None:
            return
        for is_down in (True, False):
            e = Quartz.CGEventCreateKeyboardEvent(None, code, is_down)
            Quartz.CGEventSetFlags(e, flags)
            Quartz.CGEventPost(TAP, e)


def main():
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
