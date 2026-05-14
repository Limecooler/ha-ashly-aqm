"""High-level AquaControl push-API client.

Typical usage::

    from aquacontrol import AquaControlClient

    async with AquaControlClient(
        host="192.168.1.100",
        username="haassistant",
        password="…",
    ) as client:
        client.on_event("Set Chain Mute", handle_mute)
        client.on_topic("Preset", handle_preset_lifecycle)
        client.on_any(log_everything)
        await asyncio.sleep(3600)  # listen for an hour

The client handles authentication, topic subscription, reconnection, and
event parsing. Consumers receive :class:`aquacontrol.Event` instances and
are responsible for mapping records → application state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Self, cast

from . import topics as topics_mod
from .auth import cookie_header as _cookie_header
from .auth import fetch_session_cookies
from .events import Event, parse_event
from .stream import StreamConnection

_LOGGER = logging.getLogger(__name__)

#: Listener signature. May be sync or async; sync handlers are awaited via
#: ``asyncio.iscoroutine``.
EventHandler = Callable[[Event], Awaitable[None] | None]

#: Cancellation token returned by every ``on_*`` registration call. Call it
#: to remove the listener; idempotent.
Unsubscribe = Callable[[], None]

_DEFAULT_REST_PORT = 8000
_DEFAULT_WS_PORT = 8001


class AquaControlClient:
    """Authenticated Socket.IO client for an AquaControl device.

    Parameters mirror the device's URL layout:

    - ``host`` — IP or hostname.
    - ``rest_port`` — REST/login port. Defaults to 8000.
    - ``ws_port`` — Socket.IO port. Defaults to 8001.
    - ``username`` / ``password`` — credentials accepted by the device's
      ``/v1.0-beta/session/login`` endpoint. Use a dedicated service
      account; see docs/SECURITY-API.md in the reverse-engineering repo.
    - ``topics`` — which topics to subscribe to. Default: every known
      topic (see :data:`aquacontrol.topics.ALL_TOPICS`). Pass an empty
      sequence to subscribe to nothing.
    """

    def __init__(
        self,
        *,
        host: str,
        username: str,
        password: str,
        rest_port: int = _DEFAULT_REST_PORT,
        ws_port: int = _DEFAULT_WS_PORT,
        topics: Iterable[str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._host = host
        self._rest_port = int(rest_port)
        self._ws_port = int(ws_port)
        self._username = username
        self._password = password
        self._topics: tuple[str, ...] = (
            tuple(topics) if topics is not None else topics_mod.ALL_TOPICS
        )
        self._log = logger or _LOGGER

        # Listener registries. Each handler ID is a unique int so removing
        # the same handler twice is safe (idempotent).
        self._next_id = 0
        self._event_listeners: dict[int, tuple[str, EventHandler]] = {}
        self._topic_listeners: dict[int, tuple[str, EventHandler]] = {}
        self._any_listeners: dict[int, EventHandler] = {}

        self._stream: StreamConnection | None = None
        #: Resolved on the first echo we observe of one of our own emits.
        #: Used to filter own-echoes via :meth:`Event.is_from_session`.
        self._session_id: str | int | None = None

    # ── Properties ────────────────────────────────────────────────

    @property
    def host(self) -> str:
        return self._host

    @property
    def connected(self) -> bool:
        return self._stream is not None and self._stream.connected

    @property
    def session_id(self) -> str | int | None:
        """The originating-session UUID for events triggered by this client.

        ``None`` until the device echoes one of our own mutations back to
        us. Consumers using optimistic state updates should pass this to
        :meth:`Event.is_from_session` to suppress double-application.
        """
        return self._session_id

    @property
    def topics(self) -> tuple[str, ...]:
        return self._topics

    # ── Lifecycle ─────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        """Authenticate, open the WebSocket, and start the reconnect loop.

        Returns once the background task has been started — does NOT wait
        for the first successful connection. Consumers can poll
        :attr:`connected` if they need to know.
        """
        cookies = await fetch_session_cookies(
            self._host,
            port=self._rest_port,
            username=self._username,
            password=self._password,
        )
        header = _cookie_header(cookies)
        self._stream = StreamConnection(
            host=self._host,
            port=self._ws_port,
            topics=self._topics,
            cookie_header=header,
            on_event=self._on_raw_event,
            logger=self._log,
        )
        await self._stream.start()

    async def disconnect(self) -> None:
        """Tear down the WebSocket and stop reconnect attempts."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            await stream.stop()

    # ── Listener registration ─────────────────────────────────────

    def on_event(self, name: str, handler: EventHandler) -> Unsubscribe:
        """Register a handler for events with inner ``name == name`` on
        any topic.

        Returns a callable that removes the handler when invoked.
        """
        return self._register(self._event_listeners, (name, handler))

    def on_topic(self, topic: str, handler: EventHandler) -> Unsubscribe:
        """Register a handler for every event arriving on ``topic``."""
        return self._register(self._topic_listeners, (topic, handler))

    def on_any(self, handler: EventHandler) -> Unsubscribe:
        """Register a handler for every event regardless of topic/name.

        Useful for logging, metrics, or building higher-level abstractions
        atop the raw stream. Heartbeat/meter frames are included — filter
        with :attr:`Event.is_ambient` and :attr:`Event.is_meter` if you
        only want state changes.
        """
        return self._register(self._any_listeners, handler)

    def _register(
        self,
        registry: dict[int, Any],
        entry: Any,
    ) -> Unsubscribe:
        handler_id = self._next_id
        self._next_id += 1
        registry[handler_id] = entry

        def _remove() -> None:
            registry.pop(handler_id, None)

        return _remove

    # ── Dispatch ──────────────────────────────────────────────────

    async def _on_raw_event(self, topic: str, payload: Any) -> None:
        """Fan a raw event out to all registered listeners.

        Called by :class:`StreamConnection` for every event we receive on
        the patched ``_trigger_event``. Builds an :class:`Event`, records
        our own session ID if this is the first echo of our work, and
        dispatches to listeners in this order: any-listeners, then
        topic-listeners, then event-name-listeners. Exceptions in one
        listener don't propagate to others.
        """
        event = parse_event(topic, payload)
        await self._dispatch(event)

    async def _dispatch(self, event: Event) -> None:
        # 1) catch-alls
        for handler in list(self._any_listeners.values()):
            await self._call(handler, event)
        # 2) topic-specific
        for topic, handler in list(self._topic_listeners.values()):
            if topic == event.topic:
                await self._call(handler, event)
        # 3) event-name-specific
        for name, handler in list(self._event_listeners.values()):
            if name == event.name:
                await self._call(handler, event)

    async def _call(self, handler: EventHandler, event: Event) -> None:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            self._log.exception(
                "AquaControl listener raised for %s/%s", event.topic, event.name
            )

    # ── Session-id management ─────────────────────────────────────

    def set_session_id(self, session_id: str | int | None) -> None:
        """Pin the client's own session ID for echo filtering.

        Consumers that already know their REST session UUID (e.g. from a
        ``Set-Cookie`` header on login, or from the response body of the
        first mutation they triggered) should call this to enable
        :meth:`Event.is_from_session` immediately. Otherwise the client
        will infer it lazily from the first state-change echo it sees.
        """
        self._session_id = session_id

    # ── Manual topic management (advanced) ────────────────────────

    async def join(self, topic: str) -> None:
        """Subscribe to an additional topic at runtime.

        The topic is also added to the rejoin set used after reconnect.
        Joining an unknown topic is a silent no-op on the device side.
        """
        if topic in self._topics:
            return
        self._topics = (*self._topics, topic)
        if self._stream is not None:
            await self._stream.emit("join", topic)

    async def leave(self, topic: str) -> None:
        """Unsubscribe from a topic."""
        self._topics = tuple(t for t in self._topics if t != topic)
        if self._stream is not None:
            await self._stream.emit("leave", topic)

    # ── Introspection (for tests / diagnostics) ───────────────────

    @property
    def listener_count(self) -> int:
        """Total number of registered listeners across all kinds."""
        return (
            len(self._any_listeners)
            + len(self._topic_listeners)
            + len(self._event_listeners)
        )


# Re-export for explicit callable typing on the public surface.
__all__ = ["AquaControlClient", "EventHandler", "Unsubscribe"]

# Avoid an unused-import warning when type-checking.
_ = cast
