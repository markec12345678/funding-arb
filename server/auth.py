#!/usr/bin/env python3
"""Optional bearer-token authentication for the funding-arb API server.

Enabled by setting FARB_API_TOKEN in the environment (or .env) to a non-empty
string. When enabled, every /api/* route and the /ws/events WebSocket requires:

  HTTP:        Authorization: Bearer <token>   (preferred)
               X-Api-Token: <token>
               ?token=<token>                  (curl / browser convenience)
  WebSocket:   ?token=<token>                  (browsers cannot set WS headers)

Everything else (static SPA files, docs) stays open — they contain no secrets.
When FARB_API_TOKEN is unset or empty, auth is disabled and behaviour is
byte-for-byte identical to before (paper/demo setups pay no toll).

Token comparison is timing-safe (hmac.compare_digest).
"""

from __future__ import annotations

import hmac
import os
from urllib.parse import parse_qs

from fastapi.responses import JSONResponse

ENV_VAR = "FARB_API_TOKEN"

# Only the data plane is protected. Static assets, /docs, /redoc and
# /openapi.json are public: the API surface of an open-source project is not a
# secret, and the SPA shell must load before the user can authenticate.
PROTECTED_PREFIXES = ("/api/", "/ws/")

_WS_UNAUTHORIZED_CODE = 4401  # app-defined: "unauthorized" (1008 is generic)


def auth_enabled() -> bool:
    """Auth is on only when FARB_API_TOKEN is set to a non-empty string."""
    return bool(_expected_token())


def _expected_token() -> str:
    return os.environ.get(ENV_VAR, "").strip()


def verify_token(provided: str | None) -> bool:
    """Timing-safe comparison; empty provided never matches a non-empty token."""
    expected = _expected_token()
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def _headers(scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def _query_token(scope) -> str | None:
    qs = scope.get("query_string", b"").decode("latin-1")
    if not qs:
        return None
    values = parse_qs(qs).get("token")
    return values[0] if values else None


def _bearer_token(scope) -> str | None:
    auth = _headers(scope).get(b"authorization")
    if not auth:
        return None
    parts = auth.decode("latin-1").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _api_token_header(scope) -> str | None:
    raw = _headers(scope).get(b"x-api-token")
    return raw.decode("latin-1").strip() if raw else None


def extract_token(scope) -> str | None:
    """Authorization: Bearer > X-Api-Token > ?token= (first hit wins)."""
    return _bearer_token(scope) or _api_token_header(scope) or _query_token(scope)


def is_protected_path(path: str) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p) for p in PROTECTED_PREFIXES)


class AuthMiddleware:
    """Pure ASGI middleware — covers HTTP and WebSocket scopes alike."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not is_protected_path(path) or not auth_enabled():
            await self.app(scope, receive, send)
            return

        if verify_token(extract_token(scope)):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            # Per ASGI spec, sending websocket.close before accepting rejects
            # the handshake.
            try:
                await send(
                    {
                        "type": "websocket.close",
                        "code": _WS_UNAUTHORIZED_CODE,
                        "reason": "unauthorized",
                    }
                )
            except Exception:
                pass
            return

        response = JSONResponse(
            {"detail": "unauthorized", "hint": f"set {ENV_VAR} on the server and send it as a Bearer token"},
            status_code=401,
        )
        await response(scope, receive, send)
