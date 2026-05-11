"""Bearer-token middleware for the MCP server.

We mount FastMCP as an ASGI app and wrap it with this middleware so every
request must present `Authorization: Bearer <MCP_BEARER_TOKEN>`. The token
comes from the env at startup; we read it lazily so tests can monkeypatch.

Rejection bodies are intentionally minimal — never echo the supplied token,
never echo the expected token.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, header_name: str = "authorization") -> None:
        self.app = app
        self.header_name = header_name.lower().encode()

    def _expected_token(self) -> str | None:
        tok = os.environ.get("MCP_BEARER_TOKEN", "").strip()
        return tok or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = self._expected_token()
        if expected is None:
            response = JSONResponse(
                {"error": "server_misconfigured", "detail": "MCP_BEARER_TOKEN not set"},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        supplied = self._extract_token(scope)
        if not supplied or not hmac.compare_digest(supplied, expected):
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _extract_token(self, scope: Scope) -> str | None:
        for k, v in scope.get("headers", []):
            if k == self.header_name:
                val = v.decode("latin-1").strip()
                if val.lower().startswith("bearer "):
                    return val[7:].strip()
                return None
        return None


def wrap(app: Any) -> Any:
    """Convenience wrapper: middleware-wrap an ASGI app."""
    return BearerAuthMiddleware(app)
