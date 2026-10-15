---
title: FiaOS FAQ - Claude Code on your phone, SSH and VNC alternative
description: Answers about FiaOS, the self-hosted browser remote desktop and terminal. Does it replace SSH or VNC, does it need Claude Code, how one login covers several machines, is it safe to expose to the internet.
---

# FAQ

## Can I run Claude Code on my Mac from my phone with this?

Yes. That's what the Terminal tab is for. It's a real PTY, so `claude` runs exactly as it would in Terminal.app. Open the FiaOS URL on your phone, log in, and type `claude`. Claude Code itself is Anthropic's tool and needs its own install and login on the machine.

## Does it work without Claude Code?

Yes. The screen and terminal don't depend on it. Only the optional natural-language helper (`/api/command`) and `/api/claude_stream` call the `claude` CLI, and the helper falls back to a local Ollama model.

## Does it replace SSH?

For interactive use, mostly. You get a real shell with a proper TTY. For `scp`, `rsync`, `git` over SSH, or port forwarding, you still want SSH. FiaOS has file upload and download routes, but no sync.

## How is it different from VNC or Apple Remote Desktop?

FiaOS splits the screen into 128 px tiles and sends only the tiles whose hash changed, as JPEGs over one WebSocket. A still screen sends nothing. It runs in any browser with no client. It is not a full framebuffer protocol, though. It can't get past the macOS lock or login screen, because macOS blocks synthetic input there. For that, the Windows build has a noVNC bridge to a real VNC server on `127.0.0.1:5900`. You have to supply the noVNC files yourself.

## How does one login work on several machines?

Every machine uses the same `FIAOS_PASSWORD`, and session tokens are HMAC-signed with it. A token from one machine verifies on any other without the machines sharing state. Picking a machine tab sets a `fia_target` cookie, and nginx (or the Windows server's proxy) sends the request to that machine. See [How it works](how-it-works.md#login-and-sessions).

## What happens if a machine is off?

Its tab shows a red dot. With the reference nginx config, a request for a dead machine falls back to the mini within the 5 s / 15 s timeouts, and your cookie is rewritten to `mini`. The shipped config turns this fallback off for `pc`.

## Can I use it only on my home network?

Yes. Run the server and open `http://<machine>.local:9000` from any device on the LAN. No VPS, tunnel, nginx or domain needed.

## Is it safe to put on the internet?

It has one shared password, signed 24-hour sessions, per-IP login rate limiting (10 tries per 5 minutes), and no default password. But the Terminal is a full shell, so anyone with the password owns the machine. Use a long random password, serve it over HTTPS (the reference setup uses nginx with certbot), and add 2FA in front with something like Cloudflare Access or Authelia if you need it. See [Configuration > Security notes](configuration.md#security-notes).

## Which platforms are supported?

macOS on Apple Silicon (macOS 14+ per the README) and Windows 10/11. Both need Python 3.12+. TODO-VERIFY: Linux has no server; `server.py` depends on macOS tools (`screencapture`, `sips`, `osascript`, `pbcopy`, Quartz).

## Do I need a GPU or MLX?

No. The MLX and PersonaPlex dependencies in `requirements.txt` / `pyproject.toml` are left over from a voice feature that has been removed. The screen and terminal only need `aiohttp`, `psutil`, `numpy`, `pillow`, PyObjC Quartz, and `anthropic` (imported by the macOS executor).

## What happened to the voice tab?

It was in the April 2026 release and was removed in v2. Voice now lives in a separate project. A few scripts still mention it; see [Troubleshooting > Voice leftovers](troubleshooting.md#voice-leftovers).

## Can I change the port?

Not without editing the code. `PORT = 9000` is hard-coded in both servers. Behind nginx it doesn't matter, because each machine gets its own port on the VPS through its tunnel.

## Can I add more than three machines?

The UI has three tabs hard-coded (`mini`, `m5`, `pc`) in `static/index.html` and `static/login.html`. nginx has a matching `map` and `upstream` per machine. Adding a fourth means editing both HTML files and the nginx config. TODO-VERIFY: there's no documented procedure for this.

## Is FiaOS related to claude-screen-to-phone?

Same author, different approach. [`claude-screen-to-phone`](https://github.com/nicedreamzapp/claude-screen-to-phone) is asynchronous: you text a command and get screenshots back over iMessage. FiaOS is live, in a browser.

## Can the Claude Code session use local models instead of an API key?

The author's separate project [claude-code-local](https://github.com/nicedreamzapp/claude-code-local) is for running Claude Code against local MLX models on a Mac. It's not part of FiaOS.
