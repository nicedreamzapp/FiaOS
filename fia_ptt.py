"""Fia PTT — Native macOS window with push-to-talk and voice picker.
Connects directly to PersonaPlex on localhost — no VPS, no server.py, zero latency.
Auto-starts PersonaPlex if not running."""

import asyncio
import os
import subprocess
import threading
import warnings
import numpy as np
import sounddevice as sd
import aiohttp
import objc
import time
from pathlib import Path

warnings.filterwarnings("ignore", category=objc.ObjCPointerWarning)

from Foundation import NSObject
from AppKit import (
    NSApplication, NSWindow, NSButton, NSTextField, NSScrollView, NSTextView,
    NSBackingStoreBuffered, NSMakeRect, NSFont, NSColor, NSView, NSEvent,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSBezelStyleRounded, NSTextAlignmentCenter, NSTextAlignmentLeft,
    NSApplicationActivationPolicyRegular, NSScreen,
    NSAttributedString, NSPopUpButton, NSBezierPath, NSImage,
    NSKeyDownMask, NSKeyUpMask,
    NSLeftMouseDownMask, NSLeftMouseUpMask,
)
from PyObjCTools import AppHelper

SAMPLE_RATE = 24000
WS_URL = "ws://localhost:8998/api/chat"
FIAOS_DIR = Path(__file__).parent
FIA_PROMPT = os.environ.get("FIA_PROMPT", "You are a helpful local voice assistant. Keep replies short.")


def ensure_personaplex():
    """Start PersonaPlex if not already running."""
    import socket
    try:
        s = socket.create_connection(("localhost", 8998), timeout=2)
        s.close()
        return True  # already running
    except (ConnectionRefusedError, OSError):
        pass
    print("[Fia PTT] Starting PersonaPlex...")
    subprocess.Popen(
        [
            str(FIAOS_DIR / ".venv" / "bin" / "python3"), "-u",
            "-m", "personaplex_mlx.local_web",
            "-q", "4", "--no-browser",
            "--voice", "NATF0",
            "--text-prompt", FIA_PROMPT,
            "--text-temp", "0.1",
            "--audio-temp", "0.5",
        ],
        stdout=open("/tmp/personaplex.log", "a"),
        stderr=subprocess.STDOUT,
        cwd=str(FIAOS_DIR),
    )
    # Wait for it to come up
    for i in range(30):
        time.sleep(1)
        try:
            s = socket.create_connection(("localhost", 8998), timeout=2)
            s.close()
            print(f"[Fia PTT] PersonaPlex ready ({i+1}s)")
            return True
        except (ConnectionRefusedError, OSError):
            pass
    print("[Fia PTT] PersonaPlex failed to start")
    return False

VOICES = [
    ("NATF0", "Female 1 (Native)"),
    ("NATF1", "Female 2 (Native)"),
    ("NATF2", "Female 3 (Native)"),
    ("NATF3", "Female 4 (Native)"),
    ("VARF0", "Female 1 (Varied)"),
    ("VARF1", "Female 2 (Varied)"),
    ("VARF2", "Female 3 (Varied)"),
    ("VARF3", "Female 4 (Varied)"),
    ("VARF4", "Female 5 (Varied)"),
    ("NATM0", "Male 1 (Native)"),
    ("NATM1", "Male 2 (Native)"),
    ("NATM2", "Male 3 (Native)"),
    ("NATM3", "Male 4 (Native)"),
    ("VARM0", "Male 1 (Varied)"),
    ("VARM1", "Male 2 (Varied)"),
    ("VARM2", "Male 3 (Varied)"),
    ("VARM3", "Male 4 (Varied)"),
    ("VARM4", "Male 5 (Varied)"),
]


class FiaPTT:
    def __init__(self):
        self.talking = False
        self.connected = False
        self.ws = None
        self.session = None
        self.loop = None
        self.out_stream = None
        self.in_stream = None
        self._audio_queue = []
        self._space_held = False
        self._mouse_held = False
        self._wants_reconnect = False
        self.current_voice = "NATF0"

    def run(self):
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

        w, h = 360, 520
        screen = NSScreen.mainScreen().frame()
        x = int((screen.size.width - w) / 2)
        y = int((screen.size.height - h) / 2)

        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False,
        )
        self.win.setTitle_("Fia")
        self.win.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.07, 0.07, 0.1, 1.0))
        cv = self.win.contentView()

        # Title
        t = NSTextField.alloc().initWithFrame_(NSMakeRect(0, h - 50, w, 36))
        t.setStringValue_("Fia")
        t.setFont_(NSFont.boldSystemFontOfSize_(28))
        t.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.4, 0.4, 0.95, 1.0))
        t.setBezeled_(False); t.setEditable_(False); t.setDrawsBackground_(False)
        t.setAlignment_(NSTextAlignmentCenter)
        cv.addSubview_(t)

        # Status
        self.status = NSTextField.alloc().initWithFrame_(NSMakeRect(20, h - 78, w - 40, 20))
        self.status.setStringValue_("Starting up...")
        self.status.setFont_(NSFont.systemFontOfSize_(12))
        self.status.setTextColor_(NSColor.grayColor())
        self.status.setBezeled_(False); self.status.setEditable_(False); self.status.setDrawsBackground_(False)
        self.status.setAlignment_(NSTextAlignmentCenter)
        cv.addSubview_(self.status)

        # Voice picker label
        vl = NSTextField.alloc().initWithFrame_(NSMakeRect(20, h - 110, 50, 20))
        vl.setStringValue_("Voice:")
        vl.setFont_(NSFont.systemFontOfSize_(12))
        vl.setTextColor_(NSColor.grayColor())
        vl.setBezeled_(False); vl.setEditable_(False); vl.setDrawsBackground_(False)
        cv.addSubview_(vl)

        # Voice dropdown
        self.voice_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(70, h - 113, w - 90, 24), False
        )
        self.voice_popup.setFont_(NSFont.systemFontOfSize_(12))
        selected_idx = 0
        for i, (vid, label) in enumerate(VOICES):
            self.voice_popup.addItemWithTitle_(f"{label}")
            if vid == self.current_voice:
                selected_idx = i
        self.voice_popup.selectItemAtIndex_(selected_idx)
        self.voice_popup.setTarget_(self)
        self.voice_popup.setAction_("voiceChanged:")
        cv.addSubview_(self.voice_popup)

        # ===== 3D WALKIE-TALKIE BUTTON =====
        # Outer bezel (dark shadow base)
        btn_x, btn_y, btn_w, btn_h = 25, h - 240, w - 50, 110
        self._btn_frame = (btn_x, btn_y, btn_w, btn_h)

        bezel = NSView.alloc().initWithFrame_(NSMakeRect(btn_x - 4, btn_y - 6, btn_w + 8, btn_h + 10))
        bezel.setWantsLayer_(True)
        bezel.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.03, 0.03, 0.05, 1.0).CGColor())
        bezel.layer().setCornerRadius_(22)
        cv.addSubview_(bezel)

        # Mid shadow (gives depth)
        shadow = NSView.alloc().initWithFrame_(NSMakeRect(btn_x - 2, btn_y - 4, btn_w + 4, btn_h + 6))
        shadow.setWantsLayer_(True)
        shadow.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.06, 0.06, 0.09, 1.0).CGColor())
        shadow.layer().setCornerRadius_(20)
        cv.addSubview_(shadow)

        # Main button face
        self.btn_face = NSView.alloc().initWithFrame_(NSMakeRect(btn_x, btn_y, btn_w, btn_h))
        self.btn_face.setWantsLayer_(True)
        self.btn_face.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.2, 0.2, 0.55, 1.0).CGColor())
        self.btn_face.layer().setCornerRadius_(18)
        self.btn_face.layer().setBorderWidth_(2.5)
        self.btn_face.layer().setBorderColor_(NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.8, 0.7).CGColor())
        cv.addSubview_(self.btn_face)

        # Top highlight (3D shine)
        self.btn_shine = NSView.alloc().initWithFrame_(NSMakeRect(btn_x + 8, btn_y + btn_h - 40, btn_w - 16, 28))
        self.btn_shine.setWantsLayer_(True)
        self.btn_shine.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.85, 0.25).CGColor())
        self.btn_shine.layer().setCornerRadius_(12)
        cv.addSubview_(self.btn_shine)

        # Button label
        self.ptt_label = NSTextField.alloc().initWithFrame_(NSMakeRect(btn_x, btn_y + 20, btn_w, 45))
        self.ptt_label.setStringValue_("PUSH TO TALK")
        self.ptt_label.setFont_(NSFont.boldSystemFontOfSize_(24))
        self.ptt_label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.85, 0.85, 1.0, 1.0))
        self.ptt_label.setBezeled_(False); self.ptt_label.setEditable_(False); self.ptt_label.setDrawsBackground_(False)
        self.ptt_label.setAlignment_(NSTextAlignmentCenter)
        cv.addSubview_(self.ptt_label)

        # Mic indicator dot
        self.mic_dot = NSView.alloc().initWithFrame_(NSMakeRect(btn_x + btn_w // 2 - 5, btn_y + 8, 10, 10))
        self.mic_dot.setWantsLayer_(True)
        self.mic_dot.layer().setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.7, 0.8).CGColor())
        self.mic_dot.layer().setCornerRadius_(5)
        cv.addSubview_(self.mic_dot)

        # Transcript
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(15, 15, w - 30, h - 270))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        self.transcript = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 34, h - 274))
        self.transcript.setEditable_(False)
        self.transcript.setFont_(NSFont.fontWithName_size_("Menlo", 11))
        self.transcript.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.75, 0.75, 0.75, 1.0))
        self.transcript.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.05, 0.05, 0.07, 1.0))
        scroll.setDocumentView_(self.transcript)
        cv.addSubview_(scroll)

        # Mouse monitors for PTT button area
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSLeftMouseDownMask, self._mouse_down)
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSLeftMouseUpMask, self._mouse_up)
        # Key monitors for SPACE
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSKeyDownMask, self._key_down)
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSKeyUpMask, self._key_up)

        self.win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

        # Auto-connect in background
        threading.Thread(target=self._bg_connect, daemon=True).start()

        AppHelper.runEventLoop()

    def _hit_test_btn(self, event):
        """Check if click is inside the PTT button area."""
        try:
            pt = event.locationInWindow()
            bx, by, bw, bh = self._btn_frame
            return bx <= pt.x <= bx + bw and by <= pt.y <= by + bh
        except:
            return False

    def _set_btn_pressed(self, pressed):
        """Update button visuals for pressed/released state."""
        if pressed:
            self.btn_face.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.08, 0.6, 0.2, 1.0).CGColor())
            self.btn_face.layer().setBorderColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.15, 0.85, 0.35, 0.9).CGColor())
            self.btn_shine.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.2, 0.8, 0.4, 0.2).CGColor())
            self.ptt_label.setStringValue_("TALKING")
            self.ptt_label.setFont_(NSFont.boldSystemFontOfSize_(30))
            self.ptt_label.setTextColor_(NSColor.whiteColor())
            self.mic_dot.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.2, 1.0, 0.4, 1.0).CGColor())
            # Push-in effect: shift face down 2px
            bx, by, bw, bh = self._btn_frame
            self.btn_face.setFrame_(NSMakeRect(bx + 1, by - 2, bw - 2, bh - 2))
        else:
            self.btn_face.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.2, 0.2, 0.55, 1.0).CGColor())
            self.btn_face.layer().setBorderColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.8, 0.7).CGColor())
            self.btn_shine.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.85, 0.25).CGColor())
            self.ptt_label.setStringValue_("PUSH TO TALK")
            self.ptt_label.setFont_(NSFont.boldSystemFontOfSize_(24))
            self.ptt_label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.85, 0.85, 1.0, 1.0))
            self.mic_dot.layer().setBackgroundColor_(
                NSColor.colorWithRed_green_blue_alpha_(0.35, 0.35, 0.7, 0.8).CGColor())
            # Restore position
            bx, by, bw, bh = self._btn_frame
            self.btn_face.setFrame_(NSMakeRect(bx, by, bw, bh))

    def _set_btn_disabled(self):
        self.btn_face.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.15, 0.15, 0.2, 1.0).CGColor())
        self.btn_face.layer().setBorderColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.25, 0.25, 0.3, 0.5).CGColor())
        self.btn_shine.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.2, 0.2, 0.25, 0.1).CGColor())
        self.ptt_label.setStringValue_("CONNECTING...")
        self.ptt_label.setFont_(NSFont.boldSystemFontOfSize_(20))
        self.ptt_label.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.4, 0.4, 0.45, 1.0))
        self.mic_dot.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.3, 0.3, 0.35, 0.5).CGColor())

    def _mouse_down(self, event):
        if self.connected and self._hit_test_btn(event):
            self._mouse_held = True
            self._set_btn_pressed(True)
            self._start_talking()
        return event

    def _mouse_up(self, event):
        if self._mouse_held:
            self._mouse_held = False
            self._set_btn_pressed(False)
            self._stop_talking()
        return event

    def voiceChanged_(self, sender):
        idx = sender.indexOfSelectedItem()
        new_voice = VOICES[idx][0]
        if new_voice != self.current_voice:
            self.current_voice = new_voice
            self._add_text(f"\n[Switching to {VOICES[idx][1]}...]\n")
            self._set_status("Switching voice...")
            self._wants_reconnect = True
            if self.ws and not self.ws.closed:
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)

    def _start_talking(self):
        if self.connected and not self.talking:
            self.talking = True
            self.status.setStringValue_("Listening...")

    def _stop_talking(self):
        if self.talking:
            self.talking = False
            self._audio_queue.clear()
            self.status.setStringValue_("Fia is thinking...")

    def _key_down(self, event):
        if event.keyCode() == 49 and not event.isARepeat():
            if self.connected and not self._space_held:
                self._space_held = True
                self._set_btn_pressed(True)
                self._start_talking()
            return None
        return event

    def _key_up(self, event):
        if event.keyCode() == 49:
            if self._space_held:
                self._space_held = False
                self._set_btn_pressed(False)
                self._stop_talking()
            return None
        return event

    def _set_status(self, text):
        AppHelper.callAfter(lambda: self.status.setStringValue_(text))

    def _add_text(self, text):
        def _do():
            s = self.transcript.textStorage()
            attrs = {
                "NSFont": NSFont.fontWithName_size_("Menlo", 11),
                "NSColor": NSColor.colorWithRed_green_blue_alpha_(0.75, 0.75, 0.75, 1.0),
            }
            a = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            s.appendAttributedString_(a)
            self.transcript.scrollRangeToVisible_((s.length(), 0))
        AppHelper.callAfter(_do)

    def _bg_connect(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._set_status("Starting PersonaPlex...")
        if not ensure_personaplex():
            self._set_status("Failed to start PersonaPlex")
            return
        # Connect with auto-retry on failure
        while True:
            self.loop.run_until_complete(self._connect())
            # Wait for PersonaPlex to fully release the old session
            self._set_status("Reconnecting...")
            time.sleep(3)
            # Make sure PersonaPlex is still running
            if not ensure_personaplex():
                self._set_status("Failed to start PersonaPlex")
                return

    async def _connect(self):
        try:
            print("[PTT] _connect() starting...")
            self._set_status(f"Connecting ({self.current_voice})...")
            AppHelper.callAfter(self._set_btn_disabled)
            self.session = aiohttp.ClientSession()
            url = f"{WS_URL}?voice_prompt={self.current_voice}"
            print(f"[PTT] Connecting to {url}")
            self.ws = await asyncio.wait_for(self.session.ws_connect(url), timeout=30)
            print("[PTT] WebSocket connected, waiting for handshake...")
            msg = await asyncio.wait_for(self.ws.receive(), timeout=60)
            print(f"[PTT] Got handshake: {msg.data!r}")
            if msg.data != b"\x00":
                self._set_status("Error: no handshake")
                return

            self.connected = True
            print("[PTT] CONNECTED! Ready to talk.")
            self._set_status(f"Ready — hold button to talk ({self.current_voice})")
            AppHelper.callAfter(lambda: self._set_btn_pressed(False))

            self.out_stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1,
                                              dtype="float32", blocksize=960,
                                              callback=self._out_cb)
            self.out_stream.start()

            # Find a working input device (mic) — default may be -1 if none
            in_dev = None
            if sd.default.device[0] >= 0:
                in_dev = sd.default.device[0]
            else:
                # Search for any device with input channels
                for i, d in enumerate(sd.query_devices()):
                    if d['max_input_channels'] > 0:
                        in_dev = i
                        print(f"[PTT] Using input device {i}: {d['name']}")
                        break
            if in_dev is not None:
                self.in_stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                                dtype="float32", blocksize=960,
                                                device=in_dev,
                                                callback=self._in_cb)
                self.in_stream.start()
            else:
                print("[PTT] No microphone found — sending silence (listen-only mode)")
                self._set_status(f"Ready — NO MIC (listen only) ({self.current_voice})")
                self.in_stream = None

            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.BINARY and msg.data:
                    if msg.data[0] == 0x01:
                        pcm = np.frombuffer(msg.data[1:], dtype=np.float32)
                        if pcm.shape[-1] > 0:
                            self._audio_queue.append(pcm)
                    elif msg.data[0] == 0x02:
                        text = msg.data[1:].decode("utf-8", errors="replace")
                        self._add_text(text)
                        self._set_status("Fia is speaking...")
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        except Exception as e:
            print(f"[PTT] Connection error: {e}")
            self._set_status(f"Error: {e}")
        finally:
            self.connected = False
            AppHelper.callAfter(self._set_btn_disabled)
            for s in (self.in_stream, self.out_stream):
                if s:
                    try: s.stop(); s.close()
                    except: pass
            self.in_stream = self.out_stream = None
            if self.ws and not self.ws.closed:
                await self.ws.close()
            if self.session and not self.session.closed:
                await self.session.close()
            self.ws = self.session = None
            print("[PTT] Disconnected")

    def _in_cb(self, indata, frames, ti, status):
        if not self.connected:
            return
        if self.talking:
            pcm = indata[:, 0].copy().astype(np.float32)
        else:
            pcm = np.zeros(frames, dtype=np.float32)
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send(pcm.tobytes()), self.loop)

    async def _send(self, data):
        try:
            if self.ws and not self.ws.closed:
                await self.ws.send_bytes(b"\x01" + data)
        except: pass

    def _out_cb(self, outdata, frames, ti, status):
        out = outdata[:, 0]
        if self.talking:
            out[:] = 0.0
            return
        written = 0
        while written < frames and self._audio_queue:
            chunk = self._audio_queue[0]
            n = min(len(chunk), frames - written)
            out[written:written + n] = chunk[:n]
            written += n
            if n >= len(chunk):
                self._audio_queue.pop(0)
            else:
                self._audio_queue[0] = chunk[n:]
        if written < frames:
            out[written:] = 0.0


def kill_other_instances():
    """Kill any other fia_ptt.py processes so we get the PersonaPlex connection."""
    import os, signal
    my_pid = os.getpid()
    try:
        out = subprocess.check_output(["pgrep", "-f", "fia_ptt.py"], text=True)
        for line in out.strip().split("\n"):
            pid = int(line.strip())
            if pid != my_pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                    print(f"[PTT] Killed old instance (PID {pid})")
                except ProcessLookupError:
                    pass
    except subprocess.CalledProcessError:
        pass  # no other instances


if __name__ == "__main__":
    kill_other_instances()
    time.sleep(1)  # Let old connections clean up
    FiaPTT().run()
