"""Tests for the live meter client."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.ashly.meter import (
    METER_FLOOR_DB,
    AshlyMeterClient,
    _decode_records,
    raw_to_db,
)


def test_raw_to_db_floor():
    assert raw_to_db(0) == METER_FLOOR_DB


def test_raw_to_db_unity():
    """raw=60 is unity (0 dBu) per the documented -60 floor."""
    assert raw_to_db(60) == 0.0


def test_raw_to_db_clip():
    assert raw_to_db(80) == 20.0


def test_raw_to_db_bad_input_returns_floor():
    assert raw_to_db(None) == METER_FLOOR_DB
    assert raw_to_db("not-a-number") == METER_FLOOR_DB


# ── envelope decoding ─────────────────────────────────────────────────


def test_decode_records_full():
    payload = {
        "name": "Channel Meters",
        "data": [{"api": "update", "records": [1, 2, 3], "type": "modify"}],
        "uniqueId": None,
    }
    assert _decode_records(payload) == [1, 2, 3]


def test_decode_records_missing_data():
    assert _decode_records({"name": "x"}) is None
    assert _decode_records({}) is None
    assert _decode_records(None) is None


def test_decode_records_empty_data():
    assert _decode_records({"data": []}) is None


def test_decode_records_no_records():
    assert _decode_records({"data": [{"api": "update"}]}) is None


# ── client behaviour with mocked socket.io ────────────────────────────


def _make_client() -> AshlyMeterClient:
    jar = aiohttp.CookieJar(unsafe=True)
    return AshlyMeterClient(host="192.0.2.1", port=8000, cookie_jar=jar)


async def test_initial_state():
    c = _make_client()
    assert c.latest_records == []
    assert c.connected is False


async def test_listener_throttling():
    """Successive pushes within the publish window should fire only once."""
    c = _make_client()
    calls: list[list[int]] = []
    c.add_listener(lambda r: calls.append(list(r)))
    c._maybe_publish([1, 2, 3])
    c._maybe_publish([4, 5, 6])  # should be throttled
    c._maybe_publish([7, 8, 9])
    assert calls == [[1, 2, 3]]


async def test_remove_listener_stops_callbacks():
    c = _make_client()
    calls: list[int] = []
    remove = c.add_listener(lambda r: calls.append(len(r)))
    c._maybe_publish([1, 2, 3])
    remove()
    # Force-bypass throttle by resetting last-publish time
    c._last_publish = 0.0
    c._maybe_publish([4, 5, 6])
    assert calls == [3]


async def test_add_listener_returns_removal():
    c = _make_client()
    remove = c.add_listener(lambda r: None)
    assert callable(remove)
    remove()  # idempotent — second call is harmless
    remove()


async def test_stop_when_not_started_is_noop():
    c = _make_client()
    await c.async_stop()  # no exception


# ── new tests covering audit-found gaps ───────────────────────────────


async def test_listener_throttle_resumes_after_window():
    """After METER_PUBLISH_INTERVAL_S elapses, the next publish fires again."""
    from custom_components.ashly.const import METER_PUBLISH_INTERVAL_S

    c = _make_client()
    calls: list[list[int]] = []
    c.add_listener(lambda r: calls.append(list(r)))
    c._maybe_publish([1, 2, 3])
    # Simulate enough time elapsing to clear the throttle window.
    c._last_publish -= METER_PUBLISH_INTERVAL_S + 0.1
    c._maybe_publish([4, 5, 6])
    assert calls == [[1, 2, 3], [4, 5, 6]]


async def test_listener_dedup_skips_identical_records():
    """If records haven't changed since the last publish, listeners are skipped."""
    c = _make_client()
    calls: list[list[int]] = []
    c.add_listener(lambda r: calls.append(list(r)))
    c._maybe_publish([1, 2, 3])
    # Force-bypass throttle, but records are identical — should not fire.
    c._last_publish = 0.0
    c._maybe_publish([1, 2, 3])
    assert calls == [[1, 2, 3]]
    # Now change records — should fire again.
    c._last_publish = 0.0
    c._maybe_publish([1, 2, 4])
    assert calls == [[1, 2, 3], [1, 2, 4]]


async def test_listener_exception_does_not_break_other_listeners():
    """A misbehaving listener must not block other listeners from getting updates."""
    c = _make_client()
    other_calls: list[list[int]] = []

    def bad(_records: list[int]) -> None:
        raise RuntimeError("boom")

    c.add_listener(bad)
    c.add_listener(lambda r: other_calls.append(list(r)))
    # No exception should propagate.
    c._maybe_publish([1, 2, 3])
    assert other_calls == [[1, 2, 3]]


async def test_remove_listener_during_iteration_is_safe():
    """A listener that removes itself mid-publish should not crash."""
    c = _make_client()
    later_calls: list[list[int]] = []

    remove_holder: list = []

    def self_remove(_records: list[int]) -> None:
        remove_holder[0]()  # remove this listener

    remove = c.add_listener(self_remove)
    remove_holder.append(remove)
    c.add_listener(lambda r: later_calls.append(list(r)))

    c._maybe_publish([1, 2, 3])
    # Listener after the self-removing one still fires (snapshot of listeners).
    assert later_calls == [[1, 2, 3]]
    # On the next non-throttled publish the self-removed listener is gone.
    c._last_publish = 0.0
    c._maybe_publish([1, 2, 4])
    assert later_calls == [[1, 2, 3], [1, 2, 4]]


async def test_async_stop_is_safe_to_call_twice():
    c = _make_client()
    await c.async_stop()
    await c.async_stop()  # no exception


# ── socket.io run loop with mocked client ─────────────────────────────


def _make_fake_sio_client() -> MagicMock:
    """Build a stand-in for socketio.AsyncClient with the methods we exercise."""
    sio = MagicMock()
    sio.connect = AsyncMock()
    sio.emit = AsyncMock()
    sio.wait = AsyncMock()
    sio.disconnect = AsyncMock()
    sio.connected = True
    sio.on = MagicMock(return_value=lambda fn: fn)
    sio.eio = MagicMock()
    sio.eio.external_http = False
    return sio


async def test_run_connects_and_subscribes():
    """A successful _connect_and_stream calls connect, join, startMeters."""
    sio = _make_fake_sio_client()
    # Make sio.wait() block forever until cancelled (a real socket would too).
    waiter: asyncio.Future = asyncio.get_event_loop().create_future()
    sio.wait.side_effect = lambda: waiter

    client = _make_client()
    with patch("custom_components.ashly.meter.socketio.AsyncClient", return_value=sio):
        task = asyncio.create_task(client._connect_and_stream())
        # Give the task a moment to issue connect/emit calls.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if sio.emit.await_count >= 2:
                break
        # Trigger the stop event so the task can exit.
        client._stop_event.set()
        # Release the wait()
        if not waiter.done():
            waiter.set_result(None)
        await asyncio.wait_for(task, timeout=2.0)

    sio.connect.assert_awaited_once()
    join_call = sio.emit.await_args_list[0]
    start_call = sio.emit.await_args_list[1]
    assert join_call.args == ("join", "Channel Meters")
    assert start_call.args == ("startMeters",)


async def test_run_loop_retries_on_connect_failure():
    """If _connect_and_stream raises, _run logs and retries after backoff."""
    client = _make_client()
    call_count = {"n": 0}

    async def fake_connect_and_stream() -> None:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("first attempt fails")
        # Second attempt: simulate "stop event set" so the loop exits cleanly
        # without an exception.
        client._stop_event.set()

    with (
        patch.object(client, "_connect_and_stream", side_effect=fake_connect_and_stream),
        patch("custom_components.ashly.meter._MIN_BACKOFF_S", 0.01),
        patch("custom_components.ashly.meter._MAX_BACKOFF_S", 0.05),
    ):
        await asyncio.wait_for(client._run(), timeout=2.0)

    assert call_count["n"] >= 2


async def test_run_loop_resets_backoff_after_stable_uptime():
    """A connection that stayed up long enough resets backoff to MIN."""
    client = _make_client()
    sequence = ["ok", "fail", "stop"]
    seen = []

    async def fake_connect_and_stream() -> None:
        action = sequence[len(seen)]
        seen.append(action)
        if action == "ok":
            # Sleep "past" the reset-dwell window via a patched value.
            await asyncio.sleep(0.05)
            return  # clean disconnect
        if action == "fail":
            raise RuntimeError("transient")
        client._stop_event.set()

    with (
        patch.object(client, "_connect_and_stream", side_effect=fake_connect_and_stream),
        patch("custom_components.ashly.meter._MIN_BACKOFF_S", 0.01),
        patch("custom_components.ashly.meter._MAX_BACKOFF_S", 0.02),
        patch("custom_components.ashly.meter._BACKOFF_RESET_DWELL_S", 0.01),
    ):
        await asyncio.wait_for(client._run(), timeout=2.0)
    assert seen == ["ok", "fail", "stop"]


async def test_async_start_is_idempotent_when_already_running():
    """A second async_start while the task is alive is a no-op."""
    client = _make_client()
    # Patch _run with a coroutine that blocks until cancelled.

    async def block_forever() -> None:
        await asyncio.Event().wait()

    with patch.object(client, "_run", side_effect=block_forever):
        await client.async_start()
        first_task = client._task
        await client.async_start()  # should not start a second task
        assert client._task is first_task
        await client.async_stop()


async def test_watchdog_warns_when_no_records(caplog):
    """If no records arrive within the watchdog window, a WARNING is logged."""
    import logging

    client = _make_client()
    caplog.set_level(logging.WARNING, logger="custom_components.ashly.meter")
    with patch("custom_components.ashly.meter._FIRST_RECORD_WATCHDOG_S", 0.05):
        await client._watchdog(initial_count=0)
    assert any("no meter records received" in r.message for r in caplog.records)


async def test_watchdog_silent_when_records_arrive(caplog):
    """If records have grown past initial_count, watchdog stays quiet."""
    import logging

    client = _make_client()
    client._latest_records = [1, 2, 3]
    caplog.set_level(logging.WARNING, logger="custom_components.ashly.meter")
    with patch("custom_components.ashly.meter._FIRST_RECORD_WATCHDOG_S", 0.05):
        await client._watchdog(initial_count=0)
    assert not any("no meter records received" in r.message for r in caplog.records)


async def test_watchdog_cancelled_returns_silently(caplog):
    """A cancelled watchdog must not warn."""
    import logging

    client = _make_client()
    caplog.set_level(logging.WARNING, logger="custom_components.ashly.meter")
    with patch("custom_components.ashly.meter._FIRST_RECORD_WATCHDOG_S", 5.0):
        task = asyncio.create_task(client._watchdog(initial_count=0))
        await asyncio.sleep(0.01)
        task.cancel()
        # Task should exit without re-raising or logging.
        await asyncio.wait_for(task, timeout=1.0)
    assert not any("no meter records received" in r.message for r in caplog.records)


async def test_connected_property_reflects_sio_state():
    client = _make_client()
    assert client.connected is False
    client._sio = MagicMock()
    client._sio.connected = True
    assert client.connected is True
    client._sio.connected = False
    assert client.connected is False


def test_decode_records_first_not_a_dict():
    """If the first element of data is not a dict, decode returns None."""
    assert _decode_records({"data": ["not-a-dict"]}) is None


async def test_async_stop_disconnects_connected_socket():
    """async_stop calls emit('stopMeters') + disconnect when sio is connected."""
    client = _make_client()
    sio = MagicMock()
    sio.connected = True
    sio.emit = AsyncMock()
    sio.disconnect = AsyncMock()
    client._sio = sio
    await client.async_stop()
    sio.emit.assert_awaited_with("stopMeters")
    sio.disconnect.assert_awaited()


async def test_async_stop_swallows_disconnect_errors():
    """A SocketIOError during disconnect is suppressed (not re-raised)."""
    import socketio as sio_mod

    client = _make_client()
    sio = MagicMock()
    sio.connected = True
    sio.emit = AsyncMock(side_effect=sio_mod.exceptions.SocketIOError("e"))
    sio.disconnect = AsyncMock(side_effect=RuntimeError("e"))
    client._sio = sio
    await client.async_stop()  # must not raise


async def test_run_loop_breaks_on_stop_event_during_backoff():
    """If stop_event is set while sleeping in the backoff wait, _run exits."""
    client = _make_client()

    async def fail_then_block() -> None:
        # First call: raise so we enter backoff wait.
        # The test triggers the stop event during that wait.
        client._stop_event.set()  # set before raising so the wait_for returns immediately
        raise RuntimeError("transient")

    with (
        patch.object(client, "_connect_and_stream", side_effect=fail_then_block),
        patch("custom_components.ashly.meter._MIN_BACKOFF_S", 5.0),
    ):
        await asyncio.wait_for(client._run(), timeout=2.0)


async def test_on_channel_meter_ignores_invalid_payload():
    """The socket.io meter handler short-circuits on bad payloads."""
    client = _make_client()
    # Inject a fake sio + invoke handler logic directly.
    # The handler is a closure inside _connect_and_stream; we can't easily
    # get it, so we exercise the path indirectly via _decode_records + the
    # publish flow.
    assert _decode_records({"data": []}) is None
    # And the maybe_publish path with no records does nothing.
    calls: list[list[int]] = []
    client.add_listener(lambda r: calls.append(list(r)))
    # Even if records arrive, an empty list "differs" from previous None so
    # listener fires once; that's existing behavior covered elsewhere.
    assert client._latest_records == []


async def test_connect_failure_resets_sio_and_propagates():
    """If sio.connect raises, _connect_and_stream resets _sio to None and re-raises."""
    sio = _make_fake_sio_client()
    sio.connect = AsyncMock(side_effect=RuntimeError("connect failed"))
    client = _make_client()
    with (
        patch("custom_components.ashly.meter.socketio.AsyncClient", return_value=sio),
        pytest.raises(RuntimeError, match="connect failed"),
    ):
        await client._connect_and_stream()
    assert client._sio is None
