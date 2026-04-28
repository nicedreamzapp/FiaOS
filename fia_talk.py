"""Fia Talk — Native macOS press-to-talk window for PersonaPlex."""

import asyncio
import threading
import numpy as np
import sounddevice as sd
import sphn
import aiohttp
import objc

from AppKit import (
    NSApplication, NSWindow, NSButton, NSTextField, NSScrollView, NSTextView,
    NSBackingStoreBuffered, NSMakeRect, NSFont, NSColor, NSView, NSEvent,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
    NSBezelStyleRounded, NSTextAlignmentCenter,
    NSApplicationActivationPolicyRegular, NSScreen,
    NSAttributedString, NSBezierPath,
    NSLeftMouseDownMask, NSLeftMouseUpMask, NSKeyDownMask, NSKeyUpMask,
    NSFlagsChangedMask,
)
from PyObjCTools import AppHelper

SAMPLE_RATE = 24000
PERSONAPLEX_WS = "ws://localhost:8998/api/chat"

# Global ref so event monitors can reach it
_app = None


class FiaTalkApp:
    def __init__(self):
        self.talking = False
        self.connected = False
        self.ws = None
        self.session = None
        self.loop = None
        self.opus_writer = None
        self.opus_reader = None
        self.out_stream = None
        self.in_stream = None
        self._audio_queue = []
        self.ptt_btn = None
        self.ptt_label = None

    def build_window(self):
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)

        screen = NSScreen.mainScreen().frame()
        w, h = 380, 480
        x = int((screen.size.width - w) / 2)
        y = int((screen.size.height - h) / 2)

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, False,
        )
        self.window.setTitle_("Fia Talk")
        self.window.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.06, 0.06, 0.09, 1.0))
        self.window.setMinSize_((320, 400))

        content = self.window.contentView()

        # Title
        title = NSTextField.alloc().initWithFrame_(NSMakeRect(20, h - 55, w - 40, 35))
        title.setStringValue_("Fia")
        title.setFont_(NSFont.boldSystemFontOfSize_(28))
        title.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.39, 0.40, 0.95, 1.0))
        title.setBezeled_(False)
        title.setEditable_(False)
        title.setDrawsBackground_(False)
        title.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(title)

        # Status label
        self.status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, h - 85, w - 40, 22))
        self.status_label.setStringValue_("Click Connect to start")
        self.status_label.setFont_(NSFont.systemFontOfSize_(13))
        self.status_label.setTextColor_(NSColor.grayColor())
        self.status_label.setBezeled_(False)
        self.status_label.setEditable_(False)
        self.status_label.setDrawsBackground_(False)
        self.status_label.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(self.status_label)

        # Connect button
        self.connect_btn = NSButton.alloc().initWithFrame_(NSMakeRect(w // 2 - 70, h - 130, 140, 36))
        self.connect_btn.setTitle_("Connect")
        self.connect_btn.setBezelStyle_(NSBezelStyleRounded)
        self.connect_btn.setFont_(NSFont.systemFontOfSize_(14))
        self.connect_btn.setTarget_(self)
        self.connect_btn.setAction_("connectClicked:")
        content.addSubview_(self.connect_btn)

        # PTT button — standard NSButton, we detect press/release via global event monitor
        self.ptt_btn = NSButton.alloc().initWithFrame_(NSMakeRect(w // 2 - 90, h - 210, 180, 50))
        self.ptt_btn.setTitle_("Hold SPACE to Talk")
        self.ptt_btn.setBezelStyle_(NSBezelStyleRounded)
        self.ptt_btn.setFont_(NSFont.boldSystemFontOfSize_(16))
        self.ptt_btn.setEnabled_(False)
        content.addSubview_(self.ptt_btn)

        # Transcript scroll view
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, w - 40, h - 240))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_((1 << 1) | (1 << 4))

        self.transcript = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 44, h - 244))
        self.transcript.setEditable_(False)
        self.transcript.setFont_(NSFont.fontWithName_size_("Menlo", 12))
        self.transcript.setTextColor_(NSColor.colorWithRed_green_blue_alpha_(0.8, 0.8, 0.8, 1.0))
        self.transcript.setBackgroundColor_(NSColor.colorWithRed_green_blue_alpha_(0.05, 0.05, 0.08, 1.0))
        self.transcript.setAutoresizingMask_((1 << 1) | (1 << 4))
        scroll.setDocumentView_(self.transcript)
        content.addSubview_(scroll)

        self.window.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)

        # Global event monitors for SPACE key press/release
        self._space_held = False
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSKeyDownMask, self._handle_key_down
        )
        NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSKeyUpMask, self._handle_key_up
        )

    def _handle_key_down(self, event):
        # Space = keyCode 49
        if event.keyCode() == 49 and not event.isARepeat():
            if self.connected and not self._space_held:
                self._space_held = True
                self.start_talking()
            return None  # consume the event
        return event

    def _handle_key_up(self, event):
        if event.keyCode() == 49:
            if self._space_held:
                self._space_held = False
                self.stop_talking()
            return None
        return event

    def set_status(self, text):
        AppHelper.callAfter(lambda: self.status_label.setStringValue_(text))

    def append_transcript(self, text):
        def _do():
            storage = self.transcript.textStorage()
            attrs = {
                "NSFont": NSFont.fontWithName_size_("Menlo", 12),
                "NSColor": NSColor.colorWithRed_green_blue_alpha_(0.8, 0.8, 0.8, 1.0),
            }
            astr = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            storage.appendAttributedString_(astr)
            self.transcript.scrollRangeToVisible_((storage.length(), 0))
        AppHelper.callAfter(_do)

    def connectClicked_(self, sender):
        if self.connected:
            self.disconnect()
        else:
            self.set_status("Connecting to PersonaPlex...")
            self.connect_btn.setTitle_("Disconnect")
            t = threading.Thread(target=self._run_async_connect, daemon=True)
            t.start()

    def start_talking(self):
        if not self.connected:
            return
        self.talking = True
        AppHelper.callAfter(lambda: self.ptt_btn.setTitle_("TALKING..."))
        self.set_status("Listening...")
        self._audio_queue.clear()

    def stop_talking(self):
        self.talking = False
        AppHelper.callAfter(lambda: self.ptt_btn.setTitle_("Hold SPACE to Talk"))
        self.set_status("Fia is thinking...")

    def _run_async_connect(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _connect(self):
        try:
            print("[FiaTalk] Connecting to PersonaPlex...", flush=True)
            self.session = aiohttp.ClientSession()
            self.ws = await asyncio.wait_for(
                self.session.ws_connect(PERSONAPLEX_WS), timeout=10
            )
            print("[FiaTalk] WebSocket connected, waiting for handshake...", flush=True)
            msg = await asyncio.wait_for(self.ws.receive(), timeout=15)
            if not (msg.type == aiohttp.WSMsgType.BINARY and msg.data == b"\x00"):
                raise Exception("No ready signal from PersonaPlex")
            print("[FiaTalk] Handshake OK — ready!", flush=True)

            self.opus_writer = sphn.OpusStreamWriter(SAMPLE_RATE)
            self.opus_reader = sphn.OpusStreamReader(SAMPLE_RATE)
            self.connected = True

            AppHelper.callAfter(lambda: self.ptt_btn.setEnabled_(True))
            self.set_status("Ready — hold SPACE to talk")

            self.out_stream = sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1920, callback=self._audio_out_callback,
            )
            self.out_stream.start()

            self.in_stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=1920, callback=self._audio_in_callback,
            )
            self.in_stream.start()

            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data
                    if not data:
                        continue
                    if data[0] == 0x01:
                        pcm = self.opus_reader.append_bytes(data[1:])
                        if pcm.shape[-1] > 0:
                            self._audio_queue.append(pcm.flatten().astype(np.float32))
                    elif data[0] == 0x02:
                        text = data[1:].decode("utf-8", errors="replace")
                        self.append_transcript(text)
                        self.set_status("Fia is speaking...")
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        except Exception as e:
            self.set_status(f"Error: {e}")
        finally:
            self.disconnect()

    def _audio_in_callback(self, indata, frames, time_info, status):
        if not self.talking or not self.connected or not self.ws:
            return
        pcm = indata[:, 0].copy()
        encoded = self.opus_writer.append_pcm(pcm)
        if len(encoded) > 0 and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_audio(encoded), self.loop)

    async def _send_audio(self, encoded):
        try:
            if self.ws and not self.ws.closed:
                await self.ws.send_bytes(b"\x01" + encoded)
        except Exception:
            pass

    def _audio_out_callback(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        written = 0
        while written < frames and self._audio_queue:
            chunk = self._audio_queue[0]
            available = len(chunk)
            needed = frames - written
            to_copy = min(available, needed)
            out[written:written + to_copy] = chunk[:to_copy]
            written += to_copy
            if to_copy >= available:
                self._audio_queue.pop(0)
            else:
                self._audio_queue[0] = chunk[to_copy:]
        if written < frames:
            out[written:] = 0.0

    def disconnect(self):
        self.connected = False
        self.talking = False
        for stream in (self.in_stream, self.out_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
        if self.ws:
            try:
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
            except Exception:
                pass
            self.ws = None
        if self.session:
            try:
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            except Exception:
                pass
            self.session = None
        self._audio_queue.clear()
        self.set_status("Disconnected")
        AppHelper.callAfter(lambda: self.ptt_btn.setEnabled_(False))
        AppHelper.callAfter(lambda: self.ptt_btn.setTitle_("Hold SPACE to Talk"))
        AppHelper.callAfter(lambda: self.connect_btn.setTitle_("Connect"))


def main():
    # Trigger macOS mic permission dialog before building window
    import sounddevice as sd
    try:
        print("[FiaTalk] Requesting mic access...")
        test = sd.InputStream(samplerate=24000, channels=1, dtype="float32", blocksize=1920)
        test.start()
        import time; time.sleep(0.2)
        test.stop()
        test.close()
        print("[FiaTalk] Mic access granted")
    except Exception as e:
        print(f"[FiaTalk] Mic error: {e}")

    fia = FiaTalkApp()
    fia.build_window()
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
