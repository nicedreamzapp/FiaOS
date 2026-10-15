"""Signed session tokens, revocation, rate limiting and state files.

Runs against both server.py (macOS) and windows/server.py, which share this code.
"""
import json
import time
import types

import pytest


# ── token format ──────────────────────────────────────────────────────────

def test_fresh_token_validates(srv):
    tok = srv.create_session()
    assert tok.startswith("v1.")
    assert srv.valid_session(tok)


@pytest.mark.parametrize("token", [
    "", "nonsense", "v9.999999999.abc", "v1.notanumber.abc",
    "v1.123", "v1.123.abc.def",
])
def test_malformed_tokens_rejected(srv, token):
    assert not srv.valid_session(token)


def test_tampered_expiry_rejected(srv):
    tok = srv.create_session()
    exp = srv._token_expiry(tok)
    tampered = f"v1.{exp + 86400}.{tok.split('.')[2]}"
    assert not srv.valid_session(tampered)


def test_forged_signature_rejected(srv):
    exp = int(time.time()) + 3600
    assert not srv.valid_session(f"v1.{exp}.{'0' * 64}")


def test_signature_depends_on_password(srv, monkeypatch):
    exp = int(time.time()) + 3600
    tok = srv._sign(exp)
    monkeypatch.setattr(srv, "PASSWORD", "rotated-password")
    assert not srv.valid_session(tok)


def test_token_expiry_parser(srv):
    assert srv._token_expiry("v1.12345.abc") == 12345
    assert srv._token_expiry("v1.x.abc") is None
    assert srv._token_expiry("legacy") is None
    assert srv._token_expiry("v2.1.abc") is None


# ── expiry ────────────────────────────────────────────────────────────────

def test_expired_signed_token_rejected(srv):
    assert not srv.valid_session(srv._sign(int(time.time()) - 5))


def test_expired_token_in_store_is_evicted(srv):
    past = int(time.time()) - 5
    dead = srv._sign(past)
    srv.sessions[dead] = past
    assert not srv.valid_session(dead)
    assert dead not in srv.sessions


# ── cross-machine login (the reason tokens are signed) ────────────────────

def test_token_from_another_machine_validates(srv):
    other = srv._sign(int(time.time()) + 3600)
    assert srv.valid_session(other)
    assert other not in srv.sessions


# ── logout revocation ─────────────────────────────────────────────────────

def test_revoked_token_rejected_and_persisted(srv):
    tok = srv.create_session()
    assert srv.revoke_session(tok) is True
    assert not srv.valid_session(tok)
    assert srv.REVOKED_FILE.exists()
    assert tok in json.loads(srv.REVOKED_FILE.read_text())
    srv.sessions.clear()
    assert not srv.valid_session(tok)


def test_can_revoke_token_we_never_issued(srv):
    foreign = srv._sign(int(time.time()) + 3600)
    assert srv.revoke_session(foreign)
    assert not srv.valid_session(foreign)


def test_legacy_random_token(srv):
    srv.sessions["legacyrandomtoken123"] = time.time() + 3600
    assert srv.valid_session("legacyrandomtoken123")
    assert srv.revoke_session("legacyrandomtoken123")
    assert not srv.valid_session("legacyrandomtoken123")
    assert srv.revoke_session("never-seen") is False


def test_revoking_dead_token_is_noop(srv):
    assert srv.revoke_session(srv._sign(int(time.time()) - 5)) is False
    assert srv.revoked == {}


def test_revocations_reload_from_disk(srv):
    tok = srv.create_session()
    srv.revoke_session(tok)
    assert tok in srv._load_revoked()


# ── bounded state ─────────────────────────────────────────────────────────

def test_sweep_sessions_drops_only_expired(srv):
    now = time.time()
    for i in range(50):
        srv.sessions[f"dead{i}"] = now - 1
    for i in range(5):
        srv.sessions[f"live{i}"] = now + 3600
    assert srv._sweep_sessions() == 50
    assert sorted(srv.sessions) == [f"live{i}" for i in range(5)]


def test_sweep_revoked_drops_only_expired(srv):
    now = time.time()
    for i in range(30):
        srv.revoked[f"dead{i}"] = now - 1
    srv.revoked["live"] = now + 3600
    assert srv._sweep_revoked() == 30
    assert list(srv.revoked) == ["live"]


def test_login_sweeps_session_store(srv):
    srv.sessions["stale"] = time.time() - 1
    srv.create_session()
    assert "stale" not in srv.sessions
    assert "stale" not in json.loads(srv.SESSION_FILE.read_text())


# ── rate limiting ─────────────────────────────────────────────────────────

def test_rate_limit_trips_at_max(srv):
    ip = "203.0.113.7"
    for _ in range(srv.MAX_LOGIN_ATTEMPTS - 1):
        srv.record_attempt(ip)
    assert not srv.check_rate_limit(ip)
    srv.record_attempt(ip)
    assert srv.check_rate_limit(ip)
    assert not srv.check_rate_limit("198.51.100.9")


def test_rate_limit_window_ages_out(srv):
    srv.login_attempts["old"] = [time.time() - srv.LOGIN_WINDOW - 10] * 50
    assert not srv.check_rate_limit("old")
    assert "old" not in srv.login_attempts


def test_rate_limit_table_is_bounded(srv):
    for i in range(srv.MAX_IP_BUCKETS + 100):
        srv.record_attempt(f"10.0.{i // 256}.{i % 256}")
    assert len(srv.login_attempts) <= srv.MAX_IP_BUCKETS


# ── client IP behind the tunnel ───────────────────────────────────────────

def _req(peer, headers=None):
    return types.SimpleNamespace(remote=peer, headers=headers or {})


@pytest.mark.parametrize("peer,headers,expected", [
    ("127.0.0.1", {"X-Real-IP": "8.8.8.8"}, "8.8.8.8"),
    ("::1", {"X-Real-IP": "8.8.8.8"}, "8.8.8.8"),
    ("127.0.0.1", {"CF-Connecting-IP": "1.1.1.1", "X-Real-IP": "8.8.8.8"}, "1.1.1.1"),
    ("127.0.0.1", {"X-Real-IP": "   "}, "127.0.0.1"),
    ("127.0.0.1", {}, "127.0.0.1"),
    ("192.168.1.50", {"X-Real-IP": "8.8.8.8"}, "192.168.1.50"),
    (None, {}, "unknown"),
])
def test_client_ip(srv, peer, headers, expected):
    assert srv.client_ip(_req(peer, headers)) == expected


def test_client_ip_header_is_truncated(srv):
    assert len(srv.client_ip(_req("127.0.0.1", {"X-Real-IP": "9" * 5000}))) == 64


# ── state files ───────────────────────────────────────────────────────────

def test_corrupt_state_files_load_empty(srv):
    srv.SESSION_FILE.write_text("{ this is not json")
    srv.REVOKED_FILE.write_text("]]]garbage")
    assert srv._load_sessions() == {}
    assert srv._load_revoked() == {}


def test_missing_state_file_loads_empty(srv):
    assert not srv.SESSION_FILE.exists()
    assert srv._load_sessions() == {}


def test_loader_prunes_expired(srv):
    srv.SESSION_FILE.write_text(json.dumps({"expired": time.time() - 1, "good": time.time() + 3600}))
    assert list(srv._load_sessions()) == ["good"]


def test_atomic_write(srv, tmp_path):
    target = tmp_path / "state.json"
    assert srv._write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert not (tmp_path / "state.json.tmp").exists()


def test_atomic_write_to_unwritable_path_fails_cleanly(srv, tmp_path):
    assert srv._write_json_atomic(tmp_path / "no" / "such" / "dir" / "x.json", {"a": 1}) is False


def test_atomic_write_keeps_old_file_on_failure(srv, tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}')
    assert srv._write_json_atomic(target, {"bad": object()}) is False
    assert json.loads(target.read_text()) == {"old": True}
    assert not (tmp_path / "state.json.tmp").exists()


@pytest.mark.parametrize("sig", ["é", "é" * 64, "\U0001f600"])
def test_non_ascii_signature_rejected_not_raised(srv, sig):
    """compare_digest raises TypeError on non-ASCII str; that was a 500."""
    assert not srv.valid_session(f"v1.{int(time.time()) + 3600}.{sig}")
