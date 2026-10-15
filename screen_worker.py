#!/usr/bin/env python3
"""Screen capture worker for FiaOS.

Captures the display with Apple's own call (about 7 ms, no subprocess), splits it
into tiles, and writes only the tiles that changed to stdout. ffmpeg was the
first attempt and was 20x slower before it even worked.

Record format on stdout, repeated:
    2 bytes  tile index, big endian
    4 bytes  jpeg length, big endian
    n bytes  jpeg

stdin accepts single characters:
    R   forget what the far end has, so the next pass resends every tile

Usage: screen_worker.py <target_w>x<target_h> <fps> <jpeg_quality>
"""
import hashlib
import io
import os
import select
import sys
import time

import numpy as np
import Quartz
from PIL import Image

TILE = 128


def grab():
    """The whole screen as an RGB array."""
    img = Quartz.CGDisplayCreateImage(Quartz.CGMainDisplayID())
    if img is None:
        return None
    w = Quartz.CGImageGetWidth(img)
    h = Quartz.CGImageGetHeight(img)
    bpr = Quartz.CGImageGetBytesPerRow(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    arr = np.frombuffer(data, dtype=np.uint8)[: h * bpr].reshape(h, bpr // 4, 4)
    return arr[:, :w, :3][:, :, ::-1]      # BGRA -> RGB


def fit(frame, tw, th):
    """Resize a grab to exactly the size the viewer will paint.

    frame[::2, ::2] was free but it is point sampling, and it could only ever
    produce half or a third of the screen -- so the browser rescaled the result
    a second time to fill its box. Two resamples of antialiased text is what
    read as fuzzy. Resizing once, here, to the viewer's exact pixel box means
    the canvas is drawn 1:1 and nothing is resampled again.

    LANCZOS costs ~24 ms on a 2560x1440 grab and keeps edges crisp; below half
    size BOX is both faster and the right filter (plain area average).
    """
    h, w, _ = frame.shape
    if (tw, th) == (w, h):
        return np.ascontiguousarray(frame)
    flt = Image.LANCZOS if tw * 2 > w else Image.BOX
    return np.asarray(Image.fromarray(frame).resize((tw, th), flt))


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "0x0"
    fps = float(sys.argv[2]) if len(sys.argv) > 2 else 10
    quality = int(sys.argv[3]) if len(sys.argv) > 3 else 55

    first = grab()
    if first is None:
        sys.exit("no screen")
    full_h, full_w, _ = first.shape
    # The server already worked out the exact frame size for this viewer; never
    # second-guess it, and never send more than the panel really has.
    tw, th = (int(v) for v in target.lower().split("x"))
    tw = min(full_w, tw) if tw > 0 else full_w
    th = min(full_h, th) if th > 0 else full_h
    # 4:4:4 above q70: chroma subsampling is what smears colored text, and the
    # extra bytes only ride on tiles that actually changed.
    sub = 0 if quality >= 70 else 2
    out = sys.stdout.buffer
    hashes: dict[int, bytes] = {}
    interval = 1.0 / fps
    paused = False

    while True:
        started = time.monotonic()

        # If the server that launched us is gone, stop capturing. Without this a
        # service restart leaves a worker running against a dead pipe forever.
        if os.getppid() == 1:
            return

        # stdin commands from the server:
        #   R  the viewer wants every tile again
        #   P  no viewer is attached -- stop capturing entirely
        # Parked used to mean "keep grabbing the screen at full fps into a pipe
        # nobody is reading." That burned CPU, and on a laptop battery, for as
        # long as the worker was held warm. Now P blocks here until the server
        # sends anything (acquire_worker sends R), so an unwatched machine does
        # no work at all.
        while select.select([sys.stdin], [], [], 0)[0]:
            line = sys.stdin.readline()
            if not line:
                return
            paused = line.startswith("P")
            hashes.clear()

        while paused:
            if os.getppid() == 1:
                return
            # 5 s so an orphaned worker still notices its server went away
            if select.select([sys.stdin], [], [], 5.0)[0]:
                line = sys.stdin.readline()
                if not line:
                    return
                if not line.startswith("P"):
                    paused = False
                    hashes.clear()

        frame = grab()
        if frame is None:
            time.sleep(interval)
            continue
        small = fit(frame, tw, th)
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
                Image.fromarray(tile).save(buf, "JPEG", quality=quality,
                                           subsampling=sub, optimize=False)
                jpeg = buf.getvalue()
                out.write(index.to_bytes(2, "big") + len(jpeg).to_bytes(4, "big") + jpeg)
        out.flush()

        # hold the requested pace; a still screen costs one capture and nothing else
        rest = interval - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)


if __name__ == "__main__":
    # tell the server the streamed geometry before anything else, on stderr-free stdout
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        os._exit(0)
