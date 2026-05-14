"""Event model + parser for the AquaControl push API.

Every state event on the wire wears the same envelope::

    42["<TopicName>", {
        "name": "<inner-event-name>",
        "data": [
            {"api": "<rest-path-or-update>", "records": [...], "type": "..."},
            ...
        ],
        "uniqueId": "<originating-session-id>" | 0 | null
    }]

Most events have exactly one operation in ``data``; preset recall + DSP
block lifecycle + ``Events / Update *`` events emit multi-op packets where
each entry describes a different affected area.

The :class:`Event` returned to consumers wraps the raw payload and exposes
``operations`` for the multi-op cases plus convenience accessors for the
common single-op case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import topics as _topics


@dataclass(frozen=True, slots=True)
class Operation:
    """One operation entry from an event's ``data`` array."""

    #: REST path the mutation maps to (e.g. ``/workingsettings/dsp/chain``),
    #: or the literal ``"update"`` for ambient broadcasts. Note that the
    #: device emits inconsistent casing on this field — see the
    #: ``/workingSettings/virtualDVCA/...`` vs ``/workingsettings/...``
    #: gotcha in docs/WEBSOCKET-API.md §7.
    api: str

    #: Changed field(s), full new record, or nested ops (preset recall).
    records: list[Any]

    #: One of ``"modify"``, ``"new"``, ``"delete"``, ``"update"``.
    type: str


@dataclass(frozen=True, slots=True)
class Event:
    """A parsed push event from the device.

    Both the topic (Socket.IO outer event name) and inner name are exposed;
    consumers usually dispatch on ``name``, and use ``topic`` to disambiguate
    only when the same name appears under multiple topics (rare — only
    ``Modify system info`` does this in observed payloads, distinguished by
    the ``api`` field on its single operation).

    For single-operation events, :attr:`api`, :attr:`records`, and
    :attr:`type` proxy to ``operations[0]``. For multi-op events those
    properties return ``None``/empty; iterate :attr:`operations` instead.
    """

    #: The Socket.IO outer event name — the topic the event arrived on.
    topic: str

    #: The inner event name (``data["name"]`` on the wire).
    name: str

    #: All operations in the event's ``data`` array, in order.
    operations: tuple[Operation, ...]

    #: The originating client's session ID. ``str`` UUIDs identify which
    #: connected client triggered the change; ``0`` indicates a
    #: system-emitted event (no originating session); ``None`` indicates
    #: a public broadcast (heartbeats, log entries).
    unique_id: str | int | None

    #: The raw payload as delivered. Provided for advanced callers (e.g.
    #: ones that need fields not modelled by :class:`Operation`).
    raw: Mapping[str, Any]

    # ── Convenience accessors for single-op events ─────────────────

    @property
    def api(self) -> str | None:
        """REST path of the single operation, or ``None`` for multi-op events."""
        return self.operations[0].api if len(self.operations) == 1 else None

    @property
    def records(self) -> list[Any]:
        """Records of the single operation, or empty list for multi-op events."""
        return self.operations[0].records if len(self.operations) == 1 else []

    @property
    def type(self) -> str | None:
        """``type`` of the single operation, or ``None`` for multi-op events."""
        return self.operations[0].type if len(self.operations) == 1 else None

    # ── Classification helpers ─────────────────────────────────────

    @property
    def is_ambient(self) -> bool:
        """True if this is a heartbeat/timer-driven event (System Info,
        DateTime, network link-state). Excludes :attr:`is_meter`."""
        return _topics.is_ambient(self.topic, self.name)

    @property
    def is_meter(self) -> bool:
        """True if this is a Channel Meters frame."""
        return _topics.is_meter(self.topic, self.name)

    @property
    def is_state_change(self) -> bool:
        """True if this is a user/system-driven mutation event.

        Excludes ambient heartbeats and meter frames. Returns True for
        ``Modify *``, ``Set *``, ``Change *``, ``New *``, ``Delete *``,
        preset-recall events, security events, etc.
        """
        return not (self.is_ambient or self.is_meter)

    def is_from_session(self, session_id: str | int | None) -> bool:
        """Return True if this event was emitted in response to a mutation
        made by ``session_id``.

        Useful for filtering own-echoes when applying optimistic state
        updates locally — without this check, a client that triggered the
        change would receive its own change back over the socket and
        re-apply it. See docs/WEBSOCKET-API.md §2.2 for the protocol
        rationale.

        Returns False when ``session_id`` is None — caller hasn't yet
        learned its own session ID.
        """
        if session_id is None:
            return False
        return self.unique_id == session_id


def parse_event(topic: str, payload: Any) -> Event:
    """Parse a raw push payload into an :class:`Event`.

    Tolerates malformed shapes by coercing missing fields to safe defaults
    rather than raising — the device's protocol is loosely typed and a
    strict parser would brittleify the integration to firmware quirks.
    """
    if not isinstance(payload, Mapping):
        return Event(
            topic=topic,
            name="",
            operations=(),
            unique_id=None,
            raw={"_unparseable": payload},
        )
    name = str(payload.get("name", ""))
    data = payload.get("data", [])
    ops: list[Operation] = []
    if isinstance(data, Sequence):
        for op in data:
            if not isinstance(op, Mapping):
                continue
            records = op.get("records", [])
            if not isinstance(records, list):
                records = [records]
            ops.append(
                Operation(
                    api=str(op.get("api", "")),
                    records=records,
                    type=str(op.get("type", "")),
                )
            )
    return Event(
        topic=topic,
        name=name,
        operations=tuple(ops),
        unique_id=payload.get("uniqueId"),
        raw=payload,
    )
