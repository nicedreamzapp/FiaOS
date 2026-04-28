# FiaOS

Drive your Mac from anywhere — live screen, real interactive shell, voice agent — through one self-hosted web page.

```
   ┌────────────────────────────────────────────────┐
   │  fia.your-domain.com                           │
   │  ┌────────────┬────────────┬────────────┐      │
   │  │   Screen   │  Terminal  │   Voice    │      │
   │  ├────────────┴────────────┴────────────┤      │
   │  │                                       │      │
   │  │  Live screenshot of your Mac          │      │
   │  │  (click anywhere to drive the mouse)  │      │
   │  │                                       │      │
   │  └───────────────────────────────────────┘      │
   └────────────────────────────────────────────────┘

         ↕  WebSocket + HTTPS over your VPS tunnel  ↕

   ┌────────────────────────────────────────────────┐
   │  Mac mini at home                              │
   │  ┌──────────────────────────────────────────┐  │
   │  │  FiaOS server.py (Python, aiohttp)       │  │
   │  │  ├─ /api/screenshot  — quartz capture     │  │
   │  │  ├─ /api/mouse       — cliclick relay     │  │
   │  │  ├─ /api/terminal    — PTY-backed zsh     │  │
   │  │  └─ /voice           — PersonaPlex bridge │  │
   │  └──────────────────────────────────────────┘  │
   └────────────────────────────────────────────────┘
```

## What it gives you

- **Screen tab** — live screenshot of your Mac, refreshing every 1–5 s. Click anywhere on the image to relay the click. Type in the keyboard input to send keystrokes (modifiers included).
- **Terminal tab** — a real interactive shell. PTY-backed zsh (login + interactive). `cd` sticks. `claude`, `vim`, `top`, `htop`, anything that needs a TTY just works. Rendered with [xterm.js](https://xtermjs.org/) so colors, cursor, ANSI escapes are accurate.
- **Voice tab** — push-to-talk or always-on mic that streams to a local on-device voice model ([PersonaPlex MLX](https://github.com/) running on Apple Silicon). Loads on demand, idles out to free RAM.
- **Auth** — single password, signed session cookies persisted to disk, login rate-limit. Sessions survive restarts.

## Why

Tailscale + an SSH client + a VNC viewer can all do pieces of this. FiaOS bundles them into one auth-gated web page so I can drive my Mac mini from any device — phone, laptop, friend's machine — without installing anything client-side. The Terminal tab specifically gets me a working Claude Code session on the Mac mini from my phone.

## Install

> Apple Silicon Mac, macOS 14+, Python 3.12+. The voice tab is optional.

```bash
git clone https://github.com/nicedreamzapp/FiaOS.git
cd FiaOS
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 1. Set a password

Generate something strong:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

The server **refuses to start without `FIAOS_PASSWORD` set** — there is no default.

### 2. Run it (foreground, for testing)

```bash
FIAOS_PASSWORD='your-strong-password' .venv/bin/python3 server.py
# open http://localhost:9000
```

### 3. Run it as a LaunchAgent (always on)

Copy `examples/com.fiaos.server.plist` to `~/Library/LaunchAgents/`, edit the password, then:

```bash
launchctl load ~/Library/LaunchAgents/com.fiaos.server.plist
```

The bundled `watchdog.sh` script can be wired up the same way for self-healing.

### 4. Expose it (optional)

`tunnel_to_vps.sh` opens an autossh reverse tunnel to a VPS so you can hit it at `https://fia.your-domain.com`. `nginx-fia.conf` is the matching nginx site config (HTTPS termination + WebSocket upgrade headers). Both are templates — replace the hostnames and key paths.

If you only want to use it on your home network, skip this step and use the `.local` hostname.

### 5. Voice mode (optional)

Voice requires [PersonaPlex MLX](https://github.com/nicedreamzapp/) and Hugging Face access to the model weights. See the `personaplex_mlx` install instructions. Once it's pip-installed, FiaOS will spawn it on demand when you hit the Voice tab and shut it back down after 60 s of idle.

## Security notes

- The Terminal is a **real interactive shell** running as your user. Anyone who knows the password has the same power as SSH. Treat the password like an SSH key.
- All endpoints check the session cookie. There is no anonymous access.
- The login endpoint rate-limits to 10 attempts per 5 minutes per IP.
- The basic command filter (`_PROTECTED_PATTERNS`) blocks the obvious "kill FiaOS itself" footguns but is not a security boundary — a real PTY can run arbitrary scripts.
- Use a long random password. Use HTTPS (the nginx config terminates TLS).
- 2FA is not built in. If you want it, put FiaOS behind a reverse proxy that does it (Authelia, Cloudflare Access, etc.).

## Layout

```
server.py          — aiohttp server, all routes, PTY terminal handler
executor.py        — natural-language → shell helper (used by older /api/command)
fia_ptt.py         — voice push-to-talk WebSocket bridge
fia_talk.py        — Fia persona / TTS layer
sample_voices.py   — voice sample preview helper
static/
  index.html       — single-page UI (Screen / Terminal / Voice)
  login.html       — password form
  *.js *.wasm      — Opus encoder/decoder workers for voice streaming
launch_server.sh   — venv launcher used by the LaunchAgent
start.sh           — dev helper
watchdog.sh        — keep-alive checker
tunnel_to_vps.sh   — autossh reverse tunnel
nginx-fia.conf     — nginx HTTPS + WebSocket upgrade template
```

## License

MIT — see [LICENSE](LICENSE).

## Author

Built by [Matt Macosko](https://github.com/nicedreamzapp). Fia is named after the assistant who lives on the Mac mini.
