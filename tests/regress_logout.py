"""Does a logged-out token still work? Run against any server.py."""
import asyncio, importlib.util, os, sys, tempfile
from pathlib import Path
os.environ["FIAOS_PASSWORD"] = "test-password-not-the-real-one"
SRC = sys.argv[1]
sys.path.insert(0, os.path.dirname(SRC))
spec = importlib.util.spec_from_file_location("fsrv", SRC)
m = importlib.util.module_from_spec(spec); sys.modules["fsrv"] = m
spec.loader.exec_module(m)
TMP = tempfile.mkdtemp()
m.SESSION_FILE = Path(TMP) / ".sessions.json"
if hasattr(m, "REVOKED_FILE"):
    m.REVOKED_FILE = Path(TMP) / ".revoked.json"
m.sessions.clear()
m.STATIC_DIR = Path(os.path.dirname(os.path.abspath(SRC))) / "static"

from aiohttp.test_utils import TestServer, TestClient
async def go():
    client = TestClient(TestServer(m.create_app()))
    await client.start_server()
    try:
        r = await client.post("/api/login", json={"password": os.environ["FIAOS_PASSWORD"]})
        token = (await r.json())["token"]
        await client.get("/logout", allow_redirects=False)
        r = await client.get(f"/api/status?token={token}")
        if r.status == 401:
            print(f"  SECURE   logged-out token rejected (401)")
            return 0
        print(f"  VULNERABLE  logged-out token STILL WORKS (HTTP {r.status})")
        return 1
    finally:
        await client.close()
sys.exit(asyncio.run(go()))
