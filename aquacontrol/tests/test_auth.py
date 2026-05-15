"""Tests for the auth module (REST login → cookie extraction)."""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from aquacontrol.auth import cookie_header, fetch_session_cookies
from aquacontrol.exceptions import (
    AquaControlAuthError,
    AquaControlConnectionError,
    AquaControlProtocolError,
)

LOGIN_URL = "http://192.168.1.100:8000/v1.0-beta/session/login"


async def test_fetch_session_cookies_success():
    """A 200 response with a Set-Cookie header yields the cookie dict."""
    with aioresponses() as m:
        m.post(
            LOGIN_URL,
            status=200,
            payload={"success": True, "data": [{"loggedIn": True}]},
            headers={"Set-Cookie": "ashly-sid=abc123-uuid; Path=/"},
        )
        cookies = await fetch_session_cookies(
            "192.168.1.100", username="admin", password="secret"
        )
        assert "ashly-sid" in cookies
        assert cookies["ashly-sid"] == "abc123-uuid"


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_fetch_session_cookies_auth_failures(status):
    """4xx auth-class responses raise AquaControlAuthError."""
    with aioresponses() as m:
        m.post(LOGIN_URL, status=status, body="forbidden")
        with pytest.raises(AquaControlAuthError):
            await fetch_session_cookies(
                "192.168.1.100", username="admin", password="wrong"
            )


async def test_fetch_session_cookies_protocol_error_on_5xx():
    with aioresponses() as m:
        m.post(LOGIN_URL, status=500, body="boom")
        with pytest.raises(AquaControlProtocolError):
            await fetch_session_cookies(
                "192.168.1.100", username="admin", password="secret"
            )


async def test_fetch_session_cookies_connection_error():
    with aioresponses() as m:
        m.post(LOGIN_URL, exception=aiohttp.ClientConnectionError("refused"))
        with pytest.raises(AquaControlConnectionError):
            await fetch_session_cookies(
                "192.168.1.100", username="admin", password="secret"
            )


def test_cookie_header_renders_correctly():
    """Multiple cookies are joined with `; ` per RFC 6265."""
    h = cookie_header({"ashly-sid": "abc", "extra": "val"})
    # dict insertion order is preserved on Python 3.7+
    assert h == "ashly-sid=abc; extra=val"


def test_cookie_header_handles_empty_dict():
    assert cookie_header({}) == ""


async def test_fetch_session_cookies_uses_supplied_session():
    """A supplied session is reused (not opened/closed by us)."""
    import aiohttp

    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        with aioresponses() as m:
            m.post(
                LOGIN_URL,
                status=200,
                payload={"success": True},
                headers={"Set-Cookie": "ashly-sid=xyz; Path=/"},
            )
            cookies = await fetch_session_cookies(
                "192.168.1.100",
                username="admin",
                password="secret",
                session=s,
            )
            assert cookies.get("ashly-sid") == "xyz"
        # The session is still open — we didn't close it.
        assert not s.closed
    finally:
        await s.close()


async def test_fetch_session_cookies_supports_timeout():
    """asyncio.TimeoutError surfaces as AquaControlTimeoutError."""

    from aquacontrol.exceptions import AquaControlTimeoutError

    with aioresponses() as m:
        m.post(LOGIN_URL, exception=TimeoutError())
        with pytest.raises(AquaControlTimeoutError):
            await fetch_session_cookies(
                "192.168.1.100", username="admin", password="secret"
            )


async def test_fetch_session_cookies_merges_jar_with_response_cookies():
    """A cookie present only in the jar (not on the response) is still returned."""
    from unittest.mock import patch

    import aiohttp

    from aquacontrol import auth as auth_mod

    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        with (
            aioresponses() as m,
            patch.object(
                auth_mod,
                "_cookies_from_jar",
                return_value={"jar-only": "value", "ashly-sid": "should-not-override"},
            ),
        ):
            m.post(
                LOGIN_URL,
                status=200,
                payload={"success": True},
                headers={"Set-Cookie": "ashly-sid=from-response; Path=/"},
            )
            cookies = await fetch_session_cookies(
                "192.168.1.100",
                username="admin",
                password="secret",
                session=s,
            )
            assert cookies.get("ashly-sid") == "from-response"
            assert cookies.get("jar-only") == "value"
    finally:
        await s.close()


async def test_fetch_session_cookies_handles_unreadable_body():
    """If we hit a 5xx with a body that fails to decode, the protocol-error
    message uses a placeholder instead of raising on the read."""
    from unittest.mock import patch

    from aquacontrol.exceptions import AquaControlProtocolError

    with (
        aioresponses() as m,
        patch("aquacontrol.auth._read_body_safely", return_value="<body unreadable>"),
    ):
        m.post(LOGIN_URL, status=503, body="unused")
        with pytest.raises(AquaControlProtocolError, match="unreadable"):
            await fetch_session_cookies(
                "192.168.1.100", username="admin", password="secret"
            )


async def test_read_body_safely_returns_placeholder_on_clienterror():
    """_read_body_safely covers the inner exception branch directly."""
    from unittest.mock import AsyncMock, MagicMock

    import aiohttp

    from aquacontrol.auth import _read_body_safely

    resp = MagicMock()
    resp.text = AsyncMock(side_effect=aiohttp.ClientError("decode failure"))
    assert await _read_body_safely(resp) == "<body unreadable>"


def test_looks_like_private_address():
    """The cleartext-HTTP warning fires for public IPs and DNS names, not
    for RFC 1918 / loopback / IPv6 unique-local addresses."""
    from aquacontrol.auth import _looks_like_private_address

    # Private — no warning
    assert _looks_like_private_address("127.0.0.1")
    assert _looks_like_private_address("localhost")
    assert _looks_like_private_address("192.168.1.50")
    assert _looks_like_private_address("10.0.0.1")
    assert _looks_like_private_address("172.16.0.1")
    assert _looks_like_private_address("172.31.255.254")
    assert _looks_like_private_address("fc00::1")
    assert _looks_like_private_address("fd12:3456::1")

    # Public / unknown — warn
    assert not _looks_like_private_address("8.8.8.8")
    assert not _looks_like_private_address("my-aqm.example.com")
    assert not _looks_like_private_address("172.32.0.1")  # outside 16-31 block


async def test_warning_logged_for_non_private_host(caplog):
    """Connecting to a non-private host emits a security warning."""
    import logging

    with aioresponses() as m, caplog.at_level(logging.WARNING):
        m.post(
            "http://aqm.example.com:8000/v1.0-beta/session/login",
            status=200,
            payload={"success": True},
            headers={"Set-Cookie": "ashly-sid=x"},
        )
        await fetch_session_cookies("aqm.example.com", username="u", password="p")
    assert any("cleartext HTTP" in r.message for r in caplog.records)


async def test_no_warning_for_private_host(caplog):
    """Connecting to a private host does NOT emit the cleartext warning."""
    import logging

    with aioresponses() as m, caplog.at_level(logging.WARNING):
        m.post(
            "http://192.168.1.50:8000/v1.0-beta/session/login",
            status=200,
            payload={"success": True},
            headers={"Set-Cookie": "ashly-sid=x"},
        )
        await fetch_session_cookies("192.168.1.50", username="u", password="p")
    assert not any("cleartext HTTP" in r.message for r in caplog.records)
