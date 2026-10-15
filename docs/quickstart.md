---
title: FiaOS quickstart - browser terminal and screen in five minutes
description: Start FiaOS, log in from a browser, and get a live screen and a real shell on your Mac or Windows PC. Then make it start at login and, optionally, reach it from outside your network.
---

# Quickstart

This assumes you've done the [Install](install.md) steps.

## 1. Pick a password

The server won't start without `FIAOS_PASSWORD`, and there is no default in the source. Generate a long random one:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

If you'll run FiaOS on more than one machine, use the **same password on all of them**. Session tokens are HMAC-signed with it, which is what lets one login work on every machine.

## 2. Run it

=== "macOS"

    ```bash
    cd FiaOS
    FIAOS_PASSWORD='your-strong-password' .venv/bin/python3 server.py
    ```

    You should see:

    ```text
    [FiaOS] Starting on port 9000
    [FiaOS] Dashboard: http://localhost:9000
    ```

=== "Windows"

    ```powershell
    cd FiaOS\windows
    .\run_fiaos.ps1
    ```

    The launcher loads `C:\ProgramData\fia\fiaos.env`, runs `server.py` with the venv's Python, and writes output to `C:\ProgramData\fia\fiaos.log`, not the console. Before it serves, the Windows server runs a preflight check that the capture size matches the screen size, and prints lines like:

    ```text
    [FiaOS] display: 1920x1080 pixels, input space 1920x1080 — grid matches capture
    [FiaOS] machine: pc
    ```

    (The numbers will be your display's.)

## 3. Open the page

Go to `http://localhost:9000` on the same machine, or `http://<machine-name>.local:9000` / `http://<lan-ip>:9000` from another device on your network. Enter the password.

You land on the main page with two views:

- **Screen**: the live desktop. Click, drag, scroll and type.
- **Terminal**: a real shell. Try `claude`, `vim` or `top`.

CPU and free RAM show as small chips at the top. They refresh every 10 seconds, and only while the page is visible.

## 4. Start it at login

=== "macOS (LaunchAgent)"

    1. Copy `examples/com.fiaos.server.plist` to `~/Library/LaunchAgents/`.
    2. Replace the three `/REPLACE/WITH/ABSOLUTE/PATH/TO/FiaOS` placeholders with your real path.
    3. Set `FIAOS_PASSWORD`. If this isn't your first machine, add a `FIAOS_MACHINE` key too; the example plist doesn't include it.
    4. Load it:

    ```bash
    launchctl load ~/Library/LaunchAgents/com.fiaos.server.plist
    ```

    The plist has `RunAtLoad` and `KeepAlive` on and logs to `/tmp/fiaos.log`. It must be a LaunchAgent (your GUI session), not a LaunchDaemon. Screen capture and input need the logged-in desktop.

    `launch_server.sh` is a small alternative entry point. It `cd`s to its own directory and execs `.venv/bin/python3 -u server.py`.

=== "Windows (Task Scheduler)"

    Register `windows\run_fiaos.ps1` as a Task Scheduler task that runs **at logon** for your user. The script already loops: when `server.py` exits it logs the run time, waits 5 seconds and starts it again. It rotates `fiaos.log` to `fiaos.log.1` once the log is over 5 MB.

!!! note "Avoid start.sh and watchdog.sh"
    Both scripts still try to start the removed PersonaPlex voice agent (`personaplex_mlx`), which isn't in this repo. `watchdog.sh` restarts it every loop because port 8998 is never listening. Use the LaunchAgent or `launch_server.sh`.

## 5. Reach it from outside (optional)

On your home network only, you're done. To reach your machines from anywhere, the repo includes a reference setup: each machine opens a reverse SSH tunnel to a VPS, and nginx on the VPS does HTTPS and picks the machine based on a cookie.

| File | Use |
|---|---|
| `deploy/tunnel_with_stale_port_clear.sh` | Reverse tunnel from one machine to the VPS. Clears a dead port on the VPS first so a laptop that slept reconnects right away. |
| `deploy/sshd-tunnel-keepalive.conf` | Goes on the VPS as `/etc/ssh/sshd_config.d/10-tunnel-keepalive.conf`. Makes sshd drop a dead tunnel in about 90 seconds. |
| `deploy/nginx-fia.conf` | The nginx site: cookie routing, per-machine liveness probes, WebSocket upgrade, static caching, dead-machine fallback. Replace `fia.example.com` with your hostname and run `certbot --nginx -d <your host>`. |

Give each machine its own port on the VPS. The shipped config uses `9000` (mini), `9010` (m5) and `9020` (pc). Details are in [Configuration > Deployment variables](configuration.md#deployment-and-tunnel-variables) and [How it works > Multi-machine routing](how-it-works.md#multi-machine-routing).

!!! warning "deploy/tunnel_to_vps.sh is a voice-era leftover"
    Despite the name in the README, that script tunnels port **8998** (PersonaPlex), not FiaOS's 9000, and has `VPS_IP="YOUR_VPS_IP"` hard-coded. Use `tunnel_with_stale_port_clear.sh` for FiaOS.

Treat the password like an SSH key. If you want 2FA, put FiaOS behind a proxy that provides it, such as Cloudflare Access or Authelia.
