"""Run the original standalone scripts (tests/test_auth.py, tests/regress_logout.py)
so their checks stay part of CI. Each is a subprocess because both scripts
import server.py at module level and call sys.exit with their result."""
import os
import subprocess
import sys

from conftest import ROOT, TEST_PASSWORD


def _run(script, server):
    env = {**os.environ, "FIAOS_PASSWORD": TEST_PASSWORD,
           "PYTHONPATH": str(ROOT / "tests" / "stubs")}
    return subprocess.run([sys.executable, str(ROOT / "tests" / script), str(server)],
                          capture_output=True, text=True, timeout=120, env=env, cwd=ROOT)


def test_auth_script_all_checks_pass():
    result = _run("test_auth.py", ROOT / "server.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: 62 passed, 0 failed" in result.stdout


def test_logged_out_token_is_dead():
    # macOS copy only: the script serves static/ from beside the server file,
    # and windows/static is not committed. test_http.py covers both copies.
    result = _run("regress_logout.py", ROOT / "server.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SECURE" in result.stdout
