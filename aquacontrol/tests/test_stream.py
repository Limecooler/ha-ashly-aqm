"""Tests for the low-level Socket.IO stream wrapper."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


# ── reconnect loop ──────────────────────────────────────────────────────


async def test_run_loop_retries_after_connect_failure():
    """A failing _connect_once is caught, backoff advances, then retried."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    call_count = {"n": 0}

    async def fake_connect_once():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated drop")
        # Second iteration: signal the loop to exit cleanly.
        conn._stopping = True

    # Skip real backoff sleeps so the test doesn't take 1+ seconds.
    async def fake_sleep(_):
        return

    with (
        patch.object(conn, "_connect_once", new=fake_connect_once),
        patch("aquacontrol.stream.asyncio.sleep", new=fake_sleep),
    ):
        await conn._run()
    # Loop fired connect_once at least twice (failure + clean exit).
    assert call_count["n"] >= 2


async def test_run_loop_resets_backoff_on_long_connection():
    """If _connect_once returns AFTER the dwell threshold, backoff resets to 1s.

    Captures the value of `_backoff` *before* the post-sleep `_next_backoff`
    bump (since that bump would push it back up to ~2s, masking the reset).
    """
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    conn._backoff = 16.0  # something high

    fake_time = {"now": 0.0}

    class FakeLoop:
        def time(self):
            return fake_time["now"]

    backoff_observed: list[float] = []

    async def fake_connect_once():
        fake_time["now"] += 60.0  # simulate long-lived connection

    async def fake_sleep(s):
        # Sleep is called with `_backoff` AFTER any reset but BEFORE
        # `_next_backoff` bumps it. Capture and exit the loop.
        backoff_observed.append(conn._backoff)
        conn._stopping = True

    with (
        patch.object(conn, "_connect_once", new=fake_connect_once),
        patch("aquacontrol.stream.asyncio.get_running_loop", return_value=FakeLoop()),
        patch("aquacontrol.stream.asyncio.sleep", new=fake_sleep),
    ):
        await conn._run()

    assert backoff_observed == [1.0]  # reset took effect


async def test_run_loop_exits_on_cancellation():
    """asyncio.CancelledError propagates out of _run cleanly."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )

    async def fake_connect_once():
        raise asyncio.CancelledError

    with (
        patch.object(conn, "_connect_once", new=fake_connect_once),
        pytest.raises(asyncio.CancelledError),
    ):
        await conn._run()


async def test_connect_once_propagates_socketio_connection_error():
    """A socketio.exceptions.ConnectionError → AquaControlConnectionError."""
    import socketio as sio_mod

    from aquacontrol.exceptions import AquaControlConnectionError

    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )

    class FailingSio:
        connected = False

        def __init__(self, *args, **kwargs):
            pass

        async def connect(self, *args, **kwargs):
            raise sio_mod.exceptions.ConnectionError("refused")

        async def wait(self):
            return

        async def disconnect(self):
            pass

        def on(self, name, handler):
            pass

        _trigger_event = AsyncMock()

    with (
        patch("aquacontrol.stream.socketio.AsyncClient", FailingSio),
        pytest.raises(AquaControlConnectionError),
    ):
        await conn._connect_once()


async def test_on_connect_callback_invoked_then_topics_joined():
    """When the underlying socket fires 'connect', user callback runs AND topics are rejoined."""
    on_event = AsyncMock()
    on_connect_called = []
    captured_emits: list[tuple[str, str]] = []

    async def user_on_connect():
        on_connect_called.append(True)

    conn = StreamConnection(
        host="x",
        port=8001,
        topics=["System", "WorkingSettings"],
        cookie_header=None,
        on_event=on_event,
        on_connect=user_on_connect,
    )

    # Build a FakeSio that captures the registered handlers and emit calls.
    handlers: dict[str, object] = {}
    captured: dict[str, object] = {}

    class FakeSio:
        connected = True

        def __init__(self, *args, **kwargs):
            pass

        async def connect(self, url, *, transports, headers):
            captured["headers"] = headers

        async def wait(self):
            return

        async def disconnect(self):
            pass

        async def emit(self, name, data=None):
            captured_emits.append((name, data))

        def on(self, name, handler):
            handlers[name] = handler

        _trigger_event = AsyncMock()

    with patch("aquacontrol.stream.socketio.AsyncClient", FakeSio):
        await conn._connect_once()
        # Now fire the registered connect handler.
        await handlers["connect"]()

    assert on_connect_called == [True]
    assert ("join", "System") in captured_emits
    assert ("join", "WorkingSettings") in captured_emits


async def test_on_disconnect_callback_invoked():
    on_event = AsyncMock()
    on_disconnect_called = []

    async def user_on_disconnect():
        on_disconnect_called.append(True)

    conn = StreamConnection(
        host="x",
        port=8001,
        topics=[],
        cookie_header=None,
        on_event=on_event,
        on_disconnect=user_on_disconnect,
    )

    handlers: dict[str, object] = {}

    class FakeSio:
        connected = True

        def __init__(self, *args, **kwargs):
            pass

        async def connect(self, *args, **kwargs):
            pass

        async def wait(self):
            return

        async def disconnect(self):
            pass

        async def emit(self, *_args, **_kwargs):
            pass

        def on(self, name, handler):
            handlers[name] = handler

        _trigger_event = AsyncMock()

    with patch("aquacontrol.stream.socketio.AsyncClient", FakeSio):
        await conn._connect_once()
        await handlers["disconnect"]()

    assert on_disconnect_called == [True]


async def test_emit_passes_through_when_connected():
    """emit() forwards to the underlying socket when connected."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    # Inject a fake sio that's marked connected.
    fake_sio = MagicMock()
    fake_sio.connected = True
    fake_sio.emit = AsyncMock()
    conn._sio = fake_sio
    await conn.emit("join", "MyTopic")
    fake_sio.emit.assert_awaited_once_with("join", "MyTopic")


async def test_connected_property_false_when_no_sio():
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    assert conn.connected is False


async def test_stop_suppresses_disconnect_exception():
    """Even if sio.disconnect() raises, stop() completes cleanly."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=on_event
    )
    fake_sio = MagicMock()
    fake_sio.disconnect = AsyncMock(side_effect=RuntimeError("can't disconnect"))
    conn._sio = fake_sio
    await conn.stop()  # must not raise
    fake_sio.disconnect.assert_awaited_once()


async def test_topic_join_failure_logged_not_propagated(caplog):
    """A failing emit('join', X) during the connect handler must be logged but
    must not prevent other joins or kill the connection."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x",
        port=8001,
        topics=["A", "B"],
        cookie_header=None,
        on_event=on_event,
    )

    handlers: dict[str, object] = {}
    captured_sio: list[object] = []

    class FakeSio:
        connected = True

        def __init__(self, *args, **kwargs):
            self.emit = AsyncMock(side_effect=[RuntimeError("A failed"), None])
            captured_sio.append(self)

        async def connect(self, *args, **kwargs):
            pass

        async def wait(self):
            return

        async def disconnect(self):
            pass

        def on(self, name, handler):
            handlers[name] = handler

        _trigger_event = AsyncMock()

    with patch("aquacontrol.stream.socketio.AsyncClient", FakeSio):
        await conn._connect_once()
        with caplog.at_level(logging.ERROR):
            await handlers["connect"]()

    # Loop didn't stop on A's failure — B still attempted.
    sio = captured_sio[0]
    assert sio.emit.await_count == 2  # both A and B were tried


# ── cookie provider ────────────────────────────────────────────────────


async def test_cookie_provider_invoked_per_connect_attempt():
    """A cookie_provider is awaited before each connect so reconnects
    pick up freshly issued cookies."""
    call_count = {"n": 0}

    async def provider() -> str:
        call_count["n"] += 1
        return f"ashly-sid=token-{call_count['n']}"

    on_event = AsyncMock()
    conn = StreamConnection(
        host="x",
        port=8001,
        topics=["System"],
        cookie_provider=provider,
        on_event=on_event,
    )

    captured: dict = {}

    with patch("aquacontrol.stream.socketio.AsyncClient", _make_fake_sio(captured)):
        await conn._connect_once()

    assert call_count["n"] == 1
    assert captured["headers"] == {"Cookie": "ashly-sid=token-1"}


async def test_static_cookie_header_used_when_no_provider():
    """Legacy ``cookie_header=`` still works."""
    on_event = AsyncMock()
    conn = StreamConnection(
        host="x",
        port=8001,
        topics=[],
        cookie_header="ashly-sid=static",
        on_event=on_event,
    )
    captured: dict = {}
    with patch("aquacontrol.stream.socketio.AsyncClient", _make_fake_sio(captured)):
        await conn._connect_once()
    assert captured["headers"] == {"Cookie": "ashly-sid=static"}


async def test_cookie_provider_takes_precedence_over_static_header():
    async def provider() -> str:
        return "from-provider"

    conn = StreamConnection(
        host="x",
        port=8001,
        topics=[],
        cookie_header="from-static",
        cookie_provider=provider,
        on_event=AsyncMock(),
    )
    captured: dict = {}
    with patch("aquacontrol.stream.socketio.AsyncClient", _make_fake_sio(captured)):
        await conn._connect_once()
    assert captured["headers"] == {"Cookie": "from-provider"}


# ── protocol-error guard on incompatible socketio ─────────────────────


async def test_protocol_error_when_trigger_event_hook_missing():
    """If the installed socketio is missing the private _trigger_event
    attribute, we raise AquaControlProtocolError(transient=False) rather
    than crashing later with a confusing AttributeError."""
    from aquacontrol.exceptions import AquaControlProtocolError

    conn = StreamConnection(
        host="x", port=8001, topics=[], cookie_header=None, on_event=AsyncMock()
    )

    class IncompatibleSio:
        """Missing _trigger_event entirely (simulating a 6.x rename)."""

    with pytest.raises(AquaControlProtocolError, match="incompatible"):
        conn._install_trigger_patch(IncompatibleSio())  # type: ignore[arg-type]


# ── _next_backoff rng injection ────────────────────────────────────────


def test_next_backoff_accepts_per_instance_rng():
    """Passing an RNG seeds the jitter; same seed → same value."""
    import random as _r

    from aquacontrol.stream import _next_backoff

    rng1 = _r.Random(42)
    rng2 = _r.Random(42)
    a = _next_backoff(2.0, rng=rng1)
    b = _next_backoff(2.0, rng=rng2)
    assert a == b
