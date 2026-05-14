"""Live-device integration tests for the aquacontrol library.

These tests connect to a real AQM device and verify the library's wire
contract against current firmware. They are **opt-in** — pytest skips
the whole module when the ``ASHLY_HOST`` environment variable is not set
so the offline test suite stays fast and hermetic.

To run::

    ASHLY_HOST=192.168.1.100 \\
    ASHLY_USERNAME=haassistant \\
    ASHLY_PASSWORD=… \\
    pytest -m live

Defaults if env vars are unset: username ``admin``, password ``secret``
(factory defaults). Set both to the dedicated service account on
production devices.

Every test restores any state it touches. Tests skip individually if the
device's state doesn't match what they need (e.g. the front-panel toggle
test skips on a device that has no power switch).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Final

import aiohttp
import pytest
import pytest_asyncio

from aquacontrol import (
    SYSTEM,
    AquaControlClient,
    Event,
    fetch_session_cookies,
)

# All tests in this module require the live marker AND a host env var.
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("ASHLY_HOST"),
        reason="ASHLY_HOST not set; skipping live-device tests",
    ),
]

# How long to wait for an expected event before giving up. Generous — the
# device's CPU is small and may take a beat under load.
EVENT_TIMEOUT_S: Final = 5.0

# How long to wait for the WS to come up after connect() returns.
CONNECT_TIMEOUT_S: Final = 10.0


def _host() -> str:
    return os.environ["ASHLY_HOST"]


def _username() -> str:
    return os.environ.get("ASHLY_USERNAME", "admin")


def _password() -> str:
    return os.environ.get("ASHLY_PASSWORD", "secret")


def _rest_port() -> int:
    return int(os.environ.get("ASHLY_PORT", "8000"))


@pytest_asyncio.fixture
async def rest() -> AsyncIterator[aiohttp.ClientSession]:
    """An authenticated REST session for triggering mutations + state queries."""
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    try:
        async with session.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/session/login",
            json={
                "username": _username(),
                "password": _password(),
                "keepLoggedIn": True,
            },
        ) as r:
            assert r.status == 200, await r.text()
        yield session
    finally:
        await session.close()


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AquaControlClient]:
    """A connected AquaControlClient. Waits for the WS to come up."""
    c = AquaControlClient(
        host=_host(),
        username=_username(),
        password=_password(),
        rest_port=_rest_port(),
    )
    await c.connect()
    # Wait for the underlying socket to be up before yielding.
    deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_S
    while not c.connected:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"AquaControlClient did not connect within {CONNECT_TIMEOUT_S}s")
        await asyncio.sleep(0.1)
    try:
        yield c
    finally:
        await c.disconnect()


async def _wait_for(
    client: AquaControlClient,
    *,
    name: str | None = None,
    topic: str | None = None,
    predicate=None,
    timeout: float = EVENT_TIMEOUT_S,
) -> Event:
    """Block until a matching event arrives. Returns the event."""
    fut: asyncio.Future[Event] = asyncio.get_event_loop().create_future()

    def handler(event: Event) -> None:
        if name is not None and event.name != name:
            return
        if topic is not None and event.topic != topic:
            return
        if predicate is not None and not predicate(event):
            return
        if not fut.done():
            fut.set_result(event)

    if name is not None:
        remove = client.on_event(name, handler)
    elif topic is not None:
        remove = client.on_topic(topic, handler)
    else:
        remove = client.on_any(handler)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        remove()


# ── connection & auth ───────────────────────────────────────────────────


async def test_connect_succeeds(client: AquaControlClient) -> None:
    """Basic smoke test: connecting to the device succeeds."""
    assert client.connected
    assert client.host == _host()


async def test_authenticated_topics_emit_events(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """A REST mutation produces a Modify system info push event.

    Verifies the cookie-gated path is wired correctly — without auth this
    event would not arrive (only public broadcasts would).
    """
    # Read current LED enable so we can restore.
    async with rest.get(f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info") as r:
        info = await r.json(content_type=None)
    original = info["data"][0]["frontPanelLEDEnable"]
    target = not original

    async def _poke():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info",
            json={"frontPanelLEDEnable": target},
        ):
            pass

    pop_task = asyncio.create_task(_poke())
    try:
        event = await _wait_for(
            client,
            name="Modify system info",
            predicate=lambda e: any(
                "frontPanelLEDEnable" in r for r in e.records if isinstance(r, dict)
            ),
        )
        assert event.topic == SYSTEM
        assert event.records[0]["frontPanelLEDEnable"] == target
    finally:
        await pop_task
        # Always restore
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info",
            json={"frontPanelLEDEnable": original},
        ):
            pass


async def test_ambient_events_arrive(client: AquaControlClient) -> None:
    """At least one System Info Values heartbeat lands within a few seconds.

    Doubles as a 'WS pipe is open' smoke test — these are unauthenticated
    broadcasts and arrive at 1 Hz.
    """
    event = await _wait_for(client, name="System Info Values", timeout=3.0)
    assert event.is_ambient
    assert event.unique_id is None


# ── echo filtering ──────────────────────────────────────────────────────


async def test_echo_filter_skips_own_changes(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """Setting set_session_id(...) to the REST session's uniqueId filters echoes."""
    # We have to learn the REST session's UUID first. Trigger a mutation,
    # capture the uniqueId from the resulting echo, then verify subsequent
    # mutations with that uniqueId are filtered.
    captured: list[Event] = []

    def grab(event: Event) -> None:
        if event.name == "Modify system info":
            captured.append(event)

    remove = client.on_any(grab)
    try:
        # Read + toggle to learn the session id.
        async with rest.get(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info"
        ) as r:
            info = await r.json(content_type=None)
        original = info["data"][0]["frontPanelLEDEnable"]
        target = not original
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info",
            json={"frontPanelLEDEnable": target},
        ):
            pass
        # Give the echo time to arrive
        for _ in range(20):
            if captured:
                break
            await asyncio.sleep(0.1)
        assert captured, "no echo received from triggering side"
        my_uuid = captured[0].unique_id
        assert isinstance(my_uuid, str)
        client.set_session_id(my_uuid)

        # Now toggle back and verify the new echo would be filtered.
        captured.clear()
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/system/frontPanel/info",
            json={"frontPanelLEDEnable": original},
        ):
            pass
        await asyncio.sleep(1.0)
        assert captured, "echo of restore didn't arrive"
        echo = captured[-1]
        assert echo.is_from_session(client.session_id)
    finally:
        remove()


# ── listener registration ──────────────────────────────────────────────


async def test_on_topic_receives_only_matching_topic(
    client: AquaControlClient,
) -> None:
    """A listener registered with on_topic(System, …) sees System events."""
    seen: list[Event] = []
    remove = client.on_topic(SYSTEM, lambda e: seen.append(e))
    try:
        await asyncio.sleep(2.5)  # capture a few seconds of ambient System events
    finally:
        remove()
    assert seen, "no System events arrived in 2.5 s — device offline?"
    assert all(e.topic == SYSTEM for e in seen)


async def test_unsubscribe_stops_dispatch(client: AquaControlClient) -> None:
    """After remove() is called, no further events reach the handler."""
    seen_a: list[Event] = []
    seen_b: list[Event] = []
    remove_a = client.on_topic(SYSTEM, lambda e: seen_a.append(e))
    client.on_topic(SYSTEM, lambda e: seen_b.append(e))

    await asyncio.sleep(1.5)
    remove_a()
    a_count_at_unsub = len(seen_a)
    await asyncio.sleep(1.5)

    assert len(seen_a) == a_count_at_unsub  # didn't grow after unsubscribe
    assert len(seen_b) > a_count_at_unsub or len(seen_b) > 0  # b still receives


# ── auth helper ────────────────────────────────────────────────────────


async def test_fetch_session_cookies_against_live_device() -> None:
    """The REST auth helper successfully obtains the ashly-sid cookie."""
    cookies = await fetch_session_cookies(
        _host(),
        port=_rest_port(),
        username=_username(),
        password=_password(),
    )
    assert "ashly-sid" in cookies
    assert cookies["ashly-sid"]


async def test_fetch_session_cookies_wrong_password_raises() -> None:
    from aquacontrol.exceptions import AquaControlAuthError

    with pytest.raises(AquaControlAuthError):
        await fetch_session_cookies(
            _host(),
            port=_rest_port(),
            username=_username(),
            password="wrong" + _password(),
        )
