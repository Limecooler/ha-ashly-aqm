"""Tests for the high-level AquaControlClient.

The Socket.IO and REST layers are mocked — these are pure dispatch /
listener-registry tests. End-to-end tests against the live device live
in the ha-ashly-aqm repo's integration suite.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aquacontrol import AquaControlClient, Event
from aquacontrol.topics import SYSTEM, WORKING_SETTINGS


@pytest.fixture
def patch_stream():
    """Replace StreamConnection with a mock so connect() doesn't open a socket."""
    with patch("aquacontrol.client.StreamConnection") as MockStream:
        instance = MagicMock()
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.emit = AsyncMock()
        instance.connected = False
        MockStream.return_value = instance
        yield MockStream, instance


@pytest.fixture
def patch_auth():
    """Replace fetch_session_cookies with a mock that yields a static cookie."""
    with patch("aquacontrol.client.fetch_session_cookies") as mock_fetch:
        mock_fetch.return_value = {"ashly-sid": "test-session-uuid"}
        yield mock_fetch


# ── connect / disconnect ────────────────────────────────────────────────


async def test_connect_logs_in_and_starts_stream(patch_auth, patch_stream):
    MockStream, stream = patch_stream
    client = AquaControlClient(host="192.0.2.1", username="u", password="p")
    await client.connect()
    patch_auth.assert_awaited_once()
    MockStream.assert_called_once()
    stream.start.assert_awaited_once()


async def test_disconnect_stops_stream(patch_auth, patch_stream):
    _MockStream, stream = patch_stream
    client = AquaControlClient(host="192.0.2.1", username="u", password="p")
    await client.connect()
    await client.disconnect()
    stream.stop.assert_awaited_once()


async def test_context_manager_lifecycle(patch_auth, patch_stream):
    _MockStream, stream = patch_stream
    async with AquaControlClient(host="192.0.2.1", username="u", password="p") as c:
        assert c.host == "192.0.2.1"
    stream.stop.assert_awaited_once()


# ── listener registration / dispatch ────────────────────────────────────


async def test_on_event_dispatches_by_inner_name():
    """on_event handlers receive matching events and skip non-matching ones."""
    client = AquaControlClient(host="x", username="u", password="p")
    received: list[Event] = []

    async def handler(event):
        received.append(event)

    client.on_event("Set Chain Mute", handler)

    await client._on_raw_event(
        WORKING_SETTINGS,
        {"name": "Set Chain Mute", "data": [], "uniqueId": "s"},
    )
    await client._on_raw_event(
        WORKING_SETTINGS,
        {"name": "Modify DSP Parameter", "data": [], "uniqueId": "s"},
    )

    assert len(received) == 1
    assert received[0].name == "Set Chain Mute"


async def test_on_topic_dispatches_for_every_event_on_topic():
    client = AquaControlClient(host="x", username="u", password="p")
    received: list[Event] = []

    async def handler(event):
        received.append(event)

    client.on_topic(WORKING_SETTINGS, handler)
    await client._on_raw_event(WORKING_SETTINGS, {"name": "A", "data": [], "uniqueId": "s"})
    await client._on_raw_event(WORKING_SETTINGS, {"name": "B", "data": [], "uniqueId": "s"})
    await client._on_raw_event(SYSTEM, {"name": "C", "data": [], "uniqueId": "s"})

    assert [e.name for e in received] == ["A", "B"]


async def test_on_any_dispatches_for_every_event():
    client = AquaControlClient(host="x", username="u", password="p")
    received: list[Event] = []

    async def handler(event):
        received.append(event)

    client.on_any(handler)
    await client._on_raw_event(WORKING_SETTINGS, {"name": "A", "data": [], "uniqueId": "s"})
    await client._on_raw_event(SYSTEM, {"name": "B", "data": [], "uniqueId": "s"})
    assert [e.name for e in received] == ["A", "B"]


async def test_unsubscribe_removes_handler():
    client = AquaControlClient(host="x", username="u", password="p")
    received: list[Event] = []

    async def handler(event):
        received.append(event)

    remove = client.on_event("Set Chain Mute", handler)
    await client._on_raw_event(
        WORKING_SETTINGS, {"name": "Set Chain Mute", "data": [], "uniqueId": "s"}
    )
    assert len(received) == 1

    remove()
    await client._on_raw_event(
        WORKING_SETTINGS, {"name": "Set Chain Mute", "data": [], "uniqueId": "s"}
    )
    assert len(received) == 1  # unchanged


async def test_unsubscribe_is_idempotent():
    """Removing the same handler twice doesn't raise."""
    client = AquaControlClient(host="x", username="u", password="p")
    remove = client.on_any(lambda _: None)
    remove()
    remove()  # should not raise


async def test_dispatch_order_any_then_topic_then_event_name():
    """Catch-alls fire first, then topic-specific, then name-specific."""
    client = AquaControlClient(host="x", username="u", password="p")
    seen: list[str] = []

    client.on_any(lambda _: seen.append("any"))
    client.on_topic(WORKING_SETTINGS, lambda _: seen.append("topic"))
    client.on_event("X", lambda _: seen.append("name"))

    await client._on_raw_event(WORKING_SETTINGS, {"name": "X", "data": [], "uniqueId": "s"})
    assert seen == ["any", "topic", "name"]


async def test_listener_exceptions_do_not_kill_dispatch():
    client = AquaControlClient(host="x", username="u", password="p")

    def bad_handler(_):
        raise RuntimeError("boom")

    seen: list[Event] = []

    async def good_handler(event):
        seen.append(event)

    client.on_any(bad_handler)
    client.on_any(good_handler)

    await client._on_raw_event(SYSTEM, {"name": "X", "data": [], "uniqueId": "s"})
    assert len(seen) == 1  # good handler still received it


async def test_sync_and_async_handlers_both_supported():
    client = AquaControlClient(host="x", username="u", password="p")
    sync_seen: list[Event] = []
    async_seen: list[Event] = []

    client.on_any(lambda e: sync_seen.append(e))

    async def async_handler(e):
        async_seen.append(e)

    client.on_any(async_handler)

    await client._on_raw_event(SYSTEM, {"name": "X", "data": [], "uniqueId": "s"})
    assert len(sync_seen) == 1
    assert len(async_seen) == 1


async def test_listener_count_property():
    client = AquaControlClient(host="x", username="u", password="p")
    assert client.listener_count == 0
    client.on_any(lambda _: None)
    client.on_topic("System", lambda _: None)
    client.on_event("X", lambda _: None)
    assert client.listener_count == 3


# ── session ID / echo filtering ─────────────────────────────────────────


async def test_set_session_id_enables_echo_filtering():
    client = AquaControlClient(host="x", username="u", password="p")
    client.set_session_id("my-uuid")
    assert client.session_id == "my-uuid"

    captured: list[Event] = []

    async def handler(event: Event) -> None:
        if event.is_from_session(client.session_id):
            return
        captured.append(event)

    client.on_any(handler)
    # Own echo — should be filtered
    await client._on_raw_event(
        SYSTEM, {"name": "Modify system info", "data": [], "uniqueId": "my-uuid"}
    )
    # Other client's change — should be received
    await client._on_raw_event(
        SYSTEM, {"name": "Modify system info", "data": [], "uniqueId": "other-uuid"}
    )

    assert len(captured) == 1
    assert captured[0].unique_id == "other-uuid"


# ── manual topic management ─────────────────────────────────────────────


async def test_join_adds_topic_to_rejoin_set(patch_auth, patch_stream):
    _MockStream, stream = patch_stream
    client = AquaControlClient(
        host="x", username="u", password="p", topics=["System"]
    )
    await client.connect()
    assert client.topics == ("System",)
    await client.join("WorkingSettings")
    assert "WorkingSettings" in client.topics
    stream.emit.assert_awaited_with("join", "WorkingSettings")


async def test_join_existing_topic_is_noop(patch_auth, patch_stream):
    _MockStream, stream = patch_stream
    client = AquaControlClient(
        host="x", username="u", password="p", topics=["System"]
    )
    await client.connect()
    await client.join("System")
    # No emit should have been made for the duplicate.
    stream.emit.assert_not_awaited()


async def test_leave_removes_topic(patch_auth, patch_stream):
    _MockStream, stream = patch_stream
    client = AquaControlClient(
        host="x", username="u", password="p", topics=["System", "WorkingSettings"]
    )
    await client.connect()
    await client.leave("WorkingSettings")
    assert "WorkingSettings" not in client.topics
    stream.emit.assert_awaited_with("leave", "WorkingSettings")


# ── topics argument ──────────────────────────────────────────────────────


def test_default_topics_is_all_known():
    """With no topics= arg, every known topic is subscribed."""
    from aquacontrol.topics import ALL_TOPICS

    client = AquaControlClient(host="x", username="u", password="p")
    assert client.topics == ALL_TOPICS


def test_custom_topics_respected():
    client = AquaControlClient(
        host="x", username="u", password="p", topics=["System"]
    )
    assert client.topics == ("System",)


def test_empty_topics_subscribes_to_nothing():
    """topics=[] means 'I only want explicit join() calls later'."""
    client = AquaControlClient(host="x", username="u", password="p", topics=[])
    assert client.topics == ()


async def test_connected_false_without_stream():
    """Before connect() is called, .connected reports False."""
    client = AquaControlClient(host="x", username="u", password="p")
    assert client.connected is False
