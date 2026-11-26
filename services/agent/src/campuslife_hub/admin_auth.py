"""Who may use the admin listener (E18), checked for every request before any route, error page or 404.

The listener binds 127.0.0.1 only and is never put behind tailscale serve, yet where a request
comes from authorises nothing (AGENTS.md): a process on this machine, or a request that came in
through serve, looks the same. A request is let in when
- its Host names this listener, so a page whose DNS name rebinds to 127.0.0.1 is refused,
- it is an admin API request signed with the admin token, as the CLI makes them, or a page request
  with the session cookie a one-time login link set in the browser, and
- under a session, a request that changes something carries this listener's Origin.

The admin token itself is never sent. The CLI finds the Hub by the port it left in admin.port,
and another program may hold that port by then, so the Hub proves itself first: the CLI posts a
nonce of its own to /api/hello and the Hub answers with a nonce of its own and an HMAC of both
under the token. Only then does the CLI send its request, signed with an HMAC under the token of
the Hub's nonce, the method, the path and query and the body. The Hub takes each of its nonces
once, so whoever receives a signed request can neither learn the token from it nor sign another.
Routes added to the app, #19's pairing and revoke among them, pass the same checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import cookie_parser
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .redaction import Redactor

SESSION_COOKIE = "campuslife_admin"
LOGIN_CODE_LIFETIME = timedelta(minutes=2)
SESSION_LIFETIME = timedelta(hours=12)
SECRET_BYTES = 32
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
HELLO_PATH = "/api/hello"
PUBLIC_PATHS = frozenset({"/login", "/static/admin.css", HELLO_PATH})
API_PREFIX = "/api/"
AUTH_SCOPE_KEY = "campuslife.admin"
SIGNED_SCHEME = "CampusLife"
HELLO_LIFETIME = timedelta(minutes=1)
"""How long a nonce the Hub gave in its hello may sign a request."""
MAX_PENDING_HELLOS = 256
MAX_SIGNED_BODY_BYTES = 1024 * 1024
CLIENT_NONCE = re.compile("[0-9a-f]{64}")

PAGE_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}
"""Set on every response that has not set its own; an original sets a sandbox policy of its own."""


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _mac(token: str, purpose: str, *parts: str) -> str:
    return hmac.new(token.encode(), "\0".join((purpose, *parts)).encode(), hashlib.sha256).hexdigest()


def hub_proof(token: str, client_nonce: str, hub_nonce: str) -> str:
    """The Hub's answer to a client's hello, which only a holder of the token can give."""
    return _mac(token, "campuslife-hub-proof", client_nonce, hub_nonce)


def signed_authorization(token: str, hub_nonce: str, method: str, path: str, query: str, body: bytes) -> str:
    """The authorization header of one admin API request, under a nonce the Hub gave in its hello."""
    signature = _mac(token, "campuslife-admin-request", hub_nonce, method, path, query, hashlib.sha256(body).hexdigest())
    return f"{SIGNED_SCHEME} {hub_nonce}.{signature}"


class AdminAuth:
    """The admin token, the nonces that sign admin API requests with it, and the login codes and browser sessions
    it opens; all but the token live in memory, so a restarted Hub asks for a new login."""

    def __init__(self, token: str, redactor: Redactor, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._token = redactor.register(token)
        self._redactor = redactor
        self._clock = clock
        self._codes: dict[str, datetime] = {}
        self._sessions: dict[str, datetime] = {}
        self._hellos: dict[str, datetime] = {}

    def _new_secret(self, into: dict[str, datetime], lifetime: timedelta) -> str:
        now = self._clock()
        for digest in [digest for digest, until in into.items() if until <= now]:
            del into[digest]
        secret = self._redactor.register(secrets.token_urlsafe(SECRET_BYTES))
        into[_digest(secret)] = now + lifetime
        return secret

    def hello(self, client_nonce: object) -> tuple[str, str] | None:
        """A nonce of the Hub's that may sign one request within HELLO_LIFETIME, and the Hub's proof for
        client_nonce; None when client_nonce is not 64 hex digits."""
        if not isinstance(client_nonce, str) or not CLIENT_NONCE.fullmatch(client_nonce):
            return None
        now = self._clock()
        for nonce in [nonce for nonce, until in self._hellos.items() if until <= now]:
            del self._hellos[nonce]
        while len(self._hellos) >= MAX_PENDING_HELLOS:
            del self._hellos[next(iter(self._hellos))]
        hub_nonce = secrets.token_urlsafe(SECRET_BYTES)
        self._hellos[hub_nonce] = now + HELLO_LIFETIME
        return hub_nonce, hub_proof(self._token, client_nonce, hub_nonce)

    def is_signed(self, authorization: str, method: str, path: str, query: str, body: bytes) -> bool:
        """Whether authorization signs this request under an unused, unexpired nonce of the Hub's, which is then used."""
        scheme, _, credentials = authorization.partition(" ")
        hub_nonce, _, _ = credentials.partition(".")
        until = self._hellos.pop(hub_nonce, None)
        if scheme != SIGNED_SCHEME or until is None or until <= self._clock():
            return False
        expected = signed_authorization(self._token, hub_nonce, method, path, query, body)
        return hmac.compare_digest(authorization.encode(), expected.encode())

    def issue_login_code(self) -> str:
        """A code that opens one session within LOGIN_CODE_LIFETIME."""
        return self._new_secret(self._codes, LOGIN_CODE_LIFETIME)

    def start_session(self, code: str) -> str | None:
        """A new session for an unused, unexpired code, which is then used; None for any other."""
        until = self._codes.pop(_digest(code), None)
        if until is None or until <= self._clock():
            return None
        return self._new_secret(self._sessions, SESSION_LIFETIME)

    def has_session(self, session: str) -> bool:
        until = self._sessions.get(_digest(session))
        return until is not None and self._clock() < until

    def end_session(self, session: str) -> None:
        self._sessions.pop(_digest(session), None)


def session_cookie(headers: Headers) -> str | None:
    """The session in the cookie header, read as browsers write it: a malformed cookie another app on 127.0.0.1
    set (cookies are shared across its ports) does not hide it."""
    return cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE) or None


async def _body(receive: Receive) -> bytes | None:
    """The request body, up to MAX_SIGNED_BODY_BYTES; None past that or when the client leaves."""
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunks.append(message.get("body", b""))
        size += len(chunks[-1])
        if size > MAX_SIGNED_BODY_BYTES:
            return None
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replaying(body: bytes, receive: Receive) -> Receive:
    replayed = False

    async def replay() -> Message:
        nonlocal replayed
        if replayed:
            return await receive()
        replayed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


async def _refuse(send: Send, status: int, reason: str, *, api: bool) -> None:
    body = json.dumps({"error": reason}).encode() if api else f"<!doctype html><title>{status}</title><p>{reason}</p>".encode()
    headers = {**PAGE_HEADERS, "content-type": "application/json" if api else "text/html; charset=utf-8", "content-length": str(len(body))}
    await send({"type": "http.response.start", "status": status, "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
    await send({"type": "http.response.body", "body": body})


class AdminGuard:
    def __init__(self, app: ASGIApp, auth: AdminAuth, hosts: Iterable[str]) -> None:
        self.app = app
        self.auth = auth
        self.hosts = frozenset(hosts)
        self.origins = frozenset(f"http://{host}" for host in self.hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        headers, path = Headers(scope=scope), scope["path"]
        api = path.startswith(API_PREFIX)
        if headers.get("host") not in self.hosts:
            await _refuse(send, 400, "this listener answers only to its own host name", api=api)
            return
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            await _refuse(send, 403, "requests from other origins are refused", api=api)
            return
        authorised = None
        if api and "authorization" in headers:
            body = await _body(receive)
            if body is None:
                await _refuse(send, 413, "a signed request carries at most 1 MiB", api=True)
                return
            receive = _replaying(body, receive)
            query = scope.get("query_string", b"").decode("latin-1")
            authorised = "signed" if self.auth.is_signed(headers["authorization"], scope["method"], path, query, body) else None
        elif not api:
            session = session_cookie(headers)
            authorised = "session" if session is not None and self.auth.has_session(session) else None
        if authorised is None and path not in PUBLIC_PATHS:
            if api:
                await _refuse(send, 401, "the admin API takes requests signed with the admin token", api=True)
            else:
                await _refuse(send, 401, "not logged in: open the local UI with <code>campuslife-hub open</code>", api=False)
            return
        if authorised == "session" and scope["method"] not in SAFE_METHODS and origin is None:
            await _refuse(send, 403, "a change without an Origin is refused", api=False)
            return
        scope[AUTH_SCOPE_KEY] = authorised

        async def with_page_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in PAGE_HEADERS.items():
                    response_headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, with_page_headers)
