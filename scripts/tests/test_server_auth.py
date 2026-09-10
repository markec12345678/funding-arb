#!/usr/bin/env python3
"""Auth hardening tests: /api/* token middleware, /ws/events token gate,
secret masking, and the non-loopback bind guard.

The TestClient is intentionally NOT entered as a context manager, so the
app lifespan (credential loading + background scanner loop + network
warm-up) never runs — these tests exercise routing/middleware only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server.main as server_main  # noqa: E402
from server.routes.settings import _mask  # noqa: E402

TOKEN = "unit-test-shared-secret-7f3a"
# 24-word placeholder mnemonic (never a real wallet — "abandon" test vector)
MNEMONIC = " ".join(["abandon"] * 24)

# Bare TestClient (no context manager): requests work, lifespan does NOT
# start — no background scanner loop, no network, no credential side effects.
client = TestClient(server_main.app)


def _no_token(monkeypatch) -> None:
    monkeypatch.delenv("FARB_API_TOKEN", raising=False)


# ─── HTTP middleware ────────────────────────────────────────────────────


def test_api_open_when_no_token(monkeypatch):
    """Backward compat: without FARB_API_TOKEN the API stays open."""
    _no_token(monkeypatch)
    r = client.get("/api/scanner/status")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_api_401_without_header_when_token_set(monkeypatch):
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get("/api/scanner/status")
    assert r.status_code == 401
    assert r.json() == {"success": False, "error": "unauthorized"}


def test_api_401_with_wrong_token(monkeypatch):
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get("/api/scanner/status", headers={"X-Api-Token": "wrong-token"})
    assert r.status_code == 401
    assert r.json() == {"success": False, "error": "unauthorized"}


def test_api_200_with_x_api_token(monkeypatch):
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get("/api/scanner/status", headers={"X-Api-Token": TOKEN})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_api_200_with_bearer_token(monkeypatch):
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get(
        "/api/scanner/status", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_api_401_with_wrong_scheme(monkeypatch):
    """Only the Bearer scheme counts for the Authorization header."""
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get(
        "/api/scanner/status", headers={"Authorization": f"Basic {TOKEN}"}
    )
    assert r.status_code == 401


def test_token_read_lazily_per_request(monkeypatch):
    """A token set AFTER startup (e.g. by ensure_env) takes effect immediately."""
    _no_token(monkeypatch)
    assert client.get("/api/scanner/status").status_code == 200  # auth off
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)  # token appears later
    assert client.get("/api/scanner/status").status_code == 401  # now enforced


def test_non_api_path_not_blocked(monkeypatch):
    """Static/SPA routes are untouched by the middleware (404, never 401)."""
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    r = client.get("/")
    assert r.status_code != 401


# ─── WebSocket ──────────────────────────────────────────────────────────


def test_ws_rejected_without_token(monkeypatch):
    """No ?token= (token configured) → handshake rejected with code 4401.

    The endpoint closes BEFORE accepting, so starlette's TestClient raises
    WebSocketDisconnect when entering the connection context.
    """
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/events"):
            pass  # pragma: no cover - never accepted
    assert exc_info.value.code == 4401


def test_ws_rejected_with_wrong_token(monkeypatch):
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/ws/events?token=wrong-token"):
            pass  # pragma: no cover - never accepted
    assert exc_info.value.code == 4401


def test_ws_connects_and_echoes_with_valid_token(monkeypatch):
    """Valid token → connection accepted; keepalive protocol still works."""
    monkeypatch.setenv("FARB_API_TOKEN", TOKEN)
    with client.websocket_connect(f"/ws/events?token={TOKEN}") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_ws_open_when_no_token(monkeypatch):
    """Backward compat: without FARB_API_TOKEN the WS stays open."""
    _no_token(monkeypatch)
    with client.websocket_connect("/ws/events") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


# ─── Secret masking ─────────────────────────────────────────────────────


def test_mask_is_constant():
    """_mask returns the same constant regardless of input — no length or
    content information leaks (previously first4...last4 of secrets)."""
    samples = ["short", MNEMONIC, "", "a", "0xdeadbeefcafe1234", "ABCD1234"]
    outputs = {_mask(s) for s in samples}
    assert len(outputs) == 1  # identical output for every input
    out = outputs.pop()
    assert out == "••••••••"
    for s in samples:
        if s:
            assert s not in out  # no substring of the input is echoed
            assert not any(ch in out for ch in s)  # not even single chars
    assert set(out) == {"•"}  # and nothing but bullet characters


def test_wallet_status_masks_secrets(monkeypatch):
    """Route-level check: configured secret never appears in the response,
    but field names / configured-state survive for the UI."""
    _no_token(monkeypatch)
    monkeypatch.setenv("BINANCE_API_KEY", "SUPERSECRET-KEY-DO-NOT-LEAK-1234")
    # Half-configured (secret missing) → connected=False → no balance fetch.
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    r = client.get("/api/settings/wallet/status?venue=binance")
    assert r.status_code == 200
    body = json.dumps(r.json())
    assert "SUPERSECRET" not in body
    assert "DO-NOT-LEAK" not in body
    binance = r.json()["data"]["binance"]
    assert binance["fields_masked"]["BINANCE_API_KEY"] == "••••••••"
    # Field names / configured booleans preserved so the UI still works
    assert "fields_masked" in binance
    assert binance["connected"] is False


# ─── Bind guard ─────────────────────────────────────────────────────────


def test_bind_guard_loopback_without_token_ok():
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        assert server_main._validate_bind_host(host, "", "") is None


def test_bind_guard_public_without_token_fails():
    err = server_main._validate_bind_host("0.0.0.0", "", "")
    assert err is not None
    assert "FARB_API_TOKEN" in err
    assert "FARB_ALLOW_UNAUTHENTICATED" in err


def test_bind_guard_public_with_token_ok():
    assert server_main._validate_bind_host("0.0.0.0", TOKEN, "") is None


def test_bind_guard_public_with_allow_unauth_ok():
    assert server_main._validate_bind_host("0.0.0.0", "", "1") is None
    # Only the exact "1" opt-out counts — anything else still fails.
    assert server_main._validate_bind_host("0.0.0.0", "", "0") is not None
    assert server_main._validate_bind_host("0.0.0.0", "", "true") is not None
