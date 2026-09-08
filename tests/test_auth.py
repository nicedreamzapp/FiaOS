"""FiaOS auth + session hardening: every scenario, simulated.

Runs against the REAL server.py with a throwaway password and throwaway state
files, so it never touches Matt's live sessions.
"""
import asyncio, importlib.util, json, os, sys, tempfile, time, types

os.environ["FIAOS_PASSWORD"] = "test-password-not-the-real-one"
SRC = sys.argv[1] if len(sys.argv) > 1 else "server.py"
sys.path.insert(0, os.path.dirname(SRC))   # executor/screencast live beside it

spec = importlib.util.spec_from_file_location("fsrv", SRC)
m = importlib.util.module_from_spec(spec)
sys.modules["fsrv"] = m
spec.loader.exec_module(m)

# Redirect all persistence into a temp dir BEFORE anything writes.
TMP = tempfile.mkdtemp(prefix="fiaos_test_")
from pathlib import Path
m.SESSION_FILE = Path(TMP) / ".sessions.json"
m.REVOKED_FILE = Path(TMP) / ".revoked.json"
m.sessions.clear()
m.revoked.clear()
m.login_attempts.clear()

PASS = []
FAIL = []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if detail and not cond else ""))

def reset():
    m.sessions.clear(); m.revoked.clear(); m.login_attempts.clear()

print("\n=== 1. TOKEN VALIDATION ===")
reset()
tok = m.create_session()
check("fresh token validates", m.valid_session(tok))
check("empty token rejected", not m.valid_session(""))
check("garbage token rejected", not m.valid_session("nonsense"))
check("wrong-version token rejected", not m.valid_session("v9.999999999.abc"))
check("non-integer expiry rejected", not m.valid_session("v1.notanumber.abc"))
check("two-part token rejected", not m.valid_session("v1.123"))
check("four-part token rejected", not m.valid_session("v1.123.abc.def"))

exp = m._token_expiry(tok)
tampered = f"v1.{exp + 86400}.{tok.split('.')[2]}"   # push expiry out, keep old mac
check("tampered expiry rejected (signature fails)", not m.valid_session(tampered))
bad_mac = f"v1.{exp}.{'0' * 64}"
check("forged signature rejected", not m.valid_session(bad_mac))

print("\n=== 2. EXPIRY ===")
reset()
past = int(time.time()) - 5
dead = m._sign(past)
check("expired signed token rejected", not m.valid_session(dead))
m.sessions[dead] = past
check("expired token in store still rejected", not m.valid_session(dead))
check("expired token evicted from store on check", dead not in m.sessions)

print("\n=== 3. CROSS-MACHINE LOGIN (the whole point of signing) ===")
reset()
other = m._sign(int(time.time()) + 3600)      # minted elsewhere, never in our store
check("token from another machine validates", m.valid_session(other))
check("...without being in our session store", other not in m.sessions)

print("\n=== 4. LOGOUT REVOCATION (the fix) ===")
reset()
tok = m.create_session()
check("token valid before logout", m.valid_session(tok))
m.revoke_session(tok)
check("token REJECTED after revoke", not m.valid_session(tok))
check("revocation persisted to disk", m.REVOKED_FILE.exists())
check("revocation survives a store wipe", (m.sessions.clear(), not m.valid_session(tok))[1])

reset()
foreign = m._sign(int(time.time()) + 3600)
check("can revoke a token we never issued", m.revoke_session(foreign) and not m.valid_session(foreign))

reset()
legacy = "legacyrandomtoken123"
m.sessions[legacy] = time.time() + 3600
check("legacy random token validates", m.valid_session(legacy))
m.revoke_session(legacy)
check("legacy token revoked by removal", not m.valid_session(legacy))

reset()
already = m._sign(int(time.time()) - 5)
check("revoking an already-dead token is a no-op", m.revoke_session(already) is False)
check("...and does not grow the revoked list", len(m.revoked) == 0)

print("\n=== 5. THE LISTS STAY BOUNDED ===")
reset()
now = time.time()
for i in range(50):
    m.sessions[f"dead{i}"] = now - 1
for i in range(5):
    m.sessions[f"live{i}"] = now + 3600
check("sweep drops exactly the expired sessions", m._sweep_sessions() == 50)
check("sweep keeps the live ones", len(m.sessions) == 5)

reset()
for i in range(30):
    m.revoked[f"dead{i}"] = now - 1
m.revoked["live"] = now + 3600
check("revoked sweep drops expired", m._sweep_revoked() == 30)
check("revoked sweep keeps live", list(m.revoked) == ["live"])

reset()
before = len(m.sessions)
m.create_session(); time.sleep(0.01)
m.sessions["stale"] = now - 1
m.create_session()
check("login sweeps the session file", "stale" not in m.sessions)

print("\n=== 6. RATE LIMITING ===")
reset()
ip = "203.0.113.7"
for _ in range(m.MAX_LOGIN_ATTEMPTS):
    m.record_attempt(ip)
check("trips at MAX_LOGIN_ATTEMPTS", m.check_rate_limit(ip))
check("a different IP is unaffected", not m.check_rate_limit("198.51.100.9"))

reset()
m.login_attempts["old"] = [time.time() - m.LOGIN_WINDOW - 10]
m.check_rate_limit("old")
check("empty bucket is deleted, not kept", "old" not in m.login_attempts)

reset()
for i in range(m.MAX_IP_BUCKETS + 100):
    m.record_attempt(f"10.0.{i // 256}.{i % 256}")
check("bucket table stays under the ceiling",
      len(m.login_attempts) <= m.MAX_IP_BUCKETS, f"got {len(m.login_attempts)}")

print("\n=== 7. CLIENT IP (rate limiting the caller, not the tunnel) ===")
def req(peer, headers=None):
    r = types.SimpleNamespace()
    r.remote = peer
    r.headers = headers or {}
    return r
check("loopback + X-Real-IP -> the real caller",
      m.client_ip(req("127.0.0.1", {"X-Real-IP": "8.8.8.8"})) == "8.8.8.8")
check("loopback + CF-Connecting-IP wins",
      m.client_ip(req("127.0.0.1", {"CF-Connecting-IP": "1.1.1.1", "X-Real-IP": "8.8.8.8"})) == "1.1.1.1")
check("loopback with no header -> loopback",
      m.client_ip(req("127.0.0.1")) == "127.0.0.1")
check("NON-loopback cannot spoof X-Real-IP",
      m.client_ip(req("192.168.1.50", {"X-Real-IP": "8.8.8.8"})) == "192.168.1.50")
check("absurdly long header is truncated",
      len(m.client_ip(req("127.0.0.1", {"X-Real-IP": "9" * 5000}))) <= 64)

print("\n=== 8. CORRUPT / MISSING STATE FILES ===")
m.SESSION_FILE.write_text("{ this is not json")
check("corrupt session file -> empty, no crash", m._load_sessions() == {})
m.REVOKED_FILE.write_text("]]]garbage")
check("corrupt revoked file -> empty, no crash", m._load_revoked() == {})
m.SESSION_FILE.unlink(missing_ok=True)
check("missing session file -> empty, no crash", m._load_sessions() == {})

ok = m._write_json_atomic(m.SESSION_FILE, {"a": 1})
check("atomic write succeeds", ok and json.loads(m.SESSION_FILE.read_text()) == {"a": 1})
check("no .tmp left behind", not (Path(TMP) / ".sessions.json.tmp").exists())
ok2 = m._write_json_atomic(Path("/nope/does/not/exist/x.json"), {"a": 1})
check("unwritable path fails cleanly, no raise", ok2 is False)

stale = json.dumps({"expired": time.time() - 1, "good": time.time() + 3600})
m.SESSION_FILE.write_text(stale)
check("loader prunes expired at startup", list(m._load_sessions()) == ["good"])

print("\n=== 9. LIVE HTTP (real handlers, real cookies) ===")
from aiohttp.test_utils import TestServer, TestClient

async def http_tests():
    reset()
    app = m.create_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        r = await client.post("/api/login", json={"password": "wrong"})
        check("wrong password -> 401", r.status == 401)

        r = await client.post("/api/login", json={"password": os.environ["FIAOS_PASSWORD"]})
        check("right password -> 200", r.status == 200)
        body = await r.json()
        token = body.get("token", "")
        check("login returns a token", bool(token))
        check("login sets the session cookie", m.SESSION_COOKIE in r.cookies)

        r = await client.get("/", allow_redirects=False)
        check("authenticated GET / -> 200", r.status == 200)

        r = await client.get("/api/status")
        check("authenticated API call -> 200", r.status == 200)

        # THE regression this whole pass is about
        r = await client.get("/logout", allow_redirects=False)
        check("logout redirects to /login", r.status in (302, 303, 307))
        r = await client.get(f"/?token={token}", allow_redirects=False)
        check("LOGGED-OUT TOKEN IS DEAD over HTTP", r.status == 302,
              f"got {r.status} - token still works after logout")
        r = await client.get(f"/api/status?token={token}")
        check("logged-out token rejected on the API too", r.status == 401)

        client.session.cookie_jar.clear()
        r = await client.get("/", allow_redirects=False)
        check("no cookie -> bounced to /login", r.status == 302)

        r = await client.get("/?token=v1.99999999999.deadbeef", allow_redirects=False)
        check("forged token -> bounced to /login", r.status == 302)

        r = await client.post("/api/login", data=b"x" * 5000,
                              headers={"Content-Type": "application/json"})
        check("oversized login body -> 413", r.status == 413, f"got {r.status}")

        r = await client.post("/api/login", data=b"not json at all",
                              headers={"Content-Type": "application/json"})
        check("malformed login body -> 400 not 500", r.status == 400, f"got {r.status}")

        r = await client.post("/api/login", json={"password": 12345})
        check("non-string password -> 401 not 500", r.status == 401, f"got {r.status}")

        r = await client.post("/api/login", json={})
        check("missing password field -> 401 not 500", r.status == 401, f"got {r.status}")

        r = await client.post("/api/login", json={"password": "pässwörd-ünïcode"})
        check("non-ascii password -> 401 not 500", r.status == 401, f"got {r.status}")

        m.login_attempts.clear()
        codes = []
        for _ in range(m.MAX_LOGIN_ATTEMPTS + 3):
            rr = await client.post("/api/login", json={"password": "wrong"})
            codes.append(rr.status)
        check("brute force eventually -> 429", 429 in codes, f"codes={codes[-4:]}")

        # and the real password is refused while locked out
        rr = await client.post("/api/login", json={"password": os.environ["FIAOS_PASSWORD"]})
        check("lockout applies to correct password too", rr.status == 429)
    finally:
        await client.close()

asyncio.run(http_tests())

print("\n" + "=" * 58)
print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:")
    for f in FAIL:
        print("  -", f)
print("=" * 58)
sys.exit(1 if FAIL else 0)
