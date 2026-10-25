# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report them privately through GitHub: go to the **Security** tab of this repo and click **Report a vulnerability** ([direct link](https://github.com/nicedreamzapp/FiaOS/security/advisories/new)). Only the maintainer sees the report.

Helpful details: what you found, how to reproduce it, which version or commit, the OS of the machine running the server (macOS or Windows), and whether it was reached directly or through the nginx and tunnel setup in `deploy/`.

I aim to reply within 3 days and to ship a fix or a clear plan within 30 days, depending on severity. You will be credited in the advisory unless you ask not to be.

## Supported versions

Fixes go into the latest release on `main`. Please update before reporting if you are on an older version.

## What matters most here

The parts where a mistake could hurt someone:

- login, signed session cookies, logout revocation and rate limiting in `server.py` and `windows/server.py`, since anyone who gets past them has a real shell
- the PTY terminal, screen view and mouse and keyboard input (`screencast.py`, `screen_worker.py`, `input_helper.py` and their `windows/` versions)
- the file and clipboard routes in `server.py` and `windows/server.py`
- the natural-language command helper in `executor.py` and `windows/executor.py`, and its blocked-command filter
- the multi-machine edge in `deploy/` (cookie routing in `nginx-fia.conf` and the reverse SSH tunnels)
