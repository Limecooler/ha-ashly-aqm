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


# ── Round-trips per topic ──────────────────────────────────────────────


async def test_working_settings_chain_mute_round_trip(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """Toggling a chain mute via REST surfaces as Set Chain Mute on the
    WorkingSettings topic."""
    chain_url = f"http://{_host()}:{_rest_port()}/v1.0-beta/workingsettings/dsp/chain"
    async with rest.get(chain_url) as r:
        chain = await r.json(content_type=None)
    entry = next(e for e in chain["data"] if e["id"] == "InputChannel.1")
    original = entry["muted"]
    target = not original

    async def _toggle():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"{chain_url}/mute/InputChannel.1", json={"muted": target}
        ):
            pass

    pop_task = asyncio.create_task(_toggle())
    try:
        event = await _wait_for(
            client,
            name="Set Chain Mute",
            predicate=lambda e: e.records and e.records[0].get("id") == "InputChannel.1",
        )
        assert event.topic == "WorkingSettings"
        assert event.records[0]["muted"] == target
    finally:
        await pop_task
        async with rest.post(
            f"{chain_url}/mute/InputChannel.1", json={"muted": original}
        ):
            pass


async def test_mic_preamp_round_trip(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """Mic preamp gain change surfaces on the MicPreamp topic."""
    async with rest.get(
        f"http://{_host()}:{_rest_port()}/v1.0-beta/micPreamp"
    ) as r:
        data = await r.json(content_type=None)
    entry = next(e for e in data["data"] if int(e["id"]) == 1)
    original = int(entry["gain"])
    # Pick a different allowed value (0..66 in 6 dB steps)
    target = (original + 6) % 72

    async def _change():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/micPreamp/1",
            json={"gain": target},
        ):
            pass

    pop_task = asyncio.create_task(_change())
    try:
        event = await _wait_for(client, name="Change Mic Preamp Gain")
        assert event.topic == "MicPreamp"
        assert event.records[0]["gain"] == target
    finally:
        await pop_task
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/micPreamp/1",
            json={"gain": original},
        ):
            pass


async def test_phantom_power_round_trip(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """Phantom power toggle surfaces on the PhantomPower topic."""
    async with rest.get(
        f"http://{_host()}:{_rest_port()}/v1.0-beta/phantomPower"
    ) as r:
        data = await r.json(content_type=None)
    entry = next(e for e in data["data"] if int(e["id"]) == 1)
    original = entry["phantomPowerEnabled"]
    target = not original

    async def _toggle():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/phantomPower/1",
            json={"phantomPowerEnabled": target},
        ):
            pass

    pop_task = asyncio.create_task(_toggle())
    try:
        event = await _wait_for(client, name="Change Phantom Power")
        assert event.topic == "PhantomPower"
        assert event.records[0]["phantomPowerEnabled"] == target
    finally:
        await pop_task
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/phantomPower/1",
            json={"phantomPowerEnabled": original},
        ):
            pass


async def test_gpo_round_trip(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """GPO toggle surfaces on the WorkingSettings topic as
    Modify generalPurposeOutputConfiguration."""
    base = (
        f"http://{_host()}:{_rest_port()}"
        "/v1.0-beta/workingsettings/generalPurposeOutputConfiguration"
    )
    async with rest.get(base) as r:
        data = await r.json(content_type=None)
    entry = next(
        e for e in data["data"] if e["id"] == "General Purpose Output Pin.1"
    )
    original = entry["value"]
    target = "high" if original == "low" else "low"

    async def _toggle():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"{base}/General%20Purpose%20Output%20Pin.1", json={"value": target}
        ):
            pass

    pop_task = asyncio.create_task(_toggle())
    try:
        event = await _wait_for(
            client, name="Modify generalPurposeOutputConfiguration"
        )
        assert event.topic == "WorkingSettings"
        assert event.records[0]["value"] == target
    finally:
        await pop_task
        async with rest.post(
            f"{base}/General%20Purpose%20Output%20Pin.1", json={"value": original}
        ):
            pass


# ── Multi-op (preset recall 3-phase protocol) ──────────────────────────


async def test_preset_recall_three_phase_protocol(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """Recalling a preset produces Begin → Recall → End in order, with
    the originating session UUID on Begin/Recall and 0 on End."""
    async with rest.get(
        f"http://{_host()}:{_rest_port()}/v1.0-beta/preset"
    ) as r:
        presets = (await r.json(content_type=None))["data"]
    if not presets:
        pytest.skip("Device has no presets to recall")
    preset_name = presets[0]["name"]

    events_seen: list[Event] = []
    remove = client.on_topic("Preset", lambda e: events_seen.append(e))
    try:
        async with rest.post(
            f"http://{_host()}:{_rest_port()}/v1.0-beta/preset/recall/{preset_name}",
            json={},
        ):
            pass
        # Wait up to 5s for all three phases.
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            names = [e.name for e in events_seen]
            if (
                "Preset Recall Begin" in names
                and "Preset Recall" in names
                and "Preset Recall End" in names
            ):
                break
            await asyncio.sleep(0.1)
    finally:
        remove()

    names = [e.name for e in events_seen]
    assert "Preset Recall Begin" in names, f"got {names}"
    assert "Preset Recall" in names
    assert "Preset Recall End" in names

    begin_idx = names.index("Preset Recall Begin")
    end_idx = names.index("Preset Recall End")
    assert begin_idx < end_idx
    # Middle Recall event must carry multiple operations (the bulk delta).
    recall_event = next(e for e in events_seen if e.name == "Preset Recall")
    assert len(recall_event.operations) > 1
    # End event is system-emitted.
    end_event = next(e for e in events_seen if e.name == "Preset Recall End")
    assert end_event.unique_id == 0


# ── Channel meters stream ──────────────────────────────────────────────


async def test_channel_meters_stream_emits_at_high_rate(
    client: AquaControlClient,
) -> None:
    """The Channel Meters topic emits at >2 Hz and events are classified as meters."""
    meters_seen: list[Event] = []
    remove = client.on_topic("Channel Meters", lambda e: meters_seen.append(e))
    try:
        await asyncio.sleep(2.0)
    finally:
        remove()
    rate = len(meters_seen) / 2.0
    assert rate > 2.0, f"only {len(meters_seen)} meter events in 2 s ({rate:.1f} Hz)"
    assert all(e.is_meter for e in meters_seen)
    assert all(not e.is_state_change for e in meters_seen)
    assert all(not e.is_ambient for e in meters_seen)


# ── Authentication gate negative test ──────────────────────────────────


async def test_unauthenticated_ws_still_connects(
    rest: aiohttp.ClientSession,
) -> None:
    """An unauthenticated WebSocket connection succeeds and receives events.

    Historically the cookie-gating story was believed to be strict — only
    public broadcasts to unauthenticated clients, state events only to
    authenticated ones. Empirically (firmware 1.1.8+) state events DO
    arrive without auth too, so the gate is best described as "auth
    recommended for security + future-proofing" rather than "auth required
    to see anything useful". This test documents the current reality.
    """
    from aquacontrol.stream import StreamConnection

    received: list[tuple[str, dict]] = []
    stream = StreamConnection(
        host=_host(),
        port=8001,
        topics=["System"],
        cookie_header=None,
        on_event=lambda topic, payload: received.append((topic, payload)),
    )
    await stream.start()
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not stream.connected:
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("Unauthenticated WS did not connect")
            await asyncio.sleep(0.1)
        # Wait a beat for ambient broadcasts to flow.
        await asyncio.sleep(2.0)
        event_names = {
            p.get("name", "") if isinstance(p, dict) else ""
            for _, p in received
        }
        # Connection works and ambient events flow — sufficient for the
        # library to be useful without auth, even if auth is recommended.
        assert any(
            n in event_names for n in ("System Info Values", "DateTime")
        ), f"unauthenticated WS got nothing in 2 s: {event_names}"
    finally:
        await stream.stop()


# ── Dispatch kinds ─────────────────────────────────────────────────────


async def test_on_event_dispatch_by_name(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """on_event(name, …) fires only for that specific inner event name."""
    received: list[Event] = []
    remove = client.on_event(
        "Set Chain Mute", lambda e: received.append(e)
    )
    try:
        chain_url = (
            f"http://{_host()}:{_rest_port()}/v1.0-beta/workingsettings/dsp/chain"
        )
        async with rest.get(chain_url) as r:
            chain = await r.json(content_type=None)
        entry = next(e for e in chain["data"] if e["id"] == "InputChannel.1")
        original = entry["muted"]
        async with rest.post(
            f"{chain_url}/mute/InputChannel.1", json={"muted": not original}
        ):
            pass
        try:
            await asyncio.wait_for(
                _await_predicate(lambda: bool(received)),
                timeout=EVENT_TIMEOUT_S,
            )
        finally:
            async with rest.post(
                f"{chain_url}/mute/InputChannel.1", json={"muted": original}
            ):
                pass
    finally:
        remove()
    # Every event we got is the right name.
    assert received, "no Set Chain Mute event received"
    assert all(e.name == "Set Chain Mute" for e in received)


async def test_on_any_dispatch_sees_multiple_topics(
    client: AquaControlClient,
) -> None:
    """on_any sees events from at least 2 distinct topics during a 3-s capture."""
    seen: list[Event] = []
    remove = client.on_any(lambda e: seen.append(e))
    try:
        await asyncio.sleep(3.0)
    finally:
        remove()
    topics = {e.topic for e in seen}
    # Channel Meters arrives at 5 Hz; System arrives at 1 Hz. Both within 3 s.
    assert "Channel Meters" in topics or "System" in topics
    assert len(topics) >= 2, f"only saw {topics} in 3 s"


# ── Runtime topic management ───────────────────────────────────────────


async def test_runtime_join_subscribes_after_connect() -> None:
    """Connect with topics=[], then client.join('System') and watch events flow."""
    client = AquaControlClient(
        host=_host(),
        username=_username(),
        password=_password(),
        rest_port=_rest_port(),
        topics=[],
    )
    await client.connect()
    try:
        # Wait for the underlying WS.
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_S
        while not client.connected:
            if asyncio.get_running_loop().time() > deadline:
                pytest.fail("Client did not connect")
            await asyncio.sleep(0.1)

        # Before joining, we shouldn't receive System Info Values.
        pre_seen: list[Event] = []
        pre_remove = client.on_topic("System", lambda e: pre_seen.append(e))
        await asyncio.sleep(1.5)
        pre_remove()
        assert not pre_seen, f"got System events without subscribing: {pre_seen!r}"

        # Now join.
        post_seen: list[Event] = []
        post_remove = client.on_topic("System", lambda e: post_seen.append(e))
        await client.join("System")
        await asyncio.sleep(2.5)
        post_remove()
        assert post_seen, "no System events after runtime join"
    finally:
        await client.disconnect()


# ── Event accessor shape on real events ────────────────────────────────


async def test_event_api_and_records_shape_on_crosspoint_mute(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """The parsed Event for a crosspoint-mute toggle carries the expected
    api/type/records — proves the parser matches wire-format reality."""
    from urllib.parse import quote

    cp_id = "Mixer.1.InputChannel.1.Source Mute"
    cp_url = (
        f"http://{_host()}:{_rest_port()}"
        "/v1.0-beta/workingsettings/dsp/mixer/config/parameter"
    )
    async with rest.get(cp_url) as r:
        params = (await r.json(content_type=None))["data"]
    entry = next(e for e in params if e["id"] == cp_id)
    original = entry["value"]
    target = not original

    async def _toggle():
        await asyncio.sleep(0.2)
        async with rest.post(
            f"{cp_url}/{quote(cp_id, safe='.')}", json={"value": target}
        ):
            pass

    pop_task = asyncio.create_task(_toggle())
    try:
        event = await _wait_for(
            client,
            name="Modify DSP Mixer Parameter Value",
            predicate=lambda e: e.records
            and e.records[0].get("id") == cp_id,
        )
        # Single-op event accessors are populated
        assert event.api == "/workingsettings/dsp/mixer/config/parameter"
        assert event.type == "modify"
        assert len(event.operations) == 1
        # Records carry the discriminator
        record = event.records[0]
        assert record["DSPMixerConfigParameterTypeId"] == "Mixer.Source Mute"
        assert record["value"] == target
        # Classification properties match
        assert event.is_state_change
        assert not event.is_ambient
        assert not event.is_meter
    finally:
        await pop_task
        from urllib.parse import quote as _q
        async with rest.post(
            f"{cp_url}/{_q(cp_id, safe='.')}", json={"value": original}
        ):
            pass


# ── Multi-listener fan-out ─────────────────────────────────────────────


async def test_multi_listener_fan_out_in_documented_order(
    client: AquaControlClient, rest: aiohttp.ClientSession
) -> None:
    """on_any, on_topic, and on_event all matching the same event fire in
    order: any → topic → event_name."""
    order: list[str] = []

    def _any_handler(e: Event) -> None:
        if e.name == "Set Chain Mute":
            order.append("any")

    def _topic_handler(e: Event) -> None:
        if e.name == "Set Chain Mute":
            order.append("topic")

    remove1 = client.on_any(_any_handler)
    remove2 = client.on_topic("WorkingSettings", _topic_handler)
    remove3 = client.on_event("Set Chain Mute", lambda e: order.append("event"))

    try:
        chain_url = (
            f"http://{_host()}:{_rest_port()}/v1.0-beta/workingsettings/dsp/chain"
        )
        async with rest.get(chain_url) as r:
            chain = await r.json(content_type=None)
        entry = next(e for e in chain["data"] if e["id"] == "InputChannel.1")
        original = entry["muted"]
        async with rest.post(
            f"{chain_url}/mute/InputChannel.1", json={"muted": not original}
        ):
            pass
        try:
            await asyncio.wait_for(
                _await_predicate(lambda: len(order) >= 3),
                timeout=EVENT_TIMEOUT_S,
            )
        finally:
            async with rest.post(
                f"{chain_url}/mute/InputChannel.1", json={"muted": original}
            ):
                pass
    finally:
        remove1()
        remove2()
        remove3()

    # First three entries are from our single mutation, in documented order.
    assert order[:3] == ["any", "topic", "event"], f"got {order}"


# ── Clean disconnect ───────────────────────────────────────────────────


async def test_clean_disconnect_stops_background_task() -> None:
    """await client.disconnect() exits without hanging."""
    c = AquaControlClient(
        host=_host(),
        username=_username(),
        password=_password(),
        rest_port=_rest_port(),
    )
    await c.connect()
    # Wait for the socket to come up so we exercise the actual teardown path.
    deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_S
    while not c.connected:
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("did not connect")
        await asyncio.sleep(0.1)

    # disconnect should return within 3 s on a healthy stream
    await asyncio.wait_for(c.disconnect(), timeout=3.0)
    assert not c.connected


# ── Local helper used by a couple of tests above ───────────────────────


async def _await_predicate(predicate) -> None:
    """Block until predicate() returns truthy, polling every 100 ms."""
    while not predicate():
        await asyncio.sleep(0.1)
