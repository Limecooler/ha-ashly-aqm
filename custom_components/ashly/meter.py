"""Live meter client for the Ashly AquaControl websocket.

The AquaControl Portal serves a socket.io 4.x endpoint on port 8001 of the
device. The session cookie obtained via `/session/login` on port 8000 is
honoured by the socket.io server.

Protocol (reverse-engineered from the AquaControl UI):

- Connect to ``ws://<host>:8001/socket.io/`` via the standard websocket
  transport.
- Emit ``join`` with ``"Channel Meters"`` to subscribe to the channel-meter
  topic. (``"Block Meters"`` is also available but on this device it never
  fires because no Gain DSP blocks are configured.)
- Emit ``startMeters`` (no payload) to ask the server to begin streaming.
- The server emits events named ``"Channel Meters"`` whose ``records`` field
  is a flat array of integers. Each position maps to a meter index defined
  by ``GET /workingsettings/dsp/channel/metermap``:
    * positions 0..11: rear-panel mic/line inputs 1..12 (post-preamp)
    * positions 12..23: post-DSP mixer inputs 1..12
    * positions 24..N: per-mixer automix per-input meters (only meaningful
      when automix is enabled).
- The integer at each position is a dB value above the meter's floor
  (-60 dBu). Converting back: ``dB = raw + min`` where min is read from
  ``GET /workingsettings/dsp/channel/meterParameter``.
- Updates arrive at roughly 6 Hz; we throttle publish to the rest of the
  integration to 1 Hz so the HA recorder isn't flooded.

The client runs as a background task with capped exponential backoff so
brief network blips don't propagate to entity unavailability. The cookie
jar is shared with the REST client, so a coordinator-driven re-login
seamlessly refreshes the next reconnect's credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from typing import Any

import aiohttp
import socketio

from .const import METER_INPUT_RANGE_DB, METER_PUBLISH_INTERVAL_S, METER_WS_PORT

_LOGGER = logging.getLogger(__name__)

# Initial reconnect delay; doubles on each failure up to _MAX_BACKOFF.
_MIN_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0

# A connection must stay up at least this long before we treat the next
# disconnect as fresh and reset the backoff. Prevents reconnect storms
# when a firmware quirk drops the socket cleanly every few seconds.
_BACKOFF_RESET_DWELL_S = 30.0

# Warn if the device doesn't emit a meter record this many seconds after
# we've successfully called startMeters.
_FIRST_RECORD_WATCHDOG_S = 10.0

# Meter event name emitted by the device's socket.io server.
_CHANNEL_METER_EVENT = "Channel Meters"

# Meter-value floor (silent / disconnected channel). Equal to range minimum.
METER_FLOOR_DB = METER_INPUT_RANGE_DB[0]


def _next_backoff(current: float) -> float:
    """Double the current backoff, clamp to MAX, then apply ±30% jitter.

    Jitter prevents N devices on the same LAN from all reconnecting on the
    same tick after a network outage. Floor at _MIN_BACKOFF_S so a jittered
    value can't go below the minimum poll interval.
    """
    doubled = min(_MAX_BACKOFF_S, current * 2)
    jittered = doubled * (0.7 + random.random() * 0.6)
    return max(_MIN_BACKOFF_S, jittered)


def _decode_records(payload: Any) -> list[int] | None:
    """Pull the meter-int array out of a socket.io 'Channel Meters' payload."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    records = first.get("records")
    return records if isinstance(records, list) else None


def raw_to_db(raw: int | float) -> float:
    """Convert a raw meter integer to dBu using the device's documented floor.

    Values are stored as integer offsets above ``METER_FLOOR_DB``; e.g. 0
    means -60 dBu (silent), 60 means 0 dBu, 80 means +20 dBu (clip).
    """
    try:
        return float(raw) + METER_FLOOR_DB
    except (TypeError, ValueError):
        return METER_FLOOR_DB


class AshlyMeterClient:
    """Background socket.io client that publishes the latest meter snapshot.

    The integration constructs one per device. After ``async_start()`` it
    runs forever (the background task survives disconnect/reconnect) until
    ``async_stop()`` is called.

    Other integration pieces consume the meter snapshot by either:
      - Reading ``client.latest_records`` directly (a list[int]), or
      - Registering a callback via ``add_listener`` which fires no more
        than once per ``METER_PUBLISH_INTERVAL_S`` and only when the
        records actually change.
    """

    def __init__(
        self,
        host: str,
        port: int,
        cookie_jar: aiohttp.CookieJar,
        *,
        socketio_port: int = METER_WS_PORT,
    ) -> None:
        self._host = host
        self._port = port  # REST port, used for documentation/logging only
        self._cookie_jar = cookie_jar
        self._socketio_port = socketio_port
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._listeners: list[Callable[[list[int]], None]] = []
        self._latest_records: list[int] = []
        self._last_publish: float = 0.0

    # ── Lifecycle ───────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Start the background reconnect loop (no-op if already running)."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name=f"ashly-meters[{self._host}]")

    async def async_stop(self) -> None:
        """Stop streaming and tear down the websocket cleanly.

        Safe to call multiple times; safe to call before `async_start`.
        """
        self._stop_event.set()
        if self._sio is not None and self._sio.connected:
            # Suppress — we're tearing down anyway.
            with contextlib.suppress(socketio.exceptions.SocketIOError, RuntimeError):
                await self._sio.emit("stopMeters")
            with contextlib.suppress(socketio.exceptions.SocketIOError, RuntimeError):
                await self._sio.disconnect()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    # ── Subscription ───────────────────────────────────────────────

    def add_listener(self, callback: Callable[[list[int]], None]) -> Callable[[], None]:
        """Register a callback that fires when a new meter snapshot is ready.

        Returns a remove-listener function. Throttled to one call per
        `METER_PUBLISH_INTERVAL_S` and only fired when the records array
        differs from the previously-published snapshot.
        """
        self._listeners.append(callback)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(callback)

        return _remove

    @property
    def latest_records(self) -> list[int]:
        """The most recent meter array (empty until the first message)."""
        return self._latest_records

    @property
    def connected(self) -> bool:
        return self._sio is not None and self._sio.connected

    # ── Internals ──────────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = _MIN_BACKOFF_S
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            connect_start = loop.time()
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # A single transient error is normal; back off and retry.
                # Escalate to WARNING once we hit the max backoff, since
                # at that point the device has been unreachable for
                # ~minute and the user would want to know.
                level = logging.WARNING if backoff >= _MAX_BACKOFF_S else logging.INFO
                _LOGGER.log(
                    level,
                    "[%s] meter websocket disconnected (%s); reconnecting in %.1fs",
                    self._host,
                    err,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    break  # stop requested
                except TimeoutError:
                    pass
                backoff = _next_backoff(backoff)
                continue
            # Clean disconnect → reset backoff only if the connection
            # was stable for a while; otherwise treat it as a flap and
            # keep doubling.
            uptime = loop.time() - connect_start
            backoff = _MIN_BACKOFF_S if uptime >= _BACKOFF_RESET_DWELL_S else _next_backoff(backoff)

    async def _connect_and_stream(self) -> None:
        # Build a dedicated aiohttp session with the threaded resolver so we
        # don't drag in pycares (its background thread is hostile to HA's
        # test fixtures and provides no value for the embedded LAN device).
        # Share the integration's cookie jar so a REST-driven re-auth
        # transparently refreshes credentials on this connection's next
        # reconnect.
        http_session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                resolver=aiohttp.ThreadedResolver(),
                force_close=True,
                enable_cleanup_closed=False,
            ),
            cookie_jar=self._cookie_jar,
        )
        sio = socketio.AsyncClient(reconnection=False)
        # socketio.AsyncClient documents an `http_session` kwarg but doesn't
        # propagate it to the underlying engineio client — wire it manually.
        if not hasattr(sio.eio, "external_http"):  # pragma: no cover
            _LOGGER.warning(
                "[%s] python-socketio internal layout changed; "
                "meter websocket may misbehave — please report",
                self._host,
            )
        sio.eio.http = http_session
        sio.eio.external_http = True
        self._sio = sio

        @sio.on(_CHANNEL_METER_EVENT)  # type: ignore[untyped-decorator]
        async def _on_channel_meter(payload: Any) -> None:
            records = _decode_records(payload)
            if records is None:
                return
            self._latest_records = records
            self._maybe_publish(records)

        try:
            try:
                # The cookie jar wires cookies through aiohttp automatically;
                # no manual Cookie header needed. A previous re-auth therefore
                # propagates here on the next reconnect.
                await sio.connect(
                    f"http://{self._host}:{self._socketio_port}",
                    transports=["websocket"],
                )
            except Exception:
                self._sio = None
                raise
            await sio.emit("join", "Channel Meters")
            await sio.emit("startMeters")
            _LOGGER.info("[%s] meter websocket streaming", self._host)

            # Watchdog: warn if the device never emits a record after
            # we've successfully subscribed.
            initial_record_count = len(self._latest_records)
            watchdog = asyncio.create_task(
                self._watchdog(initial_record_count),
                name=f"ashly-meters-watchdog[{self._host}]",
            )

            # Block until disconnect or stop. socketio.AsyncClient.wait()
            # blocks until the connection ends; we race it against the
            # stop event.
            wait_task = asyncio.create_task(sio.wait())
            stop_task = asyncio.create_task(self._stop_event.wait())
            try:
                await asyncio.wait([wait_task, stop_task], return_when=asyncio.FIRST_COMPLETED)
            finally:
                wait_task.cancel()
                stop_task.cancel()
                watchdog.cancel()
                with contextlib.suppress(socketio.exceptions.SocketIOError, RuntimeError):
                    await sio.disconnect()
                self._sio = None
        finally:
            if not http_session.closed:
                await http_session.close()

    async def _watchdog(self, initial_count: int) -> None:
        """Warn once if no meter records arrive within the watchdog window."""
        try:
            await asyncio.sleep(_FIRST_RECORD_WATCHDOG_S)
        except asyncio.CancelledError:
            return
        if len(self._latest_records) <= initial_count:
            _LOGGER.warning(
                "[%s] no meter records received within %.0fs of subscribing; "
                "the device may not be emitting them",
                self._host,
                _FIRST_RECORD_WATCHDOG_S,
            )

    def _maybe_publish(self, records: list[int]) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now - self._last_publish < METER_PUBLISH_INTERVAL_S:
            return
        # Cheap dedupe — when nothing has changed since the last publish,
        # spare every listener (and the recorder) a no-op state write.
        prev = getattr(self, "_last_published_records", None)
        if prev is not None and prev == records:
            self._last_publish = now
            return
        self._last_publish = now
        self._last_published_records = list(records)
        for listener in list(self._listeners):
            try:
                listener(records)
            except Exception as err:
                _LOGGER.warning(
                    "[%s] meter listener %r raised: %s",
                    self._host,
                    listener,
                    err,
                    exc_info=True,
                )
