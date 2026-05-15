"""Low-level Socket.IO connection wrapper.

Wraps ``socketio.AsyncClient`` with the AquaControl-specific bits:

- carries an authentication cookie (or a re-authentication callback) in
  the WebSocket handshake,
- joins every requested topic on (re)connect,
- patches ``_trigger_event`` so we receive *all* topics rather than
  having to register a Python-level ``@sio.on(...)`` per topic,
- exposes a single ``on_event`` callable that the high-level
  :class:`aquacontrol.AquaControlClient` fans events out from,
- handles reconnect with exponential backoff + per-instance jitter.

This module is intentionally narrow — it does no event parsing, no
listener dispatch, no echo filtering. All higher-level concerns live in
:mod:`aquacontrol.client`.

Implementation note: the ``_trigger_event`` interception in
:meth:`StreamConnection._install_trigger_patch` reaches into a private
attribute of ``socketio.AsyncClient``. That attribute's call signature is
stable across the 5.x family but is not part of the public API; the
package's dependency declaration pins to ``python-socketio>=5.10,<6.0``
and the class additionally checks for its presence at runtime so a
silent upstream rename surfaces as :class:`AquaControlProtocolError`
rather than a confusing AttributeError.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import socketio

from .exceptions import AquaControlConnectionError, AquaControlProtocolError

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

#: Async callable that returns a ready-to-use ``Cookie:`` header string
#: (or ``None`` to connect without authentication). Invoked before each
#: connect attempt so the library can refresh credentials on reconnect.
CookieProvider = Callable[[], Awaitable[str | None]]


def _next_backoff(current: float, *, rng: random.Random | None = None) -> float:
    """Double, clamp, ±30 % jitter, then re-clamp. Returns a delay in
    ``[_MIN_BACKOFF_S, _MAX_BACKOFF_S]``.

    Pass ``rng`` to use a per-instance source. Without it, falls back to
    the module-global ``random`` (only safe for single-instance use).
    """
    source = rng if rng is not None else random
    doubled = min(_MAX_BACKOFF_S, current * 2)
    jittered = doubled * (0.7 + source.random() * 0.6)
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

    **Authentication:** pass ``cookie_provider`` (preferred) to enable
    re-authentication on each reconnect. The provider is invoked before
    each connect attempt and its returned ``Cookie:`` header is included
    in the WebSocket handshake. Passing ``cookie_header`` (legacy)
    provides a static cookie that never refreshes — use only for tests
    or short-lived scripts.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        topics: Iterable[str],
        on_event: RawEventCallback,
        cookie_header: str | None = None,
        cookie_provider: CookieProvider | None = None,
        on_connect: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._topics = tuple(topics)
        self._cookie_header = cookie_header
        self._cookie_provider = cookie_provider
        self._on_event = on_event
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._log = logger or _LOGGER
        self._sio: socketio.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._backoff = _MIN_BACKOFF_S
        # Per-instance RNG so multiple StreamConnections (e.g. several
        # AQM devices configured in the same HA instance) don't synchronise
        # their reconnect jitter after a network blip.
        self._rng = random.Random()
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
                # Log only the exception TYPE, not its repr — exception
                # messages from aiohttp / socketio can include URLs and
                # connection headers we don't want in user logs.
                self._log.warning(
                    "AquaControl stream connect failed (%s); retrying in %.1fs",
                    type(err).__name__,
                    self._backoff,
                )
            if self._stopping:
                # Exit immediately on shutdown request; don't sleep.
                return
            # If the connection stayed up long enough, treat it as healthy
            # and reset backoff for the next failure.
            elapsed = asyncio.get_running_loop().time() - connect_start
            if elapsed >= _GOOD_CONNECTION_DWELL_S:
                self._backoff = _MIN_BACKOFF_S
            await asyncio.sleep(self._backoff)
            self._backoff = _next_backoff(self._backoff, rng=self._rng)

    async def _resolve_cookie_header(self) -> str | None:
        """Return the ``Cookie:`` header value to use for the next connect.

        If a :data:`CookieProvider` was supplied, it's invoked fresh on
        every call so reconnects pick up rotated session cookies. Falls
        back to the static ``cookie_header`` constructor arg.
        """
        if self._cookie_provider is not None:
            return await self._cookie_provider()
        return self._cookie_header

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

        # Resolve the cookie *now*, not at construction — this is what
        # makes reconnects re-authenticate when a cookie_provider is set.
        cookie_header_value = await self._resolve_cookie_header()
        headers: dict[str, str] = {}
        if cookie_header_value:
            headers["Cookie"] = cookie_header_value
        try:
            await sio.connect(self.url, transports=["websocket"], headers=headers)
        except socketio.exceptions.ConnectionError:
            # Drop the exception chain (`from None`): the original
            # socketio.ConnectionError can carry handshake headers in its
            # repr, which our public exception shouldn't propagate.
            raise AquaControlConnectionError(
                f"WebSocket handshake failed for {self._host}:{self._port}"
            ) from None
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

        Raises :class:`AquaControlProtocolError` if the expected private
        attribute is missing — that's the early-warning signal that the
        installed ``python-socketio`` version isn't compatible (e.g. a
        6.x install slipped past the dependency pin).
        """
        original = getattr(sio, "_trigger_event", None)
        if not callable(original):
            raise AquaControlProtocolError(
                "socketio.AsyncClient has no callable _trigger_event hook — "
                "incompatible python-socketio version. Requires 5.x.",
                transient=False,
            )

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
