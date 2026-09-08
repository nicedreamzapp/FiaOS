# FiaOS tests

Written 2026-09-07 after the M5 tab was unreachable for 8+ hours.

    .venv/bin/python tests/test_auth.py server.py     # 62 auth/session checks
    bash tests/test_infra.sh                          # 15 live failure scenarios

`test_auth.py` imports the real server.py with a throwaway password and temp
state files, so it never touches live sessions. `test_infra.sh` deliberately
breaks things (freezes the server, kills the tunnel, plants a zombie listener
on the VPS) and restores everything on exit via a trap.

`regress_logout.py <server.py>` answers one question against any copy of the
file: does a logged-out token still work? Pre-2026-09-07 code answers 200.
