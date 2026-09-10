#!/usr/bin/env python3
"""FastAPI backend for funding-arb trading dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Make the project root and scripts/ importable so we can use existing
# trading modules regardless of how the server is launched
# (python server/main.py, python -m server.main, or uvicorn server.main:app).
# ---------------------------------------------------------------------------
_ROOT_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
for _p in (str(_ROOT_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
from server.routes import backtest, positions, scanner, settings  # noqa: E402

# ---------------------------------------------------------------------------
# API authentication (shared-secret token)
# ---------------------------------------------------------------------------
#
# When FARB_API_TOKEN is set in the environment, every /api/* HTTP request
# and the /ws/events WebSocket must present the same shared secret. The
# token is read LAZILY on every request (not cached at import time) so it
# can be provided after startup — e.g. by core.credentials.ensure_env(),
# which runs in the lifespan below. When the token is unset the server
# behaves exactly as before (open API, loopback bind by default) and a
# single warning is logged at startup.

_LOG = logging.getLogger("funding-arb.server")

_TOKEN_ENV = "FARB_API_TOKEN"
_ALLOW_UNAUTH_ENV = "FARB_ALLOW_UNAUTHENTICATED"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _configured_token() -> str:
    """Current shared-secret token (lazily read from the environment)."""
    return os.environ.get(_TOKEN_ENV, "").strip()


def _token_matches(provided: str, expected: str) -> bool:
    """Constant-time token comparison; never raises on odd encodings."""
    try:
        return secrets.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )
    except (UnicodeEncodeError, TypeError):
        return False


def _request_tokens(scope: Scope) -> list[str]:
    """Extract `Authorization: Bearer` / `X-Api-Token` candidates from headers."""
    tokens: list[str] = []
    for name, value in scope.get("headers", []):
        try:
            text = value.decode("latin-1")
        except UnicodeDecodeError:
            continue
        if name == b"x-api-token":
            if text.strip():
                tokens.append(text.strip())
        elif name == b"authorization":
            scheme, _, param = text.partition(" ")
            if scheme.lower() == "bearer" and param.strip():
                tokens.append(param.strip())
    return tokens


class ApiTokenMiddleware:
    """Require the shared-secret token on ``/api/*`` HTTP requests (pure ASGI).

    HTTP scopes only — anything else (WebSocket, lifespan) is passed through
    untouched; ``/ws/events`` authenticates itself via a ``?token=`` query
    parameter inside the endpoint handler because browsers cannot set
    custom headers on the WS handshake.

    Registered so that CORSMiddleware stays the outermost layer
    (starlette's ``add_middleware`` puts the last-added middleware
    outermost, so this one is added first): browser-visible 401 responses
    still carry CORS headers, and OPTIONS preflights are answered by CORS
    directly.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path", "").startswith("/api/"):
            token = _configured_token()
            if token and not any(
                _token_matches(candidate, token)
                for candidate in _request_tokens(scope)
            ):
                response = JSONResponse(
                    {"success": False, "error": "unauthorized"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _validate_bind_host(host: str, token: str, allow_unauth: str) -> str | None:
    """Return an error message if binding non-loopback without auth, else None.

    Pure function (unit-tested in scripts/tests/test_server_auth.py):
    ``host`` is the ``--host`` argument, ``token`` the currently configured
    shared secret ("" if none) and ``allow_unauth`` the raw value of
    FARB_ALLOW_UNAUTHENTICATED ("1" = explicit risk acceptance).
    """
    normalized = host.strip().strip("[]").lower()
    if (
        normalized in _LOOPBACK_HOSTS
        # the whole 127.0.0.0/8 block is loopback per RFC 1122
        or normalized.startswith("127.")
    ):
        return None
    if token:
        return None
    if allow_unauth == "1":
        return None
    return (
        f"refusing to bind {host!r} without authentication: anyone who can "
        f"reach this interface could open live positions and read or inject "
        f"credentials. Set {_TOKEN_ENV} (or bind --host 127.0.0.1), or set "
        f"{_ALLOW_UNAUTH_ENV}=1 to explicitly accept the risk."
    )


# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Simple fan-out WebSocket manager for real-time event push."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._connections.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, event: str, data: dict) -> None:
        msg = json.dumps({"event": event, "data": data})
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


async def _background_scanner_loop() -> None:
    """Periodically run the spread scanner and broadcast results via WS."""
    from server.routes.scanner import _scan_pure_fn, scanner_trigger  # noqa: E402
    from server.routes.settings import _strategy_config  # noqa: E402

    if _scan_pure_fn is None:
        print(
            "[scanner] pure futures scanner unavailable, background loop disabled",
            flush=True,
        )
        return

    print("[scanner] background loop started", flush=True)

    # Track fire-and-forget scans so their exceptions surface in logs and they
    # can be cancelled cleanly on shutdown (instead of being silently dropped).
    bg_tasks: set[asyncio.Task] = set()

    def _spawn(coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        bg_tasks.add(t)

        def _done(task: asyncio.Task) -> None:
            bg_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                print(
                    f"[scanner] background task crashed: {task.exception()!r}",
                    flush=True,
                )

        t.add_done_callback(_done)
        return t

    async def _warm(strategy: str) -> None:
        try:
            r = await scanner_trigger(strategy=strategy)
            if not r.get("success"):
                print(f"[scanner] {strategy} scan: {r.get('error')}", flush=True)
        except Exception as e:
            print(f"[scanner] {strategy} scan failed: {e}", flush=True)

    async def _warmup_carry_unified() -> None:
        await asyncio.gather(_warm("carry"), _warm("unified"), return_exceptions=True)

    # Run initial scans at startup so all three tabs have data on first connect.
    # Pure first (fast, most viewed), then carry/unified warm up concurrently.
    await _warm("pure")
    _spawn(_warmup_carry_unified())

    cycle = 0
    while True:
        try:
            interval = int(_strategy_config.get("scan_interval_sec", 300) or 300)
            await asyncio.sleep(max(30, interval))
            await _warm("pure")
            # Carry/unified are heavier — refresh them every other cycle
            cycle += 1
            if cycle % 2 == 0:
                _spawn(_warm("carry"))
                _spawn(_warm("unified"))
        except asyncio.CancelledError:
            for t in list(bg_tasks):
                t.cancel()
            break
        except Exception as e:
            print(f"[scanner] background scan failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[funding-arb server] scripts dir: {_SCRIPTS_DIR}")
    # Load exchange credentials (keyring / age / json) into os.environ so
    # settings endpoints and live executors see them.
    try:
        from core.credentials import ensure_env  # noqa: E402

        ensure_env()
    except Exception as e:
        print(f"[credentials] ensure_env failed: {e}")
    if not _configured_token():
        _LOG.warning(
            "API authentication disabled — set %s to secure /api and /ws",
            _TOKEN_ENV,
        )
    task = asyncio.create_task(_background_scanner_loop())

    def _log_task_crash(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            print(f"[scanner] background loop crashed: {t.exception()!r}", flush=True)

    task.add_done_callback(_log_task_crash)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print("[funding-arb server] shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="funding-arb API", version="0.1.0", lifespan=lifespan)

# Token auth for /api/* — registered BEFORE CORS (starlette's add_middleware
# puts the last-added middleware outermost), so CORS stays the outermost
# layer: browser-visible 401 responses still carry CORS headers and OPTIONS
# preflights are answered by CORS directly. Must be registered before the
# app starts serving.
app.add_middleware(ApiTokenMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:5173",
        "tauri://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scanner.router, prefix="/api")
app.include_router(positions.router, prefix="/api")
app.include_router(backtest.router, prefix="/api")
app.include_router(settings.router, prefix="/api")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    # Browsers cannot set custom headers on the WS handshake, so the shared
    # secret is accepted as a ?token=<t> query parameter. Closing *before*
    # accept() rejects the handshake outright (uvicorn answers with an HTTP
    # 403; starlette's TestClient surfaces WebSocketDisconnect with our
    # custom close code 4401).
    token = _configured_token()
    if token and not _token_matches(ws.query_params.get("token", "").strip(), token):
        await ws.close(code=4401)
        return
    await manager.connect(ws)
    try:
        while True:
            try:
                text = await asyncio.wait_for(ws.receive_text(), timeout=60)
                if text == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(ws)


async def push_event(event: str, data: dict) -> None:
    """Broadcast an event to all connected WebSocket clients."""
    await manager.broadcast(event, data)


# ---------------------------------------------------------------------------
# Static file serving (Browser mode)
# ---------------------------------------------------------------------------

_WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


def _mount_static(app: FastAPI, dist_dir: Path) -> None:
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    app.mount(
        "/assets", StaticFiles(directory=dist_dir / "assets"), name="static-assets"
    )

    dist_root = dist_dir.resolve()

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        """SPA fallback: Any unknown path returns index.html for Vue Router to handle."""
        file = (dist_root / path).resolve()
        # Contain within dist/ — a path-traversal (../) request must not be
        # allowed to read arbitrary files off disk.
        if file.is_file() and file.is_relative_to(dist_root):
            return FileResponse(file)
        return FileResponse(dist_root / "index.html")


if _WEB_DIST.exists() and (_WEB_DIST / "index.html").exists():
    _mount_static(app, _WEB_DIST)
    _HAS_WEB_UI = True
else:
    _HAS_WEB_UI = False


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Funding Arb Dashboard")
    parser.add_argument(
        "--port", type=int, default=8787, help="Port number (default: 8787)"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen address (default: 127.0.0.1; pass 0.0.0.0 to expose on the LAN)",
    )
    parser.add_argument("--no-reload", action="store_true", help="Disable hot reload")
    args = parser.parse_args()

    # Populate the credentials store into os.environ (the lifespan will do
    # this again, cached) so a FARB_API_TOKEN stored there counts for the
    # bind guard below.
    try:
        from core.credentials import ensure_env  # noqa: E402

        ensure_env()
    except Exception:
        pass

    bind_error = _validate_bind_host(
        args.host,
        _configured_token(),
        os.environ.get(_ALLOW_UNAUTH_ENV, ""),
    )
    if bind_error:
        # Binding a non-loopback interface without auth is a hard failure:
        # it would expose live trading + credential injection to the network.
        print(f"\n  FATAL: {bind_error}\n", file=sys.stderr)
        sys.exit(2)

    mode_info = "Desktop + Web UI" if _HAS_WEB_UI else "API Only (Web UI not built)"
    print("\n  Funding Arb Dashboard")
    print(f"  Mode: {mode_info}")
    print(f"  URL: http://{args.host}:{args.port}")
    if _HAS_WEB_UI:
        print("  Open the above URL in your browser to use the dashboard")
    else:
        print("  Hint: cd web && npm run build to build the Web UI")
    print()

    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        reload_dirs=[str(Path(__file__).resolve().parent)],
    )
