"""Fixtures for live-device integration tests.

Every test in this directory is marked `integration` (see
`pytest_collection_modifyitems` below) and skipped unless `ASHLY_HOST` is
set. Combined with the default `addopts = -m 'not integration'` in
`pyproject.toml`, this means:

- A bare ``pytest`` run never touches the network.
- ``pytest -m integration`` runs the live tests, but they're skipped
  unless ``ASHLY_HOST`` (and optionally ``ASHLY_PORT``,
  ``ASHLY_USERNAME``, ``ASHLY_PASSWORD``) point at a reachable device.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import aiohttp
import pytest

from custom_components.ashly.client import AshlyClient


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val if val else default


@pytest.fixture(autouse=True)
def _allow_device_sockets():
    """Re-enable sockets and allow the device host for each test.

    `pytest-homeassistant-custom-component` calls
    `socket_allow_hosts(["127.0.0.1"])` + `disable_socket()` in its own
    `pytest_runtest_setup` hook before every test. We undo that here so
    live-device tests can actually reach the device.
    """
    import pytest_socket

    host = os.environ.get("ASHLY_HOST")
    if host:
        pytest_socket.socket_allow_hosts([host, "127.0.0.1"], allow_unix_socket=True)
        pytest_socket.enable_socket()
    yield


@pytest.fixture(scope="session")
def ashly_host() -> str:
    host = _env("ASHLY_HOST")
    if not host:
        pytest.skip("ASHLY_HOST not set; skipping live-device tests")
    return host


@pytest.fixture(scope="session")
def ashly_port() -> int:
    return int(_env("ASHLY_PORT", "8000"))


@pytest.fixture(scope="session")
def ashly_meter_port() -> int:
    """Override for the socket.io port (defaults to 8001).

    Useful when running tests through a localhost TCP proxy on macOS
    Tahoe, which blocks Homebrew Python from reaching LAN addresses
    directly.
    """
    return int(_env("ASHLY_METER_PORT", "8001"))


@pytest.fixture(scope="session")
def ashly_username() -> str:
    return _env("ASHLY_USERNAME", "admin") or "admin"


@pytest.fixture(scope="session")
def ashly_password() -> str:
    return _env("ASHLY_PASSWORD", "secret") or "secret"


@pytest.fixture
async def live_session() -> AsyncGenerator[aiohttp.ClientSession, None]:
    """Per-test aiohttp session with its own cookie jar.

    Uses `ThreadedResolver` to avoid pulling in pycares — keeps things
    consistent with the unit-test client fixture and avoids stray DNS
    threads showing up in any test runner that enforces thread cleanup.
    """
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=aiohttp.ThreadedResolver(),
            force_close=True,
            enable_cleanup_closed=False,
        ),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    try:
        yield session
    finally:
        await session.close()


@pytest.fixture
async def live_client(
    live_session: aiohttp.ClientSession,
    ashly_host: str,
    ashly_port: int,
    ashly_username: str,
    ashly_password: str,
) -> AshlyClient:
    client = AshlyClient(
        host=ashly_host,
        port=ashly_port,
        session=live_session,
        username=ashly_username,
        password=ashly_password,
    )
    await client.async_login()
    return client


def pytest_collection_modifyitems(config, items) -> None:
    """Auto-mark every test in this directory as `integration` and enable
    real socket I/O (the parent pytest-homeassistant-custom-component
    fixtures block sockets globally)."""
    integration = pytest.mark.integration
    enable_socket = pytest.mark.enable_socket
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(integration)
            item.add_marker(enable_socket)
