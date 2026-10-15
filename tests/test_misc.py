"""Smaller pure helpers: the terminal self-kill guard, key code tables, the
Windows peer list, and the shell scripts in the repo."""
import os
import shutil
import subprocess

import pytest

from conftest import ROOT, load_variant


# ── terminal guard: don't let the remote shell kill FiaOS itself ──────────

@pytest.mark.parametrize("cmd", [
    "launchctl unload ~/Library/LaunchAgents/com.fiaos.server.plist",
    "launchctl stop com.fiaos.caffeinate",
    "pkill -f server.py",
    "PKILL -f FIAOS",
    "kill -9 $(pgrep -f server.py)",
    "killall Python",
    "killall python3",
])
def test_terminal_guard_blocks_self_kill(srv, cmd):
    assert srv._is_self_destructive(cmd)


@pytest.mark.parametrize("cmd", ["ls", "top", "vim server.py", "claude", "kill %1"])
def test_terminal_guard_allows_normal_use(srv, cmd):
    assert not srv._is_self_destructive(cmd)


@pytest.mark.parametrize("cmd", [
    "taskkill /IM python.exe /F",
    "Stop-Process -Name python",
    "Unregister-ScheduledTask -TaskName FiaOS",
    "schtasks /delete /tn FiaOS",
    "Stop-Service fia",
])
def test_windows_terminal_guard(cmd):
    assert load_variant("win").server._is_self_destructive(cmd)


# ── key codes (macOS REST keyboard route) ─────────────────────────────────

def test_key_to_code():
    srv = load_variant("mac").server
    assert srv._key_to_code("return") == 36
    assert srv._key_to_code("escape") == 53
    assert srv._key_to_code("F12") == 111
    assert srv._key_to_code("left arrow") == 123
    assert srv._key_to_code("no such key") == 36      # documented fallback: Return


def test_input_helper_keycodes_agree_with_server():
    """input_helper.py (live view) and server.py (REST) map the same keys."""
    import ast
    src = (ROOT / "input_helper.py").read_text()
    tree = ast.parse(src)
    table = next(ast.literal_eval(n.value) for n in tree.body
                 if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "KEYCODE")
    srv = load_variant("mac").server
    pairs = {"Enter": "return", "Tab": "tab", "Space": "space", "Backspace": "delete",
             "Escape": "escape", "ArrowLeft": "left arrow", "ArrowRight": "right arrow",
             "ArrowUp": "up arrow", "ArrowDown": "down arrow", "F1": "F1", "F12": "F12"}
    for browser, server_name in pairs.items():
        assert table[browser] == srv._key_to_code(server_name), browser


# ── FIAOS_PEERS parsing (both builds) ──────────────────────────────────────────

@pytest.mark.parametrize("spec,expected", [
    ("", {}),
    (None, {}),
    ("mini=10.0.0.5:9000", {"mini": ["10.0.0.5:9000"]}),
    ("mini=10.0.0.5:9000;m5=10.0.0.9:9000,10.0.0.10:9000",
     {"mini": ["10.0.0.5:9000"], "m5": ["10.0.0.9:9000", "10.0.0.10:9000"]}),
    (" m5 = a:1 , b:2 ;", {"m5": ["a:1", "b:2"]}),
    ("garbage;pc=;mini=x:1", {"mini": ["x:1"]}),
    ("pc=host:9000=odd", {"pc": ["host:9000=odd"]}),
])
def test_parse_peers(variant, spec, expected):
    assert load_variant(variant).server._parse_peers(spec) == expected


def test_peer_targets_never_include_self(variant):
    srv = load_variant(variant).server
    assert srv.TARGETS[srv.MACHINE_KEY] == []


def test_mac_targets_come_from_fiaos_peers():
    """The macOS build keeps no addresses in source: the map is FIAOS_PEERS,
    and this machine's own entry is emptied so it never proxies to itself."""
    import json
    import subprocess
    import sys
    from conftest import ROOT
    env = dict(os.environ, FIAOS_MACHINE="mini", PYTHONPATH=str(ROOT / "tests" / "stubs"),
               FIAOS_PEERS="mini=10.0.0.5:9000;m5=10.0.0.9:9000,10.0.0.10:9000;pc=10.0.0.12:9000")
    code = "import json, server; print(json.dumps(server.TARGETS))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, env=env, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {
        "mini": [], "m5": ["10.0.0.9:9000", "10.0.0.10:9000"], "pc": ["10.0.0.12:9000"]}

    env.pop("FIAOS_PEERS")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, env=env, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"mini": []}


# ── shell scripts at least parse ──────────────────────────────────────────

SHELL_SCRIPTS = ["start.sh", "launch_server.sh", "watchdog.sh", "start_personaplex.sh",
                 "deploy/tunnel_to_vps.sh", "deploy/tunnel_with_stale_port_clear.sh",
                 "tests/test_infra.sh"]


@pytest.mark.parametrize("script", SHELL_SCRIPTS)
def test_shell_script_syntax(script):
    path = ROOT / script
    first = path.read_text().splitlines()[0]
    shell = "zsh" if "zsh" in first else "bash"
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    result = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
