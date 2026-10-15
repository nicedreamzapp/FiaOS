"""The live-screen wire protocol: tile records, grid geometry, input events,
and the warm-worker cache in screencast.py.

The capture worker and input driver themselves need Quartz (macOS) or the
Win32 API, so they are only exercised in the macOS-only tests at the bottom.
"""
import asyncio
import json
import re

import pytest

from conftest import MAC_DIR, WIN_DIR, load_variant, macos_only, run


@pytest.fixture
def sc(variant):
    return load_variant(variant).screencast


# ── geometry ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("full,want,expected", [
    ((1920, 1080), 1720, {"step": 1, "w": 1920, "h": 1080, "cols": 15, "rows": 9}),
    ((3456, 2234), 1720, {"step": 2, "w": 1728, "h": 1117, "cols": 14, "rows": 9}),
    ((5120, 2880), 1720, {"step": 3, "w": 1707, "h": 960, "cols": 14, "rows": 8}),
    ((800, 600), 1720, {"step": 1, "w": 800, "h": 600, "cols": 7, "rows": 5}),
    ((3441, 1441), 1720, {"step": 2, "w": 1721, "h": 721, "cols": 14, "rows": 6}),
])
def test_geometry(sc, full, want, expected):
    geo = sc.geometry(*full, want)
    assert geo == {**expected, "tile": 128}


@pytest.mark.parametrize("full_w,full_h,want", [
    (1920, 1080, 1720), (3456, 2234, 1720), (5120, 2880, 640),
    (2560, 1600, 2560), (1366, 768, 900), (7, 5, 1720),
])
def test_geometry_matches_numpy_subsample(sc, full_w, full_h, want):
    """The server's promised grid must equal what the worker's frame[::step]
    actually produces, or tiles land in the wrong slots."""
    np = pytest.importorskip("numpy")
    geo = sc.geometry(full_w, full_h, want)
    frame = np.zeros((full_h, full_w, 3), dtype=np.uint8)
    small = frame[::geo["step"], ::geo["step"]]
    assert small.shape[:2] == (geo["h"], geo["w"])


@pytest.mark.parametrize("path", [MAC_DIR / "screen_worker.py", WIN_DIR / "screen_worker.py"],
                         ids=["mac", "win"])
def test_worker_uses_same_tile_and_step(path):
    """screen_worker.py can't be imported off its platform, so check its source
    agrees with screencast.geometry on the two numbers that must match."""
    src = path.read_text()
    assert re.search(r"^TILE = 128$", src, re.M)
    assert "max(1, round(full_w / want_w))" in src


def test_mac_and_windows_geometry_agree():
    mac = load_variant("mac").screencast
    win = load_variant("win").screencast
    for dims in [(1920, 1080, 1720), (3456, 2234, 1720), (2560, 1440, 1280)]:
        assert mac.geometry(*dims) == win.geometry(*dims)


# ── tile records ──────────────────────────────────────────────────────────

class FakeWorker:
    def __init__(self, data: bytes):
        async def make():
            reader = asyncio.StreamReader()
            reader.feed_data(data)
            reader.feed_eof()
            return reader
        self._make = make

    async def ready(self):
        self.stdout = await self._make()
        return self


def record(index: int, jpeg: bytes) -> bytes:
    """The worker's stdout format: 2-byte index, 4-byte length, payload."""
    return index.to_bytes(2, "big") + len(jpeg).to_bytes(4, "big") + jpeg


def test_tiles_parses_worker_records(sc):
    payloads = [(0, b"\xff\xd8first"), (13, b""), (65535, b"x" * 70000)]
    stream = b"".join(record(i, j) for i, j in payloads)

    async def go():
        proc = await FakeWorker(stream).ready()
        got = []
        with pytest.raises(asyncio.IncompleteReadError):
            async for item in sc.tiles(proc):
                got.append(item)
        return got
    assert run(go()) == payloads


def test_tiles_truncated_record_raises(sc):
    async def go():
        proc = await FakeWorker(record(1, b"abcdef")[:-2]).ready()
        async for _ in sc.tiles(proc):
            pass
    with pytest.raises(asyncio.IncompleteReadError):
        run(go())


def test_pack(sc):
    assert sc.pack(0x0102, b"JPEG") == b"\x01\x02JPEG"
    with pytest.raises(OverflowError):
        sc.pack(70000, b"")


# ── input events ──────────────────────────────────────────────────────────

def test_to_screen_coords(sc):
    ev = sc.to_screen_coords({"t": "move", "fx": 0.5, "fy": 0.25}, 1920, 1080)
    assert ev == {"t": "move", "x": 960, "y": 270}
    assert sc.to_screen_coords({"t": "scroll", "dy": -3}, 100, 100) == {"t": "scroll", "dy": -3}


@pytest.mark.parametrize("t", ["move", "down", "up", "drag", "click", "dblclick",
                               "scroll", "text", "key", "combo"])
def test_encode_event_allowed(sc, t):
    line = sc.encode_event({"t": t, "fx": 1.0, "fy": 0.0}, 1440, 900)
    assert line.endswith("\n") and line.count("\n") == 1
    assert json.loads(line) == {"t": t, "x": 1440, "y": 0}


@pytest.mark.parametrize("ev", [{"t": "exec"}, {"t": None}, {}, {"t": "refresh"}])
def test_encode_event_rejects_unknown(sc, ev):
    assert sc.encode_event(ev, 100, 100) is None


def test_encode_event_text_is_one_line(sc):
    line = sc.encode_event({"t": "text", "s": "two\nlines"}, 100, 100)
    assert line.count("\n") == 1
    assert json.loads(line)["s"] == "two\nlines"


# ── warm worker cache ─────────────────────────────────────────────────────

class FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, b):
        self.written += b

    async def drain(self):
        pass


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.stdin = FakeStdin()

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def warm(sc, monkeypatch):
    started = []

    async def fake_start(python_path, worker_path, want_w, fps, quality):
        p = FakeProc()
        started.append(p)
        return p
    monkeypatch.setattr(sc, "start_worker", fake_start)
    sc._WARM.update({"proc": None, "key": None, "expires": 0.0})
    yield started
    sc._WARM.update({"proc": None, "key": None, "expires": 0.0})


def test_warm_worker_is_reused(sc, warm):
    async def go():
        a = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        await sc.release_worker(a, 1720, 10, 55)
        assert a.stdin.written == b"P\n"                 # parked = paused
        b = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        return a, b
    a, b = run(go())
    assert a is b
    assert len(warm) == 1
    assert a.stdin.written == b"P\nR\n"                  # resend every tile


def test_warm_worker_not_reused_for_other_settings(sc, warm):
    async def go():
        a = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        await sc.release_worker(a, 1720, 10, 55)
        b = await sc.acquire_worker("py", "w.py", 1280, 10, 55)
        return a, b
    a, b = run(go())
    assert a is not b
    assert a.returncode == -9                            # old one killed
    assert len(warm) == 2


def test_expired_warm_worker_not_reused(sc, warm):
    async def go():
        a = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        await sc.release_worker(a, 1720, 10, 55)
        sc._WARM["expires"] = 0.0
        b = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        return a, b
    a, b = run(go())
    assert a is not b and a.returncode == -9


def test_dead_worker_is_not_parked(sc, warm):
    async def go():
        a = await sc.acquire_worker("py", "w.py", 1720, 10, 55)
        a.returncode = 1
        await sc.release_worker(a, 1720, 10, 55)
        await sc.release_worker(None, 1720, 10, 55)
    run(go())
    assert sc._WARM["proc"] is None


# ── macOS-only: the real Quartz capture and input driver ──────────────────

@macos_only
def test_mac_screen_size_is_pixels_not_points():
    sc = load_variant("mac").screencast
    pw, ph = sc.screen_size()
    sw, sh = sc.screen_points()
    assert pw >= sw and ph >= sh


@macos_only
def test_mac_worker_grab_matches_screen_size():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mac_screen_worker", MAC_DIR / "screen_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    frame = worker.grab()
    if frame is None:
        pytest.skip("no Screen Recording permission for this process")
    assert (frame.shape[1], frame.shape[0]) == load_variant("mac").screencast.screen_size()


@macos_only
def test_mac_input_helper_ignores_unknown_keys():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mac_input_helper", MAC_DIR / "input_helper.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    assert helper.flags_for(["cmd", "nope"]) == helper.MODS["cmd"]
    assert helper.handle({"t": "key", "k": "NotAKey"}) is None      # posts nothing
