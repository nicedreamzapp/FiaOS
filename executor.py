"""FiaOS Command Executor — NL → shell command interpreter with Claude/Ollama."""

import asyncio
import json
import os
import re
import shlex
import subprocess

import anthropic

# Commands that are always blocked
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/\s*$",        # rm -rf /
    r"\brm\s+-rf\s+/\*",           # rm -rf /*
    r"\brm\s+-rf\s+~\s*$",         # rm -rf ~
    r"\bmkfs\b",                    # format disk
    r"\bdd\s+if=.+of=/dev/",       # dd to disk device
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",  # fork bomb
    r"\bshutdown\b",               # shutdown
    r"\breboot\b",                  # reboot
    r"\bhalt\b",                    # halt
    r"\bsudo\s+rm\s+-rf",          # sudo rm -rf
    r"\bnewfs\b",                   # newfs (macOS format)
    r"\bdiskutil\s+eraseDisk",     # erase disk
    # Protect FiaOS services from being killed remotely
    r"\blaunchctl\s+(unload|remove|stop).*fiaos",  # can't unload FiaOS services
    r"\bpkill.*server\.py",        # can't kill FiaOS server
    r"\bkill.*server\.py",         # can't kill FiaOS server
    r"\bpkill.*fiaos",             # can't kill FiaOS
    r"\bpkill.*personaplex",       # managed by FiaOS, not user
    r"\blaunchctl\s+(unload|remove|stop).*caffeinate",  # keep display awake
]

SYSTEM_PROMPT = """You are a macOS command translator. The user gives you natural language instructions.
You respond with ONLY a JSON object — no markdown, no explanation.

Format:
{"command": "the shell command to run", "description": "one-line description of what this does"}

Rules:
- Output valid macOS (zsh/bash) commands
- For "open" requests, use the `open` command (e.g., `open -a Safari`)
- For file listing, use `ls`
- For system info, use appropriate macOS commands (sysctl, df, top, etc.)
- If the request is a question that doesn't need a command (like "how are you"), respond with:
  {"command": null, "description": "your helpful answer here"}
- NEVER output dangerous commands (rm -rf /, shutdown, reboot, mkfs, dd to devices)
- Current working directory is the user's home: ~
- The user's projects are in ~/Desktop/PROJECTS/
- Keep commands simple and safe
"""

OLLAMA_MODEL = "qwen3-coder"


def _strip_quoted_strings(cmd: str) -> str:
    """Replace contents of double- and single-quoted strings with empty quotes,
    so the safety regex matches command-position tokens, not data inside args.
    Without this, words like 'shutdown' appearing inside `claude -p "..."` prompt
    text would trip false positives even though they're never executed."""
    no = re.sub(r'"(?:\\.|[^"\\])*"', '""', cmd)
    no = re.sub(r"'[^']*'", "''", no)
    return no


def is_command_safe(cmd: str) -> bool:
    """Check if a command is safe to execute. Scans command-position tokens only;
    contents of quoted string args are ignored (they're never executed as code)."""
    scan_target = _strip_quoted_strings(cmd)
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, scan_target):
            return False
    return True


# Claude Code's binary. Found on PATH so this works wherever npm/installer put
# it; the ~/.local/bin fallback covers a login shell PATH that launchd didn't get.
import shutil
CLAUDE_BIN = (shutil.which("claude")
              or os.path.expanduser("~/.local/bin/claude"))


async def interpret_with_claude(user_input: str) -> dict:
    """Use local Claude Code (Matt's Max plan, no API cost) to interpret natural
    language into a JSON command. Runs `claude -p` as a subprocess on this mini —
    keychain-unlocked under the GUI login that started FiaOS, so OAuth works."""
    prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_input}\n\nRespond with ONLY the JSON object:"
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    text = stdout.decode(errors="replace").strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[^}]+\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


async def interpret_with_ollama(user_input: str) -> dict:
    """Fallback: use local Ollama to interpret commands."""
    proc = await asyncio.create_subprocess_exec(
        "ollama", "run", OLLAMA_MODEL,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    prompt = f"{SYSTEM_PROMPT}\n\nUser: {user_input}\n\nRespond with ONLY the JSON object:"
    stdout, _ = await proc.communicate(prompt.encode())
    text = stdout.decode().strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    # Try to extract JSON from response
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        match = re.search(r'\{[^}]+\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {"command": None, "description": f"Ollama couldn't parse: {text[:200]}"}


async def execute_command(user_input: str) -> dict:
    """Main entry: interpret NL input and optionally execute."""
    # RAW: prefix bypasses LLM interpretation. Used by automations like the
    # morning briefing that already know the exact shell command they need to
    # run, so they don't depend on Claude/Ollama being reachable.
    if user_input.startswith("RAW:"):
        cmd = user_input[4:].strip()
        if not is_command_safe(cmd):
            return {"input": user_input, "command": cmd,
                    "output": "BLOCKED: This command was flagged as potentially dangerous.",
                    "ai_mode": "raw"}
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
            return {"input": user_input, "command": cmd,
                    "output": stdout.decode(errors="replace"),
                    "ai_mode": "raw"}
        except Exception as e:
            return {"input": user_input, "command": cmd,
                    "output": f"raw exec failed: {e}",
                    "ai_mode": "raw"}

    ai_mode = "cloud"

    # Try Claude first, fall back to Ollama
    try:
        result = await interpret_with_claude(user_input)
    except Exception:
        ai_mode = "local"
        try:
            result = await interpret_with_ollama(user_input)
        except Exception as e:
            return {
                "input": user_input,
                "command": None,
                "output": f"Both Claude and Ollama failed: {e}",
                "ai_mode": "error",
            }

    cmd = result.get("command")
    description = result.get("description", "")

    if cmd is None:
        return {
            "input": user_input,
            "command": None,
            "output": description,
            "ai_mode": ai_mode,
        }

    # Safety check
    if not is_command_safe(cmd):
        return {
            "input": user_input,
            "command": cmd,
            "output": "BLOCKED: This command was flagged as potentially dangerous.",
            "ai_mode": ai_mode,
        }

    # Execute the command
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.expanduser("~"),
            env={**os.environ, "HOME": os.path.expanduser("~")},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=1800)
        output = stdout.decode() if stdout else ""
        if stderr:
            output += "\n" + stderr.decode()
        output = output.strip()
        if not output:
            output = f"Command executed successfully (exit code {proc.returncode})"
    except asyncio.TimeoutError:
        output = "Command timed out after 30 minutes"
    except Exception as e:
        output = f"Execution error: {e}"

    return {
        "input": user_input,
        "command": cmd,
        "description": description,
        "output": output,
        "ai_mode": ai_mode,
    }
