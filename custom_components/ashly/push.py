"""AquaControl push-event client for the Ashly integration.

Thin adapter on top of :class:`aquacontrol.AquaControlClient`. Owns the
WebSocket connection, registers one handler per routable event name
(from :mod:`.event_router`) plus a topic-level handler for ``Preset``
(which always triggers a coordinator refresh) and an ``on_any``
heartbeat handler that stamps the staleness timestamp on every event,
ambient ones included.

The dispatcher applies push events to coordinator state via three
sentinels returned from :func:`event_router.route_event`:

- a new :class:`AshlyDeviceData` → ``coordinator.async_set_updated_data``;
- :data:`event_router.NO_CHANGE` → no fan-out (value already matched);
- ``None`` → ``coordinator.async_request_refresh`` (debounced; coalesces
  Preset-Recall storms into a single poll).

There is no echo filter on the dispatch path — we rely on HA's
``always_update=False`` plus dataclass equality to suppress redundant
state-update fan-outs when the device echoes a mutation HA just made.
This is cheaper than threading own-session UUIDs through every write
path, and survives the case where the device's post-mutation truth
differs from HA's optimistic patch (e.g. clipping at the device-side
parameter bounds).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp
from aquacontrol.topics import PRESET

from aquacontrol import (
    AquaControlClient,
    Event,
)

from .event_router import ROUTABLE_EVENT_NAMES, _NoChange, route_event

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.core import HomeAssistant

    from .coordinator import AshlyCoordinator

_LOGGER = logging.getLogger(__name__)

# Cap on the per-kind counter so a buggy or hostile device can't grow the
# dict without bound. Routable event names are pre-seeded at construction
# time, so familiar names always appear in diagnostics (even at zero
# count); unfamiliar names beyond this cap roll into ``_other``.
_EVENTS_BY_KIND_CAP = 200
_EVENTS_BY_KIND_OVERFLOW = "_other"


@dataclass(slots=True)
class PushStats:
    """Cheap accumulators exposed via diagnostics.

    Lifetime of the AshlyPushClient — reset on every reload. ``last_error``
    is the most recent exception caught inside a handler (the library
    catches its own; this captures router/dispatch failures specifically).
    Stored as a sanitized type-name string rather than the raw exception
    so it can't carry a host or URL into a publicly-pasted diagnostics
    bundle.
    """

    events_received: int = 0
    events_received_by_kind: dict[str, int] = field(default_factory=dict)
    last_error: str | None = None


class AshlyPushClient:
    """Owner of one push WebSocket per config entry.

    Construction does not open the connection; call :meth:`async_start`.
    Stopping is idempotent and safe to interleave with reload — a
    ``_stopping`` flag short-circuits the event handlers so any
    in-flight dispatch into a coordinator that is about to be torn
    down becomes a no-op rather than a partial write.
    """

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        coordinator: AshlyCoordinator,
        host: str,
        rest_port: int,
        ws_port: int,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._host = host
        self._stopping = False
        self._stats = PushStats(
            # Pre-seed with the routable names so diagnostics always shows
            # the dispatch table even at zero traffic.
            events_received_by_kind=dict.fromkeys(ROUTABLE_EVENT_NAMES, 0)
        )
        # Child logger so library messages nest under
        # custom_components.ashly.push in HA log filters, matching the
        # `loggers` declaration in manifest.json.
        self._log = logging.getLogger(__name__).getChild(host)
        self._client = AquaControlClient(
            host=host,
            rest_port=rest_port,
            ws_port=ws_port,
            username=username,
            password=password,
            session=session,
            logger=self._log,
        )

    # ── Lifecycle ───────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Wire handlers and start the reconnect loop.

        Safe to call from inside ``async_setup_entry``; returns once the
        background task has been launched. Does NOT block on the first
        WebSocket connect — :attr:`connected` flips True asynchronously.
        """
        # Heartbeat: stamp the timestamp on every event so the staleness
        # watchdog reflects "channel alive" not "state changing."
        self._client.on_any(self._handle_heartbeat)
        # State-change routing.
        for name in ROUTABLE_EVENT_NAMES:
            self._client.on_event(name, self._handle_routable_event)
        # Preset topic — every event triggers a refresh (recall storms
        # coalesce naturally via the coordinator's debouncer).
        self._client.on_topic(PRESET, self._handle_preset_event)
        await self._client.connect()

    async def async_stop(self) -> None:
        """Disconnect and stop all background work.

        Idempotent; safe to call multiple times and safe to call before
        :meth:`async_start`. The ``_stopping`` flag is set *before*
        awaiting the disconnect so any handler invocation that fires
        during the teardown window sees it and short-circuits without
        writing to the coordinator.
        """
        self._stopping = True
        await self._client.disconnect()

    # ── Diagnostics surface ─────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._client.connected

    @property
    def session_id(self) -> str | int | None:
        return self._client.session_id

    @property
    def last_event_at(self) -> float | None:
        # Backed by the coordinator so the heartbeat handler doesn't need
        # to write to two places. See `note_push_event`.
        return self._coordinator.last_push_event_at

    @property
    def subscribed_topics(self) -> tuple[str, ...]:
        return self._client.topics

    @property
    def stats(self) -> PushStats:
        return self._stats

    # ── Handlers ────────────────────────────────────────────────────

    def _handle_heartbeat(self, event: Event) -> None:
        """Stamp the staleness timestamp + increment counters.

        Runs synchronously inside the library's dispatcher (sync handler →
        not awaited). Fires on every event including ambients.
        """
        if self._stopping:
            return
        self._stats.events_received += 1
        kinds = self._stats.events_received_by_kind
        if event.name in kinds:
            kinds[event.name] += 1
        elif len(kinds) < _EVENTS_BY_KIND_CAP:
            kinds[event.name] = 1
        else:
            # Cap reached — bucket any further unfamiliar names so a
            # buggy or hostile device can't grow this dict unboundedly.
            kinds[_EVENTS_BY_KIND_OVERFLOW] = kinds.get(_EVENTS_BY_KIND_OVERFLOW, 0) + 1
        self._coordinator.note_push_event()

    async def _handle_routable_event(self, event: Event) -> None:
        """Patch coordinator state for a recognised state-change event."""
        if self._stopping:
            return
        coord = self._coordinator
        prev = coord.data
        if prev is None:
            # First poll hasn't completed yet — drop the event. The
            # imminent poll will resync from REST. Heartbeat already
            # ran (registration order: on_any first), so the event
            # still counts toward staleness.
            return
        try:
            patched = route_event(event, prev)
        except Exception as err:
            # Capture sanitized — the raw repr can embed device URLs or
            # other context that doesn't belong in a publicly-pasted
            # diagnostics bundle. The library catches its own; this is
            # the dispatch-layer safety net for a misbehaving router
            # handler.
            self._stats.last_error = type(err).__name__
            self._log.exception("Router raised for %s", event.name)
            return
        if isinstance(patched, _NoChange):
            return
        if patched is None:
            await coord.async_request_refresh()
            return
        coord.async_set_updated_data(patched)

    async def _handle_preset_event(self, event: Event) -> None:
        """Every Preset-topic event triggers a coordinator refresh.

        Preset Recall middle packets reach ~400 kB and touch dozens of
        fields; refreshing once is far cheaper than patching each. The
        coordinator's request-refresh debouncer (cooldown 0.3 s)
        coalesces the begin/middle/end trio into one poll.
        """
        if self._stopping:
            return
        if self._coordinator.data is None:
            return
        await self._coordinator.async_request_refresh()


__all__ = ["AshlyPushClient", "PushStats"]
