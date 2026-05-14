"""Tests for the low-level Socket.IO stream wrapper."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

from aquacontrol.stream import StreamConnection, _next_backoff


def _make_fake_sio(captured: dict):
    """Build a FakeSio class that captures connect()'s kwargs into `captured`.

    The real ``socketio.AsyncClient`` is replaced with this in tests that
    exercise :meth:`StreamConnection._connect_once` without touching a real
    socket. Supports the lifecycle calls our wrapper makes.
    """

    class FakeSio:
        connected = True

        def __init__(self, *args, **kwargs):
            self._handlers: dict[str, object] = {}

        async def connect(self, url, *, transports, headers):
            captured["url"] = url
            captured["transports"] = transports
            captured["headers"] = headers

        async def wait(self):
            return  # return immediately so _connect_once completes

        async def disconnect(self):
            pass

        async def emit(self, *_args, **_kwargs):
            pass

        def on(self, name, handler):
            self._handlers[name] = handler

        _trigger_event = AsyncMock()

    return FakeSio


def test_next_backoff_caps_and_floors():
    """Doubling + clamp + jitter never escapes [MIN, MAX] window."""
    for _ in range(200):
        b = _next_backoff(0.1)
        assert 1.0 <= b <= 30.0
        b2 = _next_backoff(100.0)  # already above cap
        assert 1.0 <= b2 <= 30.0


async def test_emit_noop_when_disconnected():
    """emit() while not connected should silently do nothing, not raise."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    # _sio is None until start() runs and connect() completes
    await conn.emit("join", "System")  # must not raise
    on_event.assert_not_called()


async def test_url_built_from_host_and_port():
    on_event = AsyncMock()
    conn = StreamConnection(
        host="192.168.1.10",
        port=8001,
        topics=[],
        cookie_header=None,
        on_event=on_event,
    )
    assert conn.url == "http://192.168.1.10:8001"


async def test_patched_trigger_event_forwards_app_events_to_on_event():
    """Application events reach on_event with (topic, payload)."""
    on_event = MagicMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    # Build a fake socketio client and install the patch on it.
    fake_sio = MagicMock()
    original_trigger = AsyncMock()
    fake_sio._trigger_event = original_trigger
    conn._install_trigger_patch(fake_sio)

    # When socket.io receives "Set Chain Mute" event with namespace + payload
    await fake_sio._trigger_event("Set Chain Mute", "/", {"name": "Set Chain Mute"})
    on_event.assert_called_once_with("Set Chain Mute", {"name": "Set Chain Mute"})
    original_trigger.assert_awaited_once()


async def test_patched_trigger_event_skips_reserved_events():
    """connect / disconnect / reconnect / connect_error don't reach on_event."""
    on_event = MagicMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    fake_sio = MagicMock()
    fake_sio._trigger_event = AsyncMock()
    conn._install_trigger_patch(fake_sio)

    for reserved in ("connect", "disconnect", "reconnect", "connect_error"):
        await fake_sio._trigger_event(reserved)

    on_event.assert_not_called()


async def test_patched_trigger_event_supervises_listener_exceptions(caplog):
    """Listener exceptions are caught + logged, don't break socket dispatch."""
    on_event = MagicMock(side_effect=RuntimeError("boom"))
    conn = StreamConnection(
        host="x",
        port=8001,
        topics=[],
        cookie_header=None,
        on_event=on_event,
        logger=logging.getLogger("test"),
    )
    fake_sio = MagicMock()
    original = AsyncMock()
    fake_sio._trigger_event = original
    conn._install_trigger_patch(fake_sio)

    with caplog.at_level(logging.ERROR):
        await fake_sio._trigger_event("Some Event", "/", {"data": "x"})
    # Original dispatch still runs even after listener blew up.
    original.assert_awaited_once()


async def test_patched_trigger_event_supports_async_on_event():
    """on_event may be an async function — it should be awaited."""
    seen: list[tuple[str, object]] = []

    async def on_event(topic, payload):
        seen.append((topic, payload))

    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    fake_sio = MagicMock()
    fake_sio._trigger_event = AsyncMock()
    conn._install_trigger_patch(fake_sio)
    await fake_sio._trigger_event("Set Chain Mute", "/", {"x": 1})
    assert seen == [("Set Chain Mute", {"x": 1})]


async def test_start_is_idempotent():
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    with patch.object(conn, "_run", new=AsyncMock(return_value=None)):
        await conn.start()
        task1 = conn._task
        await conn.start()
        task2 = conn._task
        # Same task — no second start.
        assert task1 is task2
    await conn.stop()


async def test_stop_when_never_started():
    """stop() on a never-started connection is a no-op."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    await conn.stop()  # must not raise


async def test_cookie_header_used_in_handshake():
    """When a cookie header is supplied, it's set on the underlying socket.io connect."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x",
        port=8001,
        topics=["System"],
        cookie_header="ashly-sid=abc123",
        on_event=on_event,
    )

    # Mock the socketio.AsyncClient so we can capture connect's kwargs.
    captured: dict[str, object] = {}

    with patch("aquacontrol.stream.socketio.AsyncClient", _make_fake_sio(captured)):
        # Run one connect pass manually to avoid the reconnect loop.
        await conn._connect_once()

    assert captured["url"] == "http://x:8001"
    assert captured["transports"] == ["websocket"]
    assert captured["headers"] == {"Cookie": "ashly-sid=abc123"}


async def test_no_cookie_header_means_no_cookie_in_handshake():
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    captured: dict[str, object] = {}

    with patch("aquacontrol.stream.socketio.AsyncClient", _make_fake_sio(captured)):
        await conn._connect_once()
    assert captured["headers"] == {}
