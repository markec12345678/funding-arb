#!/usr/bin/env python3
"""Hermetic tests for server/auth.py bearer-token middleware (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server.auth import AuthMiddleware, ENV_VAR  # noqa: E402


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    @app.get("/public")
    async def public():
        return {"ok": True}

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        await ws.accept()
        text = await ws.receive_text()
        if text == "ping":
            await ws.send_text("pong")
        await ws.close()

    return app


@pytest.fixture()
def client():
    return TestClient(_make_app())


def test_disabled_by_default(client, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert client.get("/api/ping").status_code == 200


def test_enabled_missing_token_401(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    r = client.get("/api/ping")
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_bearer_header_ok(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    r = client.get("/api/ping", headers={"Authorization": "Bearer secret-token"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_x_api_token_header_ok(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    r = client.get("/api/ping", headers={"X-Api-Token": "secret-token"})
    assert r.status_code == 200


def test_query_param_ok(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    r = client.get("/api/ping?token=secret-token")
    assert r.status_code == 200


def test_wrong_token_401(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    assert client.get("/api/ping", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/ping?token=wrong").status_code == 401


def test_empty_token_in_request_401(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    assert client.get("/api/ping?token=").status_code == 401


def test_non_api_paths_open_even_when_enabled(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    assert client.get("/public").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_ws_rejected_without_token(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/events"):
            pass


def test_ws_rejected_with_wrong_token(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/events?token=wrong"):
            pass


def test_ws_accepted_with_query_token(client, monkeypatch):
    monkeypatch.setenv(ENV_VAR, "secret-token")
    with client.websocket_connect("/ws/events?token=secret-token") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"


def test_ws_open_when_disabled(client, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with client.websocket_connect("/ws/events") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "pong"
