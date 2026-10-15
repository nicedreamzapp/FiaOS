# FiaOS tests

## pytest suite (runs anywhere, runs in CI)

    python -m venv .venv-test
    .venv-test/bin/pip install -r requirements-test.txt
    .venv-test/bin/python -m pytest

No Mac, GPU, model download, network, Claude Code or Ollama needed. It imports
the real `server.py` and `windows/server.py` with a throwaway password and puts
every state file in a temp dir, so it never touches live sessions. LLM calls are
replaced with fake subprocesses. The three tests that need real CoreGraphics
(screen capture and the input driver) skip unless run on macOS with
`pyobjc-framework-Quartz` installed.

| File | What it covers |
| --- | --- |
| `test_sessions.py` | signed tokens, expiry, logout revocation, rate limiting, client IP behind the tunnel, state files |
| `test_http.py` | the real aiohttp app: login/logout, bad login bodies, auth on every route, `/api/status`, the file API in a fake home dir |
| `test_executor.py` | the command safety filter, LLM output parsing, Claude to Ollama fallback, `RAW:` commands |
| `test_screen_protocol.py` | tile records, grid geometry, input event encoding, the warm-worker cache |
| `test_misc.py` | terminal self-kill guard, key code tables, `FIAOS_PEERS` parsing, shell script syntax |
| `test_legacy_scripts.py` | runs `test_auth.py` and `regress_logout.py` below, and checks startup without a password |

`.github/workflows/ci.yml` runs this on every push and pull request, on Python
3.12 and 3.13, plus `flake8 --select=E9,F63,F7,F82` (syntax errors and
undefined names).

## Original scripts

Written 2026-09-07 after the M5 tab was unreachable for 8+ hours.

    .venv/bin/python tests/test_auth.py server.py     # 62 auth/session checks
    bash tests/test_infra.sh                          # 15 live failure scenarios

`test_auth.py` imports the real server.py with a throwaway password and temp
state files, so it never touches live sessions. `test_infra.sh` deliberately
breaks things (freezes the server, kills the tunnel, plants a zombie listener
on the VPS) and restores everything on exit via a trap.

`regress_logout.py <server.py>` answers one question against any copy of the
file: does a logged-out token still work? Pre-2026-09-07 code answers 200.
