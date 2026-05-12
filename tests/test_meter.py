"""Tests for the live meter client."""

from __future__ import annotations

import aiohttp

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
