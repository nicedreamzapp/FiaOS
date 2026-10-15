---
title: FiaOS changelog - release history from git
description: FiaOS release history built from the repository's git log. Initial release in April 2026, the v2 Windows port and multi-machine routing in September 2026, idle and tunnel fixes, and documentation updates.
---

# Changelog

The repo has no git tags or GitHub releases. This page comes from the commit history on `main`. Dates are commit dates. Numbers quoted below come from the commit messages themselves; they are the author's measurements on his own machines, not benchmarks in this repo.

## 2026-10-09: README updates

- `78f7d64` Link to claude-code-local (#3).
- `5598206` Fix the macOS install line, note where `FIAOS_PEERS` is read, trim sponsor text (#2).

## 2026-09-27: README summary and known limits

- `4f3bcb3` (merged in #1) Adds a plain summary, the proof section pointing at the tests, a list of what was built in the repo, and a Known limits section.

## 2026-09-08: Dead-tunnel deadlock fix, real logout

`0c1a876`

- **Tunnels.** A laptop that slept left a dead SSH session holding its port on the VPS. nginx hung on it and the laptop could never reconnect. Adds `deploy/sshd-tunnel-keepalive.conf` (`ClientAliveInterval 30`), `deploy/tunnel_with_stale_port_clear.sh`, and 5 s connect / 15 s read timeouts on nginx's page-load locations.
  Commit message figures: hung machine falls back in 15 s (was 60 s+), killed tunnel self-heals in 4 s, a planted zombie port is cleared and reconnected in 11 s.
- **Logout now revokes the token.** Adds the persisted `.revoked.json`. Before this, a logged-out signed token kept working until it expired.
- `.sessions.json` is pruned at every login, not only at startup.
- State files are written atomically.
- Login rate limiting is keyed on the real client IP instead of the tunnel's `127.0.0.1`.
- The rate-limit table is pruned and capped.
- `/api/login` reads at most 4 KB.
- Adds `tests/test_auth.py`, `tests/test_infra.sh` and `tests/regress_logout.py`.

## 2026-09-07: Idle machines do no work

`87a2ac4`

- A parked screen worker now stops capturing (`P` command) instead of grabbing the screen into an unread pipe for 90 s. Commit message figures: on an M4, 19.7% of a core while parked before, 0.0% after; on the mini 10.6% to 0.0%.
- The page stops polling `/api/status` while it is hidden.
- Same change on Windows, using the stdin reader thread.

## 2026-09-07: Screenshots

`00a3ec9` New screenshots; removes the voice tab, which the build no longer had.

## 2026-09-07: v2, Windows port, multi-machine routing, live screen

`d404e12`

- **Windows port** in `windows/`: ConPTY terminal via pywinpty, `SendInput`, GDI capture, `SetThreadExecutionState`, Start Menu app list, the `run_fiaos.ps1` launcher that reads the password from a file, and a noVNC bridge.
- **Multi-machine**: `deploy/nginx-fia.conf` with `fia_target` cookie routing, per-machine liveness probes, and dead-machine fallback. LAN peers come from `FIAOS_PEERS`.
- **macOS live screen**: publishes `screencast.py`, `screen_worker.py`, `input_helper.py`, the `/api/screen` WebSocket and `/api/claude_stream`.
- Removes the password fallback from the source: the server aborts when `FIAOS_PASSWORD` is unset.
- Fixes the Windows recursive-delete guards, which never matched, and PowerShell clipboard encoding.

## 2026-08-06: Credits

`ecde0ba` Adds `CREDITS.md`, naming the upstream projects.

## 2026-04-27 to 2026-05-02: Initial release and README passes

- `97f0c36` **Initial release.** macOS `server.py`, `executor.py`, the web UI, and the voice-era files (`fia_ptt.py`, `fia_talk.py`, `sample_voices.py`, PersonaPlex scripts). The voice helpers were later dropped in v2.
- `890ed30`, `316c80a`, `a6f0a40` README rewrite with screenshots, search keywords, and a comparison with `claude-screen-to-phone`.
- `023615c` GitHub Sponsors link.
