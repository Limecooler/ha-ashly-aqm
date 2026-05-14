"""Low-level Socket.IO connection wrapper.

Wraps ``socketio.AsyncClient`` with the AquaControl-specific bits:

- carries an authentication cookie in the WebSocket handshake (required
  to receive state-change events; see docs/WEBSOCKET-API.md §1.1),
- joins every requested topic on (re)connect,
- patches ``_trigger_event`` so we receive *all* topics rather than
  having to register a Python-level ``@sio.on(...)`` per topic,
- exposes a single ``on_event`` callable that the high-level
  :class:`aquacontrol.AquaControlClient` fans events out from,
- handles reconnect with exponential backoff + jitter.

This module is intentionally narrow — it does no event parsing, no
listener dispatch, no echo filtering. All higher-level concerns live in
:mod:`aquacontrol.client`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable, Iterable
from typing import Any

import socketio

from .exceptions import AquaControlConnectionError

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff bounds. Min is also the floor after jitter so an
# unlucky random draw can't immediately retry.
_MIN_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0

# A connection that stays up at least this long is treated as "good";
# the next disconnect resets backoff to _MIN_BACKOFF_S so we don't
# accumulate cruft from a long-running connection that finally drops.
_GOOD_CONNECTION_DWELL_S = 30.0

#: Type of a raw event callback. Receives ``(topic, payload)`` where
#: ``topic`` is the outer Socket.IO event name and ``payload`` is the
#: inner data dict (or whatever the server sent). Sync handlers may return
#: ``None``; async handlers return a coroutine that will be awaited.
RawEventCallback = Callable[[str, Any], Any]


def _next_backoff(current: float) -> float:
    """Double, clamp, ±30 % jitter, then re-clamp. Returns a delay in
    ``[_MIN_BACKOFF_S, _MAX_BACKOFF_S]``."""
    doubled = min(_MAX_BACKOFF_S, current * 2)
    jittered = doubled * (0.7 + random.random() * 0.6)
    return max(_MIN_BACKOFF_S, min(_MAX_BACKOFF_S, jittered))


class StreamConnection:
    """Manages a single Socket.IO connection to an AquaControl device.

    Lifecycle:

    1. ``await conn.start()`` — kicks off a background task that connects,
       joins topics, dispatches events. Returns immediately; the task
       reconnects in the background.
    2. Events fan out via the ``on_event`` callback you supply at
       construction.
    3. ``await conn.stop()`` — clean shutdown; cancels the task and closes
       the underlying socket.

    The class does NOT block on connection success at ``start()`` — the
    AquaControl integration's UX requires that HA's setup succeed even
    if the device is briefly unreachable, and the reconnect loop covers
    transient outages without user-visible failures.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        topics: Iterable[str],
        cookie_header: str | None,
        on_event: RawEventCallback,
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._topics = tuple(topics)
        self._cookie_header = cookie_header
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._log = logger or _LOGGER
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._backoff = _MIN_BACKOFF_S
        # Reserved event names — socket.io's own lifecycle signals that
        # aren't part of the AquaControl topic stream and that should
        # never reach the application's on_event callback.
        self._reserved = frozenset(
            {"connect", "disconnect", "reconnect", "connect_error", "message"}
        )

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @property
    def connected(self) -> bool:
        return self._sio is not None and self._sio.connected

    async def start(self) -> None:
        """Start the background connect-and-reconnect loop."""
        if self._task is not None and not self._task.done():
            return  # already running
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="aquacontrol-stream")

    async def stop(self) -> None:
        """Stop the loop and close the socket."""
        self._stopping = True
        sio = self._sio
        if sio is not None:
            with contextlib.suppress(Exception):
                await sio.disconnect()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        """Connect-loop body. Survives any non-cancellation exception."""
        while not self._stopping:
            connect_start = asyncio.get_running_loop().time()
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._log.warning(
                    "AquaControl stream connect failed (%s); retrying in %.1fs",
                    err,
                    self._backoff,
                )
            if self._stopping:
                return
            # If the connection stayed up long enough, treat it as healthy
            # and reset backoff for the next failure.
            elapsed = asyncio.get_running_loop().time() - connect_start
            if elapsed >= _GOOD_CONNECTION_DWELL_S:
                self._backoff = _MIN_BACKOFF_S
            await asyncio.sleep(self._backoff)
            self._backoff = _next_backoff(self._backoff)

    async def _connect_once(self) -> None:
        """One connect-and-listen pass. Returns when the socket disconnects."""
        sio = socketio.AsyncClient(reconnection=False, logger=False, engineio_logger=False)
        self._sio = sio
        self._install_trigger_patch(sio)

        async def _on_connect() -> None:
            self._log.debug("AquaControl stream connected to %s", self.url)
            if self._on_connect is not None:
                await _maybe_await(self._on_connect())
            # Rejoin every topic. Joining is idempotent on the device side.
            for topic in self._topics:
                try:
                    await sio.emit("join", topic)
                except Exception:
                    self._log.exception("emit('join', %r) failed", topic)

        async def _on_disconnect() -> None:
            self._log.debug("AquaControl stream disconnected from %s", self.url)
            if self._on_disconnect is not None:
                await _maybe_await(self._on_disconnect())

        sio.on("connect", _on_connect)
        sio.on("disconnect", _on_disconnect)

        headers: dict[str, str] = {}
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        try:
            await sio.connect(self.url, transports=["websocket"], headers=headers)
        except socketio.exceptions.ConnectionError as err:
            raise AquaControlConnectionError(str(err)) from err
        # Block until disconnect — sio.wait() returns when the socket closes.
        await sio.wait()
        self._sio = None

    def _install_trigger_patch(self, sio: socketio.AsyncClient) -> None:
        """Hook into the client so every incoming event reaches us.

        ``socketio.AsyncClient._trigger_event`` is the device-side
        dispatcher; intercepting it lets us see every topic without
        having to register a per-topic handler. The signature varies
        across python-socketio versions (positional vs keyword
        ``namespace``), so we accept both via ``*args, **kwargs``.
        """
        original = sio._trigger_event

        async def patched(event: str, *args: Any, **kwargs: Any) -> Any:
            if event not in self._reserved and not event.startswith("__"):
                # For application events the call signature is
                # ``(name, namespace, *data)`` — payload is args[1].
                payload = args[1] if len(args) >= 2 else None
                try:
                    result = self._on_event(event, payload)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    self._log.exception("Listener for %r raised", event)
            return await original(event, *args, **kwargs)

        sio._trigger_event = patched

    async def emit(self, event: str, data: Any = None) -> None:
        """Forward an emit through the underlying socket (e.g. for testing).

        Returns silently if the socket isn't currently connected.
        """
        sio = self._sio
        if sio is None or not sio.connected:
            return
        await sio.emit(event, data)


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value
