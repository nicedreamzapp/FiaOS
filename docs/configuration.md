---
title: FiaOS configuration - environment variables, ports, and API reference
description: Every FiaOS setting read from the source code. FIAOS_PASSWORD, FIAOS_MACHINE, FIAOS_PEERS, tunnel variables, hard-coded limits, cookies, state files, and the full HTTP and WebSocket route list.
---

# Configuration

Everything on this page comes from the source. FiaOS has no config file and no command-line flags. Settings are environment variables plus constants at the top of `server.py` and `windows/server.py`. Where the code's behavior is unclear, it is marked **TODO-VERIFY**.

## Server environment variables

| Variable | Used by | Default | What it does |
|---|---|---|---|
| `FIAOS_PASSWORD` | `server.py`, `windows/server.py` | none, **required** | The login password. Also the HMAC key that signs session tokens. If it's unset, the server exits at import time. Every machine in a fleet must use the same value. Changing it invalidates every session on every machine. |
| `FIAOS_MACHINE` | both servers | `mini` | Which machine this copy is: `mini`, `m5` or `pc`. On both servers it decides which entry in the peer table is "this machine" (that entry is never proxied to) and what `/machines/probe/<key>` reports as local. |
| `FIAOS_PORT` | macOS `server.py`, `start.sh`, `watchdog.sh` | `9000` | Port the macOS server listens on. The Windows server ignores it. |
| `FIAOS_PEERS` | both servers | Windows: placeholder table (see below). macOS: empty (no peers, every tab stays local) | LAN addresses of the other machines, for the built-in machine proxy. On macOS put it in `~/.config/fiaos/fiaos.env`. Format: `key=host:port[,host:port...]` entries separated by `;`. |

Example `FIAOS_PEERS`, taken from the comment in `windows/server.py`:

```text
FIAOS_PEERS="mini=10.0.0.5:9000;m5=10.0.0.9:9000,10.0.0.10:9000;pc=10.0.0.12:9000"
```

Several addresses for one machine are tried in order, and the first one that accepts a TCP connection within 1.5 s wins. Reachability is cached for 10 s per address. Put a direct cable link ahead of Wi-Fi. If `FIAOS_PEERS` is empty or can't be parsed, the server uses the built-in placeholder table (`mini=10.0.0.5:9000`, `m5=10.0.0.9:9000,10.0.0.10:9000`, `pc=10.0.0.12:9000`). The code itself says these "are placeholders, not real hosts", so set your own.

!!! note "Where to set them"
    - macOS LaunchAgent: the `EnvironmentVariables` dict in the plist.
    - Windows: `C:\ProgramData\fia\fiaos.env`, which `run_fiaos.ps1` loads. Only lines matching `^\s*([A-Z_]+)=(.*)$` are applied.
    - By hand: prefix the command, for example `FIAOS_PASSWORD=... .venv/bin/python3 server.py`.

### Variables set for the terminal

When it spawns the shell, the server copies its own environment and sets `TERM=xterm-256color` and `COLORTERM=truecolor`. On macOS it also sets `LANG=en_US.UTF-8` if `LANG` isn't already set. The shell starts in your home directory.

## Script-only variables

These are read by helper scripts, not by the servers.

### Deployment and tunnel variables

| Variable | Script | Default | Meaning |
|---|---|---|---|
| `FIA_VPS` | `deploy/tunnel_with_stale_port_clear.sh` | `root@your.vps.here` | SSH destination of the VPS. |
| `FIA_REMOTE_PORT` | same | `9010` | This machine's port on the VPS (its nginx upstream). |
| `FIA_LOCAL_PORT` | same | `9000` | Local FiaOS port. |
| `VPS_IP` | `start.sh`, `watchdog.sh` | empty (tunnel skipped) | Opens `ssh -R 9000:localhost:9000 root@$VPS_IP`. |
| `DOMAIN` | `start.sh` | `localhost:9000` | Only used to print the URL at the end. |
| `FIA_PROMPT` | `start.sh`, `watchdog.sh`, `start_personaplex.sh`, example plist | `You are a helpful local voice assistant. Keep replies short.` | Persona prompt for the removed PersonaPlex voice agent. FiaOS itself doesn't use it. |

### Test variables

| Variable | Script | Meaning |
|---|---|---|
| `FIA_VPS` | `tests/test_infra.sh` | Required. VPS to test against, e.g. `root@your.vps`. |
| `FIA_URL` | `tests/test_infra.sh` | Required. Public URL, e.g. `https://fia.example.com`. |
| `FIA_TUNNEL_SCRIPT` | `tests/test_infra.sh` | Tunnel script to run during a scenario. Default `$HOME/FiaOS/tunnel_m5.sh`, which isn't in the repo. TODO-VERIFY. |

`test_infra.sh` also assumes the reference setup's LaunchAgent labels (`com.fiaos.server`, `com.fiaos.tunnel-contabo`).

## Hard-coded settings

None of these can be changed without editing the source.

| Setting | Value | Where |
|---|---|---|
| Listen address and port | `0.0.0.0:9000` | `PORT`, `web.run_app(...)` in both servers; on Windows `FIAOS_PORT` and `FIAOS_HOST` in the env file override them |
| Session lifetime | 24 hours (`SESSION_EXPIRY = 86400`) | both |
| Login rate limit | 10 failed attempts (`MAX_LOGIN_ATTEMPTS`) per 5 minutes (`LOGIN_WINDOW = 300`) per client IP | both |
| Rate-limit table cap | 4096 IPs (`MAX_IP_BUCKETS`); the 64 oldest are evicted when full | both |
| Login body limit | 4096 bytes; larger returns 413 | `handle_login` |
| Upload limit | 100 MB (`client_max_size`) | `create_app` |
| Session cookie | `fiaos_session`, not HttpOnly; `SameSite=None; Secure` behind HTTPS (`X-Forwarded-Proto: https`), otherwise `SameSite=Lax` | `handle_login` |
| Screenshot downscale | longest side 1600 px (`sips -Z 1600` on macOS, resize to 1600 wide on Windows) | `handle_screenshot` |
| Screen tile size | 128 px | `screencast.py`, `screen_worker.py` |
| Warm worker hold | 90 s (`WARM_HOLD`) | `screencast.py` |
| VNC bridge target (Windows) | `127.0.0.1:5900` | `windows/server.py` |
| NL helper timeout | 1800 s (30 min) for the Claude call and for the command | `executor.py` |
| Ollama fallback model | `qwen3-coder` | `executor.py` |

### Live screen parameters

The browser opens `/api/screen?w=<width>&fps=<fps>&q=<quality>`. The server clamps each value:

| Query | Default | Clamped to |
|---|---|---|
| `w` (streamed width in px) | 1720 | 640 to 2560 |
| `fps` | 10 | 2 to 20 |
| `q` (JPEG quality) | 55 | 20 to 90 |

The shipped UI picks these itself (`static/index.html`). On a LAN hostname (`10.*`, `192.168.*`, `172.16-31.*`, `127.*`, `localhost`) it asks for at least 1920 wide, 15 fps, quality 80. Otherwise it asks for the panel width times device pixel ratio (capped at 2) at 10 fps and quality 72, or 12 fps and quality 58 on touch devices.

The worker shrinks by an integer step (`round(full_width / w)`) rather than resizing, so the actual streamed width can differ from `w`.

## Cookies and browser storage

| Name | Set by | Purpose |
|---|---|---|
| `fiaos_session` | server on login | The signed session token, `v1.<expiry>.<hmac-sha256>`. |
| `fia_target` | machine tabs in the UI; nginx fallback | Which machine to route to (`mini`, `m5`, `pc`). One-year max-age. |
| `localStorage.fiaos_token` | login page | Copy of the token. Used as the terminal WebSocket's `?token=` when the cookie isn't readable. |

The server accepts a token from the cookie or a `?token=` query parameter, and uses whichever one validates.

## State files

Both servers write these next to `server.py`. Writes are atomic: a temp file, then `os.replace`.

| File | Contents |
|---|---|
| `.sessions.json` | Tokens this machine issued, with their expiry. Expired ones are pruned on load and at each login. |
| `.revoked.json` | Tokens killed by logout, kept until they would have expired. **Per machine**: logging out on one machine doesn't revoke the token on another. Rotating `FIAOS_PASSWORD` is the only global revoke. |

Screenshots go to a temp directory created at startup (`fiaos_screenshots_*`).

## Security notes

- The Terminal is a real shell. Anyone with the password has the same power as an SSH login.
- The blocked-command regexes in `executor.py` / `windows/executor.py` (e.g. `rm -rf /`, `mkfs`, `shutdown`, `format C:`, killing FiaOS itself) apply only to the natural-language helper and to the terminal's legacy line mode. They are a guardrail, not a security boundary. Normal keystrokes in the xterm terminal are sent as JSON `input` messages, and those are not filtered.
- File routes (`/api/files*`) only allow paths under the home directory. The check is a plain string prefix match on the resolved path, so TODO-VERIFY: a sibling directory whose name starts with the home path (for example `/Users/alice2` next to `/Users/alice`) would also pass.
- `POST /api/claude_stream` runs `claude --dangerously-skip-permissions -p <prompt>`. Any logged-in user can start an unattended Claude Code run on the machine.
- `client_ip()` trusts `CF-Connecting-IP` and `X-Real-IP` only when the request's TCP peer is loopback, which is the case behind the SSH tunnel and nginx. If you put a different proxy on the same host, make sure it overwrites those headers.

## HTTP and WebSocket routes

All routes except the first five need a valid session and return `401 Not authenticated` without one.

| Method | Path | macOS | Windows | Notes |
|---|---|---|---|---|
| GET | `/login` | yes | yes | Login page; redirects to `/` if already logged in |
| POST | `/api/login` | yes | yes | Body `{"password": "..."}`; returns `{"ok": true, "token": ...}` |
| GET | `/logout` | yes | yes | Revokes the token, clears the cookie, redirects to `/login` |
| GET | `/api/authcheck` | yes | yes | 200 or 401, for nginx `auth_request` |
| GET | `/` | yes | yes | Main UI; redirects to `/login` without a session |
| POST | `/api/command` | yes | yes | Natural-language helper; `RAW:` prefix skips the model (still safety-checked) |
| POST | `/api/claude_stream` | yes | yes | Streams `claude -p --output-format stream-json` as SSE; optional `session_id` / `resume_session_id` |
| GET | `/api/files` | yes | yes | List a directory or stat a file (`?path=`, default `~/Desktop`) |
| GET | `/api/files/download` | yes | yes | `?path=` |
| POST | `/api/files/upload` | yes | yes | Multipart `file`, optional `dest` (default `~/Desktop`) |
| POST | `/api/files/delete` | yes | yes | Deletes a file or a whole directory tree (`shutil.rmtree`) |
| POST | `/api/files/move` | yes | yes | `src`, `dst` |
| GET | `/api/status` | yes | yes | CPU, RAM, disk, battery, network |
| GET | `/api/processes` | yes | yes | Top 50 by memory (over 0.1%) |
| POST | `/api/processes/kill` | yes | yes | `pid`; sends terminate |
| POST | `/api/system` | yes | yes | `sleep`, `lock`, `brightness_up`, `brightness_down`. Windows refuses `sleep` with 409. |
| GET | `/api/screenshot` | yes | yes | One JPEG; `?quality=`, `?last=<hash>` returns 204 if unchanged |
| POST | `/api/mouse` | yes | yes | `click`, `doubleclick`, `rightclick`, `move`, `scroll` |
| POST | `/api/keyboard` | yes | yes | `type`, `keystroke`, `hotkey` |
| GET/POST | `/api/clipboard` | yes | yes | |
| GET/POST | `/api/volume` | yes | yes | Windows: GET returns `null`s; POST steps media keys |
| GET | `/api/apps` | yes | yes | macOS `.app` bundles; Windows Start Menu `.lnk`/`.url` |
| POST | `/api/apps/open`, `/api/apps/quit` | yes | yes | |
| POST | `/api/notification` | yes | yes | macOS notification / Windows balloon tip |
| WS | `/api/screen` | yes | yes | Live tiles down, input events up |
| WS | `/api/terminal` | yes | yes | PTY |
| WS | `/api/vnc` | no | yes | Raw RFB bridge to `127.0.0.1:5900` |
| GET | `/vnc`, `/vnc/` | no | yes | Redirects to `/static/novnc/vnc.html` (noVNC isn't bundled) |
| GET | `/machines/probe/{key}` | no | yes | `{"up": true, "via": ...}` or 503. On a Mac-only setup, nginx answers this path instead. |
| GET | `/static/*` | yes | yes | |

The shipped UI only uses login, logout, `/api/status`, `/api/screen`, `/api/terminal` and `/machines/probe/*`. The other routes are there for scripts and automations.
