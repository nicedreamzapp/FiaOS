"""FiaOS Command Executor — NL → shell command interpreter with Claude/Ollama."""

import asyncio
import json
import os
import re
import shlex
import subprocess

# Commands that are always blocked
BLOCKED_PATTERNS = [
    r"\brm\s+-rf\s+/\s*$",        # rm -rf /
    r"\brm\s+-rf\s+/\*",           # rm -rf /*
    r"\brm\s+-rf\s+~\s*$",         # rm -rf ~
    r"\bmkfs\b",                    # format disk
    r"\bdd\s+if=.+of=/dev/",       # dd to disk device
    r"\b:(){ :\|:& };:",           # fork bomb
    r"\bshutdown\b",               # shutdown
    r"\breboot\b",                  # reboot
    r"\bhalt\b",                    # halt
    r"\bsudo\s+rm\s+-rf",          # sudo rm -rf
    r"\bnewfs\b",                   # newfs (macOS format)
    r"\bdiskutil\s+eraseDisk",     # erase disk
    # Windows spellings of the same disasters
    r"\bformat\s+[a-zA-Z]:",       # format a drive
    # These were written with \\\\ -- a regex for TWO literal backslashes, which no
    # real command has, so they never matched. Rewritten to fire on the actual
    # forms, and scoped to absolute/home paths so a local `Remove-Item .\build
    # -Recurse` still works.
    r"(?i)\bRemove-Item\b(?=.*(?:-Recurse|-r\b))(?=.*(?:[A-Za-z]:\\|\\Users\b|\$env:USERPROFILE|\$HOME|~))",
    r"(?i)\br(?:d|mdir)\s+/s\b",     # rd /s %USERPROFILE%
    r"(?i)\bdel\s+/[sq]",             # del /s /q %USERPROFILE%\*
    r"(?i)\bcipher\s+/w",             # wipe free space
    r"Stop-Computer|Restart-Computer",
    r"\bshutdown(\.exe)?\s+/",     # shutdown /s /r
    # Protect FiaOS services from being killed remotely
    r"\blaunchctl\s+(unload|remove|stop).*fiaos",  # can't unload FiaOS services
    r"\bpkill.*server\.py",        # can't kill FiaOS server
    r"\bkill.*server\.py",         # can't kill FiaOS server
    r"\bpkill.*fiaos",             # can't kill FiaOS
    r"\bpkill.*personaplex",       # managed by FiaOS, not user
    r"Stop-Process.*(python|server\.py|fiaos)",
    r"taskkill.*(python|fiaos)",
    r"(Stop|Unregister)-ScheduledTask.*Fia",
    r"schtasks.*/(end|delete).*Fia",
]

SYSTEM_PROMPT = """You are a Windows command translator. The user gives you natural language instructions.
You respond with ONLY a JSON object — no markdown, no explanation.

Format:
{"command": "the shell command to run", "description": "one-line description of what this does"}

Rules:
- Output valid Windows PowerShell commands (they run via PowerShell)
- For "open" requests, use `start` (e.g., `start brave`, `start notepad`)
- For file listing, use `dir` or `Get-ChildItem`
- For system info, use PowerShell cmdlets (Get-Process, Get-CimInstance, etc.)
- If the request is a question that doesn't need a command (like "how are you"), respond with:
  {"command": null, "description": "your helpful answer here"}
- NEVER output dangerous commands (Remove-Item -Recurse on system paths, format, shutdown, restart)
- Current working directory is the user's home: C:\\Users\\matt
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


import shutil

CLAUDE_BIN = (shutil.which("claude") or shutil.which("claude.cmd")
              or os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"))


async def interpret_with_claude(user_input: str) -> dict:
    """Use local Claude Code (Matt's Max plan, no API cost) to interpret natural
    language into a JSON command. Runs `claude -p` as a subprocess."""
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
