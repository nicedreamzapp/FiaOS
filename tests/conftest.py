"""Shared fixtures for the FiaOS pytest suite.

Everything here runs on a plain Linux box: no display, no GPU, no network, no
Claude Code, no Quartz. The real server.py (macOS) and windows/server.py are
imported as-is with a throwaway password, and every bit of state they persist
is redirected into pytest's tmp_path so a test run never touches live sessions.
"""
import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAC_DIR = ROOT
WIN_DIR = ROOT / "windows"
TEST_PASSWORD = "test-password-not-the-real-one"

# test_auth.py and regress_logout.py are standalone scripts (they run at import
# and call sys.exit). test_legacy_scripts.py runs them as subprocesses instead.
collect_ignore = ["test_auth.py", "regress_logout.py"]

# The macOS executor.py imports `anthropic` at the top but never calls it (it
# shells out to the `claude` CLI). Fall back to an empty stub when the SDK isn't
# installed so the suite doesn't need it.
try:
    import anthropic  # noqa: F401
except ImportError:
    sys.path.append(str(ROOT / "tests" / "stubs"))

os.environ.setdefault("FIAOS_PASSWORD", TEST_PASSWORD)

IS_MAC = sys.platform == "darwin"
try:
    import Quartz  # noqa: F401
    HAS_QUARTZ = True
except ImportError:
    HAS_QUARTZ = False

macos_only = pytest.mark.skipif(
    not (IS_MAC and HAS_QUARTZ),
    reason="needs macOS with pyobjc-framework-Quartz (CoreGraphics capture/input)")

# Sibling modules each server imports by bare name. The macOS and Windows
# copies share names, so they are loaded in turn and cached under unique names.
_SIBLINGS = ("executor", "screencast", "input_helper", "screen_worker")
_cache: dict = {}


def load_variant(variant: str):
    """Import server.py + executor.py + screencast.py for 'mac' or 'win'."""
    if variant in _cache:
        return _cache[variant]
    src_dir = MAC_DIR if variant == "mac" else WIN_DIR
    for name in _SIBLINGS + ("server",):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(src_dir))
    try:
        mods = {}
        for name in ("executor", "screencast", "server"):
            spec = importlib.util.spec_from_file_location(name, src_dir / f"{name}.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            mods[name] = mod
    finally:
        sys.path.remove(str(src_dir))
        for name in _SIBLINGS + ("server",):
            sys.modules.pop(name, None)
    ns = types.SimpleNamespace(**mods)
    _cache[variant] = ns
    return ns


@pytest.fixture(params=["mac", "win"])
def variant(request):
    return request.param


@pytest.fixture
def srv(variant, tmp_path, monkeypatch):
    """The real server module with state redirected into tmp_path."""
    m = load_variant(variant).server
    monkeypatch.setattr(m, "SESSION_FILE", tmp_path / ".sessions.json")
    monkeypatch.setattr(m, "REVOKED_FILE", tmp_path / ".revoked.json")
    # windows/ ships without its own copy of static/; installs copy it across.
    monkeypatch.setattr(m, "STATIC_DIR", ROOT / "static")
    # create_app() kills stray screen_worker.py / input_helper.py processes by
    # name; never let a test run do that on a developer's machine.
    monkeypatch.setattr(m, "_sweep_strays", lambda: None)
    m.sessions.clear()
    m.revoked.clear()
    m.login_attempts.clear()
    yield m
    m.sessions.clear()
    m.revoked.clear()
    m.login_attempts.clear()


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fake home directory, so the file API has somewhere safe to work."""
    h = tmp_path / "home" / "matt"
    (h / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


def run(coro):
    return asyncio.run(coro)
