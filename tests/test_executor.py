"""executor.py: the command safety filter and the NL -> shell pipeline.

The LLM calls (`claude -p`, `ollama run`) are replaced with fake subprocesses,
so these tests need no Claude Code install, no Ollama and no network.
"""
import asyncio
import sys

import pytest

from conftest import load_variant, run


@pytest.fixture
def ex(variant):
    return load_variant(variant).executor


@pytest.fixture
def mac_ex():
    return load_variant("mac").executor


@pytest.fixture
def win_ex():
    return load_variant("win").executor


# ── safety filter (shared by both platforms) ──────────────────────────────

BLOCKED_EVERYWHERE = [
    "rm -rf /",
    "rm -rf /*",
    "rm -rf ~",
    "sudo rm -rf /usr",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/disk0",
    "shutdown -h now",
    "reboot",
    "halt",
    "newfs /dev/disk2",
    "diskutil eraseDisk APFS x disk2",
    "launchctl unload ~/Library/LaunchAgents/com.fiaos.server.plist",
    "pkill -f server.py",
    "kill $(pgrep -f server.py)",
    "pkill fiaos",
    "pkill -f personaplex",
    "ls; reboot",
]

ALLOWED_EVERYWHERE = [
    "ls -la ~/Desktop",
    "rm -rf ./build",
    "rm -rf /tmp/scratch",
    "df -h",
    "open -a Safari",
    "echo hello",
    'claude -p "please do not shutdown or reboot anything"',   # words inside data
    "echo 'mkfs is a command'",
    "git log --oneline",
]


@pytest.mark.parametrize("cmd", BLOCKED_EVERYWHERE)
def test_dangerous_commands_blocked(ex, cmd):
    assert not ex.is_command_safe(cmd)


@pytest.mark.parametrize("cmd", ALLOWED_EVERYWHERE)
def test_ordinary_commands_allowed(ex, cmd):
    assert ex.is_command_safe(cmd)


@pytest.mark.parametrize("cmd", [
    "launchctl stop caffeinate",
])
def test_mac_only_blocks(mac_ex, cmd):
    assert not mac_ex.is_command_safe(cmd)


@pytest.mark.parametrize("cmd", [
    "format C:",
    r"Remove-Item C:\Users\alice -Recurse -Force",
    "Remove-Item $env:USERPROFILE -Recurse",
    "rd /s /q C:\\Users",
    "rmdir /s C:\\",
    "del /s /q *",
    "cipher /w:C",
    "Stop-Computer",
    "Restart-Computer -Force",
    "shutdown /s /t 0",
    "Stop-Process -Name python",
    "taskkill /IM python.exe /F",
    "Stop-ScheduledTask -TaskName FiaOS",
    "schtasks /end /tn FiaOS",
])
def test_windows_blocks(win_ex, cmd):
    assert not win_ex.is_command_safe(cmd)


@pytest.mark.parametrize("cmd", [
    r"Remove-Item .\build -Recurse",       # relative path: a normal cleanup
    "Get-ChildItem",
    "start notepad",
])
def test_windows_allows(win_ex, cmd):
    assert win_ex.is_command_safe(cmd)


def test_strip_quoted_strings(ex):
    assert ex._strip_quoted_strings('claude -p "reboot now"') == 'claude -p ""'
    assert ex._strip_quoted_strings("echo 'halt'") == "echo ''"
    assert ex._strip_quoted_strings(r'echo "a \"reboot\" b"') == 'echo ""'
    assert ex._strip_quoted_strings("plain reboot") == "plain reboot"


# ── NL interpretation with a fake LLM subprocess ──────────────────────────

class FakeProc:
    def __init__(self, out: bytes, returncode=0):
        self._out = out
        self.returncode = returncode
        self.killed = False

    async def communicate(self, input=None):
        return self._out, b""

    def kill(self):
        self.killed = True


def fake_exec(outputs):
    """Stand-in for asyncio.create_subprocess_exec keyed on argv[0]."""
    calls = []

    async def _exec(*argv, **kwargs):
        calls.append(argv)
        out = outputs[argv[0] if argv[0] == "ollama" else "claude"]
        if isinstance(out, Exception):
            raise out
        return FakeProc(out)
    _exec.calls = calls
    return _exec


@pytest.mark.parametrize("raw", [
    b'{"command": "ls", "description": "list"}',
    b'```json\n{"command": "ls", "description": "list"}\n```',
    b'Sure! Here you go: {"command": "ls", "description": "list"} hope that helps',
], ids=["plain", "fenced", "chatty"])
def test_interpret_with_claude_parses(ex, monkeypatch, raw):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec({"claude": raw}))
    assert run(ex.interpret_with_claude("list files")) == {"command": "ls", "description": "list"}


def test_interpret_with_claude_unparseable_raises(ex, monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec({"claude": b"no json here"}))
    with pytest.raises(ValueError):
        run(ex.interpret_with_claude("list files"))


def test_interpret_with_ollama_unparseable_degrades(ex, monkeypatch):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec({"ollama": b"gibberish"}))
    out = run(ex.interpret_with_ollama("list files"))
    assert out["command"] is None
    assert "gibberish" in out["description"]


def test_execute_falls_back_to_ollama(mac_ex, monkeypatch):
    fake = fake_exec({"claude": FileNotFoundError("no claude"),
                      "ollama": b'{"command": null, "description": "hi there"}'})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    out = run(mac_ex.execute_command("how are you"))
    assert out["ai_mode"] == "local"
    assert out["command"] is None
    assert out["output"] == "hi there"


def test_execute_both_llms_fail(mac_ex, monkeypatch):
    fake = fake_exec({"claude": FileNotFoundError("no claude"),
                      "ollama": FileNotFoundError("no ollama")})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    out = run(mac_ex.execute_command("anything"))
    assert out["ai_mode"] == "error"
    assert "Both Claude and Ollama failed" in out["output"]


def test_execute_blocks_dangerous_llm_output(ex, monkeypatch):
    fake = fake_exec({"claude": b'{"command": "rm -rf /", "description": "oops"}'})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    async def never(*a, **k):
        raise AssertionError("a blocked command reached the shell")
    monkeypatch.setattr(asyncio, "create_subprocess_shell", never)
    out = run(ex.execute_command("clean up my disk"))
    assert out["output"].startswith("BLOCKED")


@pytest.mark.skipif(sys.platform == "win32", reason="runs /bin/sh")
def test_execute_runs_llm_command(mac_ex, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = fake_exec({"claude": b'{"command": "echo hi-from-test", "description": "say hi"}'})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    out = run(mac_ex.execute_command("say hi"))
    assert out["ai_mode"] == "cloud"
    assert out["command"] == "echo hi-from-test"
    assert out["output"] == "hi-from-test"


# ── RAW: prefix (automations that already know the command) ───────────────

@pytest.mark.skipif(sys.platform == "win32", reason="runs /bin/sh")
def test_raw_runs_without_llm(mac_ex, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("RAW: must not call an LLM")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", never)
    out = run(mac_ex.execute_command("RAW: echo raw-ok"))
    assert out["ai_mode"] == "raw"
    assert out["output"].strip() == "raw-ok"


def test_raw_still_blocks(ex, monkeypatch):
    async def never(*a, **k):
        raise AssertionError("a blocked command reached the shell")
    monkeypatch.setattr(asyncio, "create_subprocess_shell", never)
    out = run(ex.execute_command("RAW: reboot"))
    assert out["output"].startswith("BLOCKED")


@pytest.mark.parametrize("cmd", [":(){ :|:& };:", ":(){ :|: & };:", ":() { : | : & } ; :"])
def test_fork_bomb_blocked(ex, cmd):
    assert not ex.is_command_safe(cmd)
