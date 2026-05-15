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
    #:
    #: **Security note:** ``raw`` is untrusted data from a network peer
    #: and can be arbitrarily large (the ``Preset Recall`` middle packet
    #: routinely reaches ~400 kB). Avoid logging or serialising it
    #: unconditionally — a DoS-class log flood is easy to trigger. If
    #: you need to log, truncate at a sensible byte budget yourself.
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

    @property
    def is_single_operation(self) -> bool:
        """True if this event has exactly one operation in ``data``.

        The convenience accessors :attr:`api`, :attr:`records`, and
        :attr:`type` return ``None``/empty for multi-op events to avoid
        silently picking the first operation. Use this property to gate
        access to those accessors::

            if event.is_single_operation and event.api == "/some/path":
                handle(event.records[0])
            else:
                for op in event.operations:
                    handle_op(op)
        """
        return len(self.operations) == 1

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


def parse_event(topic: str, payload: Any, *, strict: bool = False) -> Event:
    """Parse a raw push payload into an :class:`Event`.

    By default, tolerates malformed shapes by coercing missing fields to
    safe defaults rather than raising — the device's protocol is loosely
    typed and a strict parser would brittleify the integration to firmware
    quirks.

    Pass ``strict=True`` to make the parser raise
    :class:`~aquacontrol.exceptions.AquaControlProtocolError` on:

    - a non-mapping outer payload,
    - a non-sequence ``data`` field,
    - or a non-mapping entry inside ``data``.

    Strict mode is useful in development for catching firmware schema
    changes early; in production the default tolerant mode keeps the
    integration alive across minor wire-format drifts.
    """
    from .exceptions import AquaControlProtocolError  # local: avoid cycle

    if not isinstance(payload, Mapping):
        if strict:
            raise AquaControlProtocolError(
                f"Event payload is not a mapping (topic={topic!r}, "
                f"got {type(payload).__name__})"
            )
        return Event(
            topic=topic,
            name="",
            operations=(),
            unique_id=None,
            raw={"_unparseable": payload},
        )
    name = str(payload.get("name", ""))
    data = payload.get("data", [])
    # str/bytes are technically Sequences but are obviously not the
    # list-of-operations shape we want — treat them as protocol drift.
    if strict and (not isinstance(data, Sequence) or isinstance(data, (str, bytes))):
        raise AquaControlProtocolError(
            f"Event 'data' is not a sequence (topic={topic!r}, name={name!r}, "
            f"got {type(data).__name__})"
        )
    ops: list[Operation] = []
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        for op in data:
            if not isinstance(op, Mapping):
                if strict:
                    raise AquaControlProtocolError(
                        f"Operation entry is not a mapping (topic={topic!r}, "
                        f"name={name!r}, got {type(op).__name__})"
                    )
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
