"""REST authentication helper.

The AquaControl device's Socket.IO server on port 8001 honours the
``ashly-sid`` cookie returned by ``POST /v1.0-beta/session/login`` on
port 8000. Without that cookie, the WebSocket connection succeeds but
only public broadcasts (Channel Meters, System Info Values, etc.) are
delivered — every state-change event is silently dropped. See
docs/WEBSOCKET-API.md §1.1 for the gating rationale.

This module performs just enough REST to obtain the session cookie. The
HA integration's existing REST client owns the full ``/v1.0-beta/*``
surface; nothing else lives here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any, Final

import aiohttp

from .exceptions import (
    AquaControlAuthError,
    AquaControlConnectionError,
    AquaControlProtocolError,
    AquaControlTimeoutError,
)

_LOGIN_PATH: Final = "/v1.0-beta/session/login"
_DEFAULT_TIMEOUT_S: Final = 10.0


async def fetch_session_cookies(
    host: str,
    *,
    port: int = 8000,
    username: str,
    password: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, str]:
    """Authenticate against the device and return its session cookies.

    Returns a ``{name: value}`` mapping of cookies set by the login
    response. Callers typically pass the rendered header back into
    :class:`aquacontrol.stream.StreamConnection` via ``Cookie:``.

    Pass an existing :class:`aiohttp.ClientSession` to reuse connection
    pooling (e.g. when sharing with another REST client); leave as
    ``None`` to open a one-shot session that's closed on exit.
    """
    own_session = session is None
    if session is None:
        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        try:
            url = f"http://{host}:{port}{_LOGIN_PATH}"
            async with session.post(
                url,
                json={
                    "username": username,
                    "password": password,
                    "keepLoggedIn": True,
                },
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                # Device returns 400 for credentials that fail its
                # alphanumeric schema, 401/403 for valid-format-but-wrong
                # creds. Treat all of those as auth failures.
                if resp.status in (400, 401, 403):
                    raise AquaControlAuthError(
                        f"Authentication failed (HTTP {resp.status})"
                    )
                if resp.status >= 400:
                    body = await _read_body_safely(resp)
                    raise AquaControlProtocolError(
                        f"Login returned HTTP {resp.status}: {body[:200]}"
                    )
                # Capture cookies from BOTH paths: the response's own
                # cookies attribute (faster, no jar dependency) and the
                # jar (catches cookies set via Set-Cookie on URL forms
                # the jar handles but resp.cookies doesn't surface).
                cookies: dict[str, str] = {}
                for key, morsel in resp.cookies.items():
                    cookies[str(key)] = morsel.value
                for cookie in _cookies_from_jar(session.cookie_jar).items():
                    cookies.setdefault(cookie[0], cookie[1])
                return cookies
        except asyncio.TimeoutError as err:  # noqa: UP041 — broad py3.10 compat
            raise AquaControlTimeoutError(
                f"Login to {host}:{port} timed out after {timeout_s}s"
            ) from err
        except aiohttp.ClientError as err:
            raise AquaControlConnectionError(
                f"Cannot connect to {host}:{port}"
            ) from err
    finally:
        if own_session:
            await session.close()


async def _read_body_safely(resp: aiohttp.ClientResponse) -> str:
    try:
        return await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError):
        return "<body unreadable>"


def _cookies_from_jar(jar: Iterable[Any]) -> dict[str, str]:
    """Extract ``{key: value}`` from any iterable yielding cookie morsels.

    aiohttp's :class:`~aiohttp.CookieJar` is iterable; tests can substitute
    a list of morsels with ``.key`` and ``.value`` attributes.
    """
    return {c.key: c.value for c in jar}


def cookie_header(cookies: dict[str, str]) -> str:
    """Render a dict of cookies into a ``Cookie:`` header value."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
