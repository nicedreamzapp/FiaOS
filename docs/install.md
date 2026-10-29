---
title: Install FiaOS on macOS or Windows
description: How to install FiaOS, the self-hosted browser remote desktop and terminal, on an Apple Silicon Mac or a Windows 10/11 PC, including the exact Python packages each server imports.
---

# Install

FiaOS has two servers that share the same routes, UI and session format:

- `server.py` at the repo root, for macOS
- `windows/server.py`, for Windows 10/11

Both listen on port **9000** on all interfaces (`0.0.0.0`). On macOS set `FIAOS_PORT` to use another port; the Windows server still hard-codes `PORT = 9000`.

## macOS

**Requires:** Apple Silicon Mac, macOS 14+, Python 3.12+ (from the README; TODO-VERIFY whether Intel Macs or older macOS work, nothing in the code checks for them).

The quick way, which does every step below plus the password file and a LaunchAgent:

```bash
curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/install.sh | bash
```

Remove it with `curl -fsSL https://raw.githubusercontent.com/nicedreamzapp/FiaOS/main/uninstall.sh | bash` (add `-s -- --purge` to delete the checkout and `~/.config/fiaos/fiaos.env` too). The manual steps:

```bash
git clone https://github.com/nicedreamzapp/FiaOS.git
cd FiaOS
python3.12 -m venv .venv
.venv/bin/pip install aiohttp psutil numpy anthropic pillow pyobjc-framework-Quartz
```

The venv must be at `FiaOS/.venv`. The server launches its screen worker, input driver and mouse helper with the hard-coded path `.venv/bin/python3` next to `server.py`.

!!! warning "Don't use requirements.txt or pyproject.toml"
    Both files are left over from the PersonaPlex voice project. They pull in `mlx`, `rustymimi`, `sounddevice` and other packages the screen and terminal don't use, and they leave out `anthropic`, `pillow` and `pyobjc-framework-Quartz`, which the server does import. Install the list above instead.

Why each package is needed, from the imports:

| Package | Imported by |
|---|---|
| `aiohttp` | `server.py` (web server, WebSockets) |
| `psutil` | `server.py` (status, processes) |
| `anthropic` | `executor.py` imports it at the top, so the server won't start without it, even though the helper actually calls the `claude` CLI |
| `numpy`, `pillow` | `screen_worker.py` (tiling and JPEG encoding) |
| `pyobjc-framework-Quartz` | `screen_worker.py`, `input_helper.py`, `screencast.py` |

### macOS permissions

- **Screen Recording.** Give it to the terminal app you start FiaOS from, or to the venv's Python if it runs as a LaunchAgent: System Settings > Privacy & Security > Screen Recording. Without it the screenshot route returns an error instead of your desktop.
- **Accessibility.** Mouse and keyboard events are posted with Quartz `CGEventPost`. TODO-VERIFY: macOS usually asks for Accessibility permission before it accepts synthetic input, but the README and code don't mention this. The `/api/keyboard` route also drives `System Events` through `osascript`, which normally triggers an Automation prompt.

## Windows

**Requires:** Windows 10/11, Python 3.12+. The server must run in the **interactive desktop session** of a logged-in user. Screen capture (GDI `BitBlt`) and `SendInput` don't work from a Windows service.

```powershell
git clone https://github.com/nicedreamzapp/FiaOS.git
cd FiaOS\windows
Copy-Item -Recurse ..\static .\static   # windows\server.py serves windows\static, which isn't committed
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-windows.txt
```

`windows/requirements-windows.txt` lists `aiohttp>=3.10.5,<3.11`, `numpy>=2.1.0`, `Pillow>=10`, `psutil>=6.0.0` and `pywinpty>=2.0.13`. The Windows `executor.py` doesn't import `anthropic`, so it isn't needed there.

The Windows server looks for its venv at `windows\.venv\Scripts\python.exe`. If that file doesn't exist it falls back to whatever Python is running the server (`sys.executable`).

Then create `C:\ProgramData\fia\fiaos.env`:

```ini
FIAOS_PASSWORD=your-strong-password
FIAOS_MACHINE=pc
```

`windows/run_fiaos.ps1` reads this file. Only lines matching `NAME=value`, where the name is upper-case letters and underscores, are loaded. See [Configuration](configuration.md) for every variable.

## Optional: Claude Code and Ollama

You don't need either one for the screen or terminal. They are only used by the natural-language command helper (`POST /api/command`):

- The helper runs the [Claude Code](https://claude.com/claude-code) CLI as `claude -p`. On macOS it finds `claude` on `PATH`, or falls back to `~/.local/bin/claude`. On Windows it also tries `claude.cmd`, `%APPDATA%\npm\claude.cmd`, `~\.local\bin\claude.exe` and `~\AppData\Local\Programs\claude\claude.exe`.
- If the Claude call fails, it falls back to a local [Ollama](https://ollama.com/) model, `qwen3-coder`.

You can of course run `claude` interactively in the Terminal tab. That is just a normal shell session.

## Next

Go to the [Quickstart](quickstart.md) to set the password and open the page.
