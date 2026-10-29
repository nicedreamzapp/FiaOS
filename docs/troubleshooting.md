---
title: FiaOS troubleshooting - error messages and fixes
description: Fixes for common FiaOS problems, built from the actual error messages in the code. Server won't start, screen shows an error or black, login says too many attempts, machine tab red, tunnel won't reconnect, Windows preflight failed.
---

# Troubleshooting

The repository has no GitHub issues filed yet, so this page is built from the error messages and failure handling in the code and from the fixes described in the commit history. Search this page for the exact text you see.

## The server won't start

### `FIAOS_PASSWORD env var is not set` / `FIAOS_PASSWORD is not set`

There's no default password. Set one:

```bash
FIAOS_PASSWORD='your-strong-password' .venv/bin/python3 server.py
```

On Windows, put `FIAOS_PASSWORD=...` in `C:\ProgramData\fia\fiaos.env` and start with `run_fiaos.ps1`.

!!! note "macOS: you may see `NameError: name 'sys' is not defined` instead"
    In `server.py`, `_abort_no_password()` calls `sys.exit(...)`, but `server.py` never imports `sys`. With no password set, the server still refuses to start, but you get a `NameError` traceback instead of the friendly message. The fix is the same: set `FIAOS_PASSWORD`. TODO-VERIFY: confirm on a Mac. This is read from the code, not observed.

### `ModuleNotFoundError: No module named 'anthropic'` (or `Quartz`, `PIL`, `numpy`)

You probably installed `requirements.txt`, which is the old voice project's list. Install what the macOS server actually imports:

```bash
.venv/bin/pip install aiohttp psutil numpy anthropic pillow pyobjc-framework-Quartz
```

### Address already in use (port 9000)

The port defaults to 9000 (on macOS, `FIAOS_PORT` changes it). Something else, often an older FiaOS, is already listening. On macOS, `lsof -i :9000` shows what. If a LaunchAgent with `KeepAlive` is running, it restarts the server whenever you kill it. Unload it first with `launchctl unload ~/Library/LaunchAgents/com.fiaos.server.plist`.

## Windows preflight failures

`windows/server.py` checks a few things before serving and exits with a message if any fail.

### `[FiaOS] preflight: screen capture returned nothing`

The capture got no image. The usual cause is that the server isn't running in the logged-in interactive desktop session (a service, or a scheduled task set to run whether the user is logged in or not). Run `run_fiaos.ps1` from an at-logon task for your user.

### `preflight FAILED: screen_size() says WxH but the capture is WxH`

The tile grid wouldn't match the captured image, so the picture would look doubled or scrambled. The code calls this "the DPI-awareness trap". Every process calls `SetProcessDpiAwareness(2)`. If something forces a different DPI mode (a compatibility setting on `python.exe`, for example), clear it. TODO-VERIFY which Windows settings can cause this in practice.

### `preflight FAILED: FIAOS_MACHINE=... but TARGETS[...] contains this machine's own address`

Requests would proxy to themselves forever. Set `FIAOS_MACHINE` to this machine's key (`pc` for the PC) and make sure `FIAOS_PEERS` doesn't list this machine's own address under a different key.

!!! note "TODO-VERIFY"
    `TARGETS` always empties the entry for `FIAOS_MACHINE` before this check runs, so as written the check may never fire. If you leave `FIAOS_MACHINE` at its default `mini` on the PC and `FIAOS_PEERS` lists the PC's own IP under `pc`, requests with `fia_target=pc` could still loop. Set `FIAOS_MACHINE` correctly either way.

## Screen view problems

### `Screen Recording permission required. Go to System Settings > Privacy & Security > Screen Recording and enable Terminal (or Python).`

That's the macOS `/api/screenshot` route (HTTP 403). Give Screen Recording permission to whatever launches the server: your terminal app if you run it by hand, or the venv's `python3` if you use a LaunchAgent. Then restart FiaOS.

### The Screen tab stays on "Reconnecting…"

The UI shows this whenever the `/api/screen` WebSocket closes and retries every 1.5 s. Things to check:

- **macOS:** the capture worker exits with `no screen` if `CGDisplayCreateImage` returns nothing, which usually means no Screen Recording permission (see above).
- **The venv path.** The macOS server starts the worker with `FiaOS/.venv/bin/python3`, hard-coded. If your venv lives somewhere else, the worker can't start.
- **Behind nginx:** make sure the `/api/screen` location has the `Upgrade` / `Connection "upgrade"` headers, as in `deploy/nginx-fia.conf`.

### `Screen capture failed.` (Windows)

`/api/screenshot` returns this (HTTP 500) when the BitBlt grab or JPEG save fails. The underlying error is printed to the log as `[FiaOS] screenshot failed: ...`. See `C:\ProgramData\fia\fiaos.log`.

### Clicks land in the wrong place, or the picture looks doubled

This was a real bug on a Retina Mac. The grid was sized in points while the worker tiled pixels. The current code sizes the grid from the display mode's pixel size. If you still see it, file an issue with your display resolution and scaling.

### Mouse or keyboard does nothing (macOS)

Input is posted with Quartz `CGEventPost`. macOS may silently drop synthetic events until the process has **Accessibility** permission. TODO-VERIFY: the repo doesn't document this; check System Settings > Privacy & Security > Accessibility. Errors from `input_helper.py` go to its stderr, which the server discards.

### `Mouse control failed: ...`

That's the `/api/mouse` route. On macOS it runs a small Quartz script with `FiaOS/.venv/bin/python3`, and the message is that script's stderr. A missing `pyobjc-framework-Quartz` in the venv is the usual cause.

## Login problems

### `Too many attempts. Try again later.` (HTTP 429)

10 failed logins from one client IP within 5 minutes. Wait 5 minutes. Behind the reference nginx setup each client is limited separately. If everyone is getting locked out together, your proxy isn't setting `X-Real-IP`, or the request isn't arriving from loopback, so all clients share one bucket.

### `Wrong password` (HTTP 401)

The password must match `FIAOS_PASSWORD` byte for byte. Look for trailing spaces or quotes in your plist or `fiaos.env`.

### `Bad request` (HTTP 400 or 413)

The login body wasn't valid JSON, or was over 4096 bytes. The shipped login page always sends valid JSON, so this usually means a script or a proxy rewrote the body.

### I log in on one machine but another machine asks again

Tokens only carry over if every machine has exactly the same `FIAOS_PASSWORD`. Restart each server after changing it.

### I logged out but the token still works on another machine

That's by design. The revocation list is per machine. To kill every session everywhere, change `FIAOS_PASSWORD` on all machines.

### The session cookie isn't kept over plain HTTP from a remote host

Behind HTTPS, the server sets the cookie `Secure; SameSite=None` when it sees `X-Forwarded-Proto: https`. On plain HTTP it uses `SameSite=Lax`. If your proxy terminates HTTPS without setting that header, the browser may handle the cookie differently than expected. Pass `X-Forwarded-Proto $scheme` as the shipped nginx config does.

## Terminal problems

### `[shell spawn failed: ...]`

The server couldn't start the shell. On macOS it runs `/bin/zsh -l -i`. On Windows it imports `winpty` and runs `powershell.exe`, so check that `pywinpty` is installed in the venv.

### `[BLOCKED] Can't kill FiaOS services from remote terminal.`

The legacy line-mode input matched a pattern that would kill FiaOS itself, such as `pkill ... server.py`, `killall python`, `taskkill ... python` or `Stop-ScheduledTask ... Fia`. Restart FiaOS from the machine itself or through your service manager.

### `Disconnected (code) — reconnecting...`

The terminal WebSocket closed. The UI retries with backoff up to 30 s. Close codes 1008 and 4001 send you back to the login page. Behind nginx, `/api/terminal` needs the upgrade headers and a long `proxy_read_timeout` (the shipped config uses 86400).

## Natural-language helper (`/api/command`)

| Output | Meaning |
|---|---|
| `BLOCKED: This command was flagged as potentially dangerous.` | The proposed or `RAW:` command matched a blocked pattern. |
| `Both Claude and Ollama failed: ...` | `claude -p` failed (not installed, not logged in) and `ollama run qwen3-coder` also failed. |
| `Ollama couldn't parse: ...` | Ollama answered, but not with JSON. |
| `Command timed out after 30 minutes` | Hit the 1800 s limit. |
| `No command provided` (400) | Empty `command` field. |

## Multi-machine and tunnel problems

### A machine tab has a red dot

The probe for that machine failed. In nginx mode it means the VPS couldn't get `/login` from that machine's tunnel port within 3 s / 5 s. Check, in order: FiaOS running on that machine, the tunnel process up, the right `FIA_REMOTE_PORT`, the `map` / `upstream` entries in nginx.

### `FiaOS: the PC is not serving yet. SSH tunnel is up on :9020 but nothing is listening behind it on the PC.`

This comes from nginx, not FiaOS. The shipped config disables the fallback for `pc` (`$fia_fallback_ok` is `0`). Once your PC is serving, set it to `1` in `deploy/nginx-fia.conf` and reload nginx.

### I switched tabs but landed on the mini (`X-Fia-Fallback: mini`)

The machine you picked didn't answer within nginx's 5 s connect / 15 s read timeout, so nginx served the mini and rewrote your `fia_target` cookie to `mini`.

### `remote port forwarding failed for listen port 9010`

A dead SSH session on the VPS still holds the port. This is the "dead-tunnel deadlock" fixed in commit `0c1a876`. Install `deploy/sshd-tunnel-keepalive.conf` on the VPS (`sshd -t && systemctl reload ssh`), and use `deploy/tunnel_with_stale_port_clear.sh`, which clears the stale port before reconnecting.

### `:9010 already serving, another tunnel is live - exiting`

Not an error. `tunnel_with_stale_port_clear.sh` found a working tunnel and stood down so two don't stack. It sleeps 30 s before exiting so launchd doesn't restart it in a tight loop.

### `<key> unreachable via <host:port>: ...` (HTTP 502)

The Windows built-in proxy found the peer reachable but the proxied request failed. WebSocket proxy failures are logged as `[FiaOS] ws proxy to <key> (<host:port>) failed: ...`.

### Windows noVNC tab is blank or 404

`/vnc` redirects to `/static/novnc/vnc.html`, which isn't in the repo. Put noVNC's files in `windows/static/novnc/` and run a VNC server on `127.0.0.1:5900`. If nothing is listening there, the `/api/vnc` socket closes with code 1011.

## Voice leftovers

`start.sh`, `watchdog.sh`, `start_personaplex.sh` and `deploy/tunnel_to_vps.sh` refer to PersonaPlex (`personaplex_mlx`, port 8998), which is no longer in this repo. Errors from those scripts about `personaplex_mlx` are expected. Use the LaunchAgent or `launch_server.sh`.
