---
title: How FiaOS works - tile streaming, PTY terminal, signed sessions
description: FiaOS architecture read from the code. How the live screen streams only changed JPEG tiles, how the browser terminal gets a real PTY or ConPTY, how signed tokens give one login across machines, and how cookie routing picks a machine.
---

# How it works

FiaOS is a single aiohttp application per machine plus a static single-page UI. There's no database and no build step. This page walks through the main parts, with file names so you can read the code yourself.

```text
browser (static/index.html, xterm.js)
   │  HTTPS                                  optional, for remote access
   ▼
nginx on a VPS ── cookie fia_target ──► reverse SSH tunnel per machine
   │                                          │
   ▼                                          ▼
server.py (macOS) / windows/server.py  on port 9000
   ├── /api/terminal  → zsh in a PTY  |  PowerShell in a ConPTY
   ├── /api/screen    → screen_worker.py (capture, tile, hash, JPEG)
   │                    input_helper.py (Quartz CGEvent | Win32 SendInput)
   ├── /api/command   → executor.py → claude -p, else ollama qwen3-coder
   └── auth           → HMAC-signed tokens, .sessions.json, .revoked.json
```

## The live screen

Files: `screencast.py`, `screen_worker.py`, `input_helper.py` (and the same names under `windows/`).

1. The browser opens the `/api/screen` WebSocket with a target width, fps and JPEG quality (see [Configuration](configuration.md#live-screen-parameters)).
2. The server replies with an `info` message: the screen's pixel size and the tile grid (`step`, `w`, `h`, `cols`, `rows`, `tile`).
3. A **capture worker** process grabs the display. On macOS that's `CGDisplayCreateImage` through PyObjC Quartz. On Windows it's GDI `BitBlt` into a reused DIB section. The worker shrinks the frame by an integer step, which costs nothing (a code comment says a real resize cost 53 ms a frame), and cuts it into 128×128 tiles.
4. Each tile is hashed with BLAKE2b. Only tiles whose hash changed since the last pass are JPEG-encoded and written to stdout as `2-byte index + 4-byte length + JPEG`.
5. The server forwards each tile to the browser as `2-byte index + JPEG`. The page draws it into a canvas at `(index % cols, index / cols) × 128`.

A still desktop costs one capture per frame interval and sends nothing.

**Input** goes the other way on the same socket. The browser sends JSON events (`move`, `down`, `up`, `drag`, `click`, `dblclick`, `scroll`, `text`, `key`, `combo`) with coordinates as fractions of the picture. `screencast.encode_event` accepts only those types and converts fractions to screen coordinates. A long-running `input_helper.py` process turns each line into a real event: Quartz `CGEventPost` on macOS, `SendInput` on Windows. Keeping that process alive matters. A code comment says the older design started a new Python process per click and "made dragging impossible".

**Pixels vs points.** On a Retina Mac the capture is in pixels but mouse events are in points. `screencast.screen_size()` reads pixel size from the display mode, and `screen_points()` reads point size from `CGDisplayBounds`. On Windows every screen process calls `SetProcessDpiAwareness(2)` so both spaces are physical pixels, and `_preflight()` refuses to start if the capture size and the reported screen size disagree.

**Idle costs nothing.** When the last viewer disconnects, the worker isn't killed. It's "parked" for 90 seconds (`WARM_HOLD`) and sent a `P`, after which it blocks and stops capturing. If the same viewer settings reconnect within 90 seconds, the worker is reused and sent `R` to resend every tile. That matters for phones, which drop the socket every time the tab goes to the background. Workers also exit when their parent server goes away, and on startup `_sweep_strays()` kills any `screen_worker.py` / `input_helper.py` left over from a previous run.

While someone is watching, the display is kept awake: `caffeinate -d -u` for the life of the socket on macOS, `SetThreadExecutionState` (reference-counted across viewers) on Windows.

There's also a simpler polled route, `GET /api/screenshot`, which returns one JPEG and answers `204 No Content` when the frame hash matches `?last=`.

## The terminal

Files: `server.py` (`handle_terminal_ws`), `windows/server.py`, `static/index.html`.

- **macOS:** `pty.openpty()`, then `/bin/zsh -l -i` with the slave end as stdin/stdout/stderr, in a new session (`os.setsid`). The master fd is set non-blocking and watched with `loop.add_reader`. Output goes to the browser as binary WebSocket frames.
- **Windows:** `winpty.PtyProcess.spawn(["powershell.exe", "-NoLogo", "-NoExit"])` (ConPTY via pywinpty). pywinpty's read blocks, so reads run in a thread executor.

The browser sends JSON: `{"type":"input","data":...}` for keystrokes and `{"type":"resize","rows":..,"cols":..}` on resize. On macOS resize maps to `TIOCSWINSZ`, on Windows to `setwinsize`. Plain text that isn't JSON is treated as "legacy line mode": it's checked against a short list of patterns that would kill FiaOS itself (for example `pkill ... server.py`, `launchctl unload ... fiaos`, `taskkill ... python`), then run with a newline appended.

When the socket closes, the shell's process group gets `SIGHUP` (macOS) or the ConPTY process is terminated (Windows).

## Login and sessions

Files: `server.py` top section, `tests/test_auth.py`.

- A token is `v1.<expiry>.<hex HMAC-SHA256 of "v1.<expiry>" keyed with FIAOS_PASSWORD>`.
- Every machine has the same password, so a token minted on one verifies on all the others **without the machines talking to each other**. That's how the machine tabs switch without a second login.
- `valid_session()` checks, in order: the token is in `.revoked.json` (rejected), it's in this machine's `.sessions.json` and not expired (accepted), it's a well-formed `v1` token with a valid signature and future expiry (accepted).
- Logout writes the token to `.revoked.json` until its expiry. This list is per machine.
- Failed logins are rate-limited per client IP (10 per 5 minutes). Behind the SSH tunnel every request comes from `127.0.0.1`, so `client_ip()` uses `CF-Connecting-IP` or `X-Real-IP`, but only when the TCP peer is loopback.
- Password comparison uses `hmac.compare_digest`.

## Multi-machine routing

There are two ways a machine tab reaches its machine. They can be used together.

### 1. nginx on a VPS (`deploy/nginx-fia.conf`)

Each machine runs FiaOS on local port 9000 and opens a reverse SSH tunnel to the VPS on its own port. nginx maps the `fia_target` cookie to an upstream:

| Cookie | Upstream | VPS port |
|---|---|---|
| (none) / `mini` | `fia_mini` | 9000 |
| `m5` | `fia_m5` | 9010 |
| `pc` | `fia_pc` | 9020 |

- `/machines/probe/<key>` locations proxy to that machine's `/login`, with a 3 s connect and 5 s read timeout. The UI turns the result into a green or red dot.
- Page and static locations use `proxy_connect_timeout 5s` and `proxy_read_timeout 15s`, and send 502/504 to `@fia_fallback`. That location serves the mini and sets `fia_target=mini`, so a stale cookie for a machine that's off doesn't leave you on an error page. The `$fia_fallback_ok` map turns this off per machine. **In the shipped file it's off (`0`) for `pc`.**
- `/api/terminal` and `/api/screen` get WebSocket upgrade headers and 24-hour timeouts. `/api/command` gets 30-minute timeouts with buffering off. Other `/api/` calls get 600 s.
- Static assets are cached on the VPS with the upstream name in the cache key, so one machine's files are never served for another.

**Why the tunnel scripts look the way they do.** From the commit history: when a laptop sleeps, its SSH client dies without closing the connection. By default sshd never notices, keeps the forwarded port open, and nginx connects and then hangs. `deploy/sshd-tunnel-keepalive.conf` sets `ClientAliveInterval 30` / `ClientAliveCountMax 3`, so a dead client is dropped in about 90 s. `deploy/tunnel_with_stale_port_clear.sh` first asks the VPS whether its port is actually serving `/login`. If it is, another tunnel is live and the script exits. If not, it runs `fuser -k` on the port and reconnects right away with `ExitOnForwardFailure=yes`.

### 2. The Windows server's built-in proxy

Both servers install a `machine_proxy` middleware. If a request carries `fia_target=<key>` for a different machine, it proxies the whole request (WebSockets included) to the first reachable address for that key from `FIAOS_PEERS`, minus the `fia_target` cookie. `/login`, `/api/login`, `/logout`, `/machines/` and `/static/` are always served locally. If the target can't be reached, the request is served locally.

The README reports one measured number for this path on the reference setup: 0.57 ms over a Thunderbolt bridge vs. 63 ms average (4 to 113 ms) over Wi-Fi. That is the author's measurement, not a benchmark in this repo.

## The natural-language helper

Files: `executor.py`, `windows/executor.py`.

`POST /api/command` with `{"command": "show disk usage"}`:

1. If the text starts with `RAW:`, the rest is run as a shell command directly, after the safety check.
2. Otherwise it asks Claude Code (`claude -p "<system prompt> User: ..."`) for a JSON object `{"command": ..., "description": ...}`. If that fails, it asks `ollama run qwen3-coder`.
3. The proposed command goes through `is_command_safe()`. That function first blanks out quoted strings, so words inside arguments don't trigger it, then matches the blocked patterns.
4. The command runs in your home directory with a 30-minute timeout. The result includes `ai_mode`: `cloud` (Claude), `local` (Ollama), `raw` or `error`.

The macOS system prompt mentions `~/Desktop/PROJECTS/` as the user's project folder. The Windows prompt says the working directory is `C:\Users\matt`. Both are specific to the author's machines; edit them for yours.

## Windows-specific behavior

| macOS | Windows |
|---|---|
| `pty` + `zsh -l -i` | ConPTY via pywinpty + PowerShell |
| Quartz `CGEvent` | Win32 `SendInput`, DPI-aware |
| `screencapture` / `CGDisplayCreateImage` | GDI `BitBlt` |
| `caffeinate` | `SetThreadExecutionState` |
| `pbcopy` / `pbpaste` | `Get-Clipboard` / `Set-Clipboard` via PowerShell, UTF-8 pinned, text sent on stdin |
| `osascript` volume | Media keys only; `GET /api/volume` returns `null` |
| `pmset sleepnow` | Refused (409): "Sleep is disabled on the PC - it is a headless server." |
| none | `/api/vnc` raw RFB bridge to `127.0.0.1:5900` for a noVNC client |
