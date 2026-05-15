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

The client handles authentication (with re-auth on reconnect), topic
subscription, reconnection, and event parsing. Consumers receive
:class:`aquacontrol.Event` instances and are responsible for mapping
records → application state.

Listener registration supports both direct and decorator forms::

    # Direct — returns an Unsubscribe callable
    unsub = client.on_event("Set Chain Mute", handler)
    unsub()

    # Decorator — handler stays registered for the client's lifetime
    @client.listen(name="Set Chain Mute")
    async def handler(event): ...
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Self

import aiohttp

from . import topics as topics_mod
from .auth import cookie_header as _cookie_header
from .auth import fetch_session_cookies
from .events import Event, parse_event
from .stream import StreamConnection

_LOGGER = logging.getLogger(__name__)

#: A listener callback. May be **sync** (returns ``None``) or **async**
#: (returns an ``Awaitable[None]`` that the client awaits). Async
#: handlers are bounded by :data:`_HANDLER_TIMEOUT_S` so a hung handler
#: cannot stall dispatch indefinitely.
EventHandler = Callable[[Event], Awaitable[None] | None]

#: Returned by every ``on_*`` registration. Calling it removes the
#: listener. Idempotent — safe to call multiple times.
Unsubscribe = Callable[[], None]

_DEFAULT_REST_PORT = 8000
_DEFAULT_WS_PORT = 8001

# How long to wait for a single async handler to finish before logging a
# warning and moving on. Prevents one hung listener from stalling the
# rest of the dispatch chain for the same event.
_HANDLER_TIMEOUT_S = 5.0


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
    - ``session`` — optional :class:`aiohttp.ClientSession` to reuse for
      REST authentication. Pass one for connection-pool sharing with an
      existing REST client; leave as ``None`` to let the library open a
      one-shot session per login.
    - ``logger`` — optional logger. Pass a child of your application's
      logger (e.g. ``logging.getLogger("my_app").getChild("aquacontrol")``)
      so library logs show up under your namespace.
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
        session: aiohttp.ClientSession | None = None,
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
        self._session = session
        self._log = logger or _LOGGER

        # Listener registries. Each handler ID is a unique int so removing
        # the same handler twice is safe (idempotent).
        self._next_id = 0
        self._event_listeners: dict[int, tuple[str, EventHandler]] = {}
        self._topic_listeners: dict[int, tuple[str, EventHandler]] = {}
        self._any_listeners: dict[int, EventHandler] = {}

        self._stream: StreamConnection | None = None
        #: Set explicitly via :meth:`set_session_id`. Used by
        #: :meth:`is_own_event` to filter echoes of mutations this client
        #: triggered (when the caller knows its session UUID).
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
        """The session ID assigned via :meth:`set_session_id`, or ``None``
        if the caller hasn't supplied one.

        Use :meth:`is_own_event` for the common case of suppressing
        own-echoes during optimistic updates.
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

        Performs an initial authentication so callers fail fast on bad
        credentials. The same credentials are re-used on every subsequent
        reconnect via the cookie-provider callback wired into the stream,
        so long-running deployments survive the device rotating session
        cookies (e.g. across reboots) without manual reauth.

        Calling ``connect()`` while already connected is a no-op — the
        existing stream's reconnect loop continues to run. This makes
        the method safe to call from idempotent setup paths (e.g. HA's
        ``async_setup_entry`` after a reload).

        Returns once the background task has been started — does NOT wait
        for the first WebSocket connection. Poll :attr:`connected` if
        you need to know.
        """
        if self._stream is not None:
            # Already started; don't orphan the existing task by overwriting
            # self._stream with a fresh StreamConnection.
            return
        # Validate credentials up-front so callers can fail-fast on bad
        # creds rather than discover the failure asynchronously inside
        # the reconnect loop. AquaControlAuthError surfaces here.
        await self._cookie_provider()
        self._stream = StreamConnection(
            host=self._host,
            port=self._ws_port,
            topics=self._topics,
            cookie_provider=self._cookie_provider,
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

    async def _cookie_provider(self) -> str:
        """Re-authenticate and return a fresh ``Cookie:`` header.

        Invoked by :class:`StreamConnection` before each connect attempt.
        Uses the supplied :class:`aiohttp.ClientSession` (if any) for
        connection-pool reuse.
        """
        cookies = await fetch_session_cookies(
            self._host,
            port=self._rest_port,
            username=self._username,
            password=self._password,
            session=self._session,
        )
        return _cookie_header(cookies)

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

    def listen(
        self,
        *,
        name: str | None = None,
        topic: str | None = None,
    ) -> Callable[[EventHandler], EventHandler]:
        """Decorator-friendly listener registration.

        Returns a decorator that registers the wrapped function and
        returns it unchanged. Unlike :meth:`on_event` / :meth:`on_topic`
        / :meth:`on_any`, the caller gets the original function back
        (not an :data:`Unsubscribe` callable), making the result usable
        as a Python decorator::

            @client.listen(name="Set Chain Mute")
            async def handle_mute(event): ...

            @client.listen(topic="Preset")
            async def handle_preset(event): ...

            @client.listen()  # catch-all
            async def handle_any(event): ...

        Trade-off: decorator-registered handlers can't be unsubscribed
        explicitly — they live for the lifetime of the client. Use the
        direct ``on_*`` methods when you need an :data:`Unsubscribe`.
        """
        if name is not None and topic is not None:
            raise ValueError("listen(): pass at most one of name or topic")

        def _decorator(handler: EventHandler) -> EventHandler:
            if name is not None:
                self._register(self._event_listeners, (name, handler))
            elif topic is not None:
                self._register(self._topic_listeners, (topic, handler))
            else:
                self._register(self._any_listeners, handler)
            return handler

        return _decorator

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
        the patched ``_trigger_event``. Builds an :class:`Event` and
        dispatches to listeners in this order: any-listeners, then
        topic-listeners, then event-name-listeners. Each handler runs
        under a bounded timeout; one handler hanging does not block the
        rest of the dispatch chain or kill the stream.
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
        """Invoke one listener under a bounded timeout.

        Sync handlers run inline. Async handlers are awaited with
        :func:`asyncio.wait_for` so a single misbehaving listener can't
        stall the dispatch chain forever. The handler's exception (and
        timeout) are caught and logged.
        """
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await asyncio.wait_for(result, timeout=_HANDLER_TIMEOUT_S)
        except TimeoutError:
            self._log.warning(
                "AquaControl listener for %s/%s exceeded %.1fs — handler hung",
                event.topic,
                event.name,
                _HANDLER_TIMEOUT_S,
            )
        except Exception:
            self._log.exception(
                "AquaControl listener raised for %s/%s", event.topic, event.name
            )

    # ── Session-id management + echo filtering ────────────────────

    def set_session_id(self, session_id: str | int | None) -> None:
        """Pin the client's own session ID for echo filtering.

        Consumers that know their REST session UUID (e.g. from a
        ``Set-Cookie`` header on login, or from the ``uniqueId`` field
        of the first mutation they triggered) should call this and then
        use :meth:`is_own_event` to suppress double-application of
        optimistic updates.
        """
        self._session_id = session_id

    def is_own_event(self, event: Event) -> bool:
        """Return True if ``event`` was emitted in response to a mutation
        triggered by this client's session.

        Always False until :meth:`set_session_id` has been called.
        Wraps :meth:`Event.is_from_session` with the client's pinned ID
        so callers don't have to thread the session ID through every
        listener::

            client.set_session_id(my_uuid)

            @client.listen(name="Set Chain Mute")
            async def handle(event):
                if client.is_own_event(event):
                    return  # already applied optimistically
                apply_to_state(event)
        """
        return event.is_from_session(self._session_id)

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


__all__ = ["AquaControlClient", "EventHandler", "Unsubscribe"]
