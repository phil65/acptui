"""``acptui serve``: acptui in the browser, behind a token.

textual-serve runs the command for every browser tab, but has no authentication and
accepts WebSocket connections from any site (browsers do not apply CORS to WebSockets).
:class:`SecureServer` adds both with an aiohttp middleware, like Jupyter does:

- A random token per run (or ``--token`` / ``$ACPTUI_SERVE_TOKEN``) is required for every
  request: in the URL (``?token=...``) the first time, then from an HTTP-only,
  ``SameSite=Strict`` cookie the page sets (the page's own WebSocket and static files
  send it). ``--no-token`` is only allowed on a loopback address.
- WebSocket upgrades from another origin are refused (their ``Origin`` must be the
  server's own, or ``--public-url``'s).
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from aiohttp import web
from textual_serve.server import Server


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


LOOPBACK_NAMES = frozenset({"localhost"})


def is_loopback(host: str) -> bool:
    """Whether ``host`` only reaches this machine."""
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def new_token() -> str:
    """A random token for one run."""
    return secrets.token_urlsafe(24)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of a URL (lower case)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


class SecureServer(Server):
    """textual-serve's server with a token and an Origin check."""

    def __init__(
        self,
        command: str,
        *,
        host: str = "localhost",
        port: int = 8000,
        title: str | None = None,
        public_url: str | None = None,
        token: str | None = None,
    ) -> None:
        super().__init__(command, host=host, port=port, title=title, public_url=public_url)
        self.token = token
        self.cookie_name = f"acptui_token_{port}"  # cookies ignore ports: one per server

    @property
    def url(self) -> str:
        """The URL to open (with the token)."""
        return f"{self.public_url}/?token={self.token}" if self.token else f"{self.public_url}/"

    async def _make_app(self) -> web.Application:
        app = await super()._make_app()
        app.middlewares.append(self._guard)
        return app

    def origin_allowed(self, origin: str, request: web.Request) -> bool:
        """Whether a WebSocket from ``origin`` may connect (same origin or the public URL)."""
        origin = origin.lower()
        scheme = "https" if request.secure else "http"
        return origin in {origin_of(self.public_url), f"{scheme}://{request.host}".lower()}

    def _token_ok(self, request: web.Request) -> bool:
        if self.token is None:
            return True
        given = request.query.get("token") or request.cookies.get(self.cookie_name) or ""
        return hmac.compare_digest(given.encode(), self.token.encode())

    @web.middleware
    async def _guard(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            origin = request.headers.get("Origin")
            if origin is not None and not self.origin_allowed(origin, request):
                raise web.HTTPForbidden(text="WebSocket from another origin")
        if not self._token_ok(request):
            raise web.HTTPUnauthorized(
                text="acptui serve: open the URL with the token it printed (?token=...)."
            )
        response = await handler(request)
        if self.token is not None and "token" in request.query and not response.prepared:
            response.set_cookie(
                self.cookie_name, self.token, httponly=True, samesite="Strict", path="/"
            )
        return response

    async def on_startup(self, app: web.Application) -> None:
        self.console.print(f"acptui serve: {self.command!r}")
        self.console.print(f"Open [bold]{self.url}[/bold]", highlight=False)
        if self.token is None:
            self.console.print("[yellow]No token: anyone on this machine can connect.")
        self.console.print("[cyan]Press Ctrl+C to quit")
