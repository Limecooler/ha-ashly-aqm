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
