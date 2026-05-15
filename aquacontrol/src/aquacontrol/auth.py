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
import logging
from collections.abc import Iterable
from typing import Any, Final

import aiohttp

from .exceptions import (
    AquaControlAuthError,
    AquaControlConnectionError,
    AquaControlProtocolError,
    AquaControlTimeoutError,
)

_LOGGER = logging.getLogger(__name__)

_LOGIN_PATH: Final = "/v1.0-beta/session/login"
_DEFAULT_TIMEOUT_S: Final = 10.0

# Hosts considered safe for cleartext HTTP without a "transport is unencrypted"
# warning. AquaControl devices speak only HTTP/WS, so the warning would
# otherwise fire on every legitimate use; we suppress it for loopback and
# RFC 1918 private ranges that are typical for on-prem AV installations.
_LOOPBACK_PREFIXES: Final = ("127.", "::1", "localhost")
_PRIVATE_NETWORK_PREFIXES: Final = ("10.", "172.16.", "172.17.", "172.18.", "172.19.",
                                    "172.20.", "172.21.", "172.22.", "172.23.",
                                    "172.24.", "172.25.", "172.26.", "172.27.",
                                    "172.28.", "172.29.", "172.30.", "172.31.",
                                    "192.168.", "fc", "fd")


def _looks_like_private_address(host: str) -> bool:
    """Best-effort check: is `host` a loopback or RFC 1918 private address?

    Returns True for typical home/AV-rack LAN ranges and loopback. Hostnames
    (DNS names) are NOT considered private — they could resolve anywhere, so
    we warn for them too.
    """
    low = host.lower()
    return low.startswith(_LOOPBACK_PREFIXES) or low.startswith(_PRIVATE_NETWORK_PREFIXES)


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
    if not _looks_like_private_address(host):
        # The device speaks only plaintext HTTP/WS — there is no TLS path
        # to a remote AquaControl. A non-private host means the operator
        # is reaching across an untrusted network, where the cookie + the
        # login credentials are sniffable. Warn loudly; don't refuse.
        _LOGGER.warning(
            "Connecting to AquaControl device over cleartext HTTP at %s — "
            "credentials and session cookie travel unencrypted. "
            "Tunnel via VPN or SSH for production deployments.",
            host,
        )
    own_session = session is None
    if session is None:
        # unsafe=True permits cookies on non-HTTPS responses. The AQM device
        # only speaks plaintext HTTP on its REST + WS ports, so the standard
        # "drop cookies set over HTTP" jar default would discard the very
        # ashly-sid cookie the rest of the library depends on. Safe in this
        # narrow context — see docs/SECURITY-API.md.
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
