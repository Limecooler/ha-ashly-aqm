"""Socket.IO topic and event-name constants for the AquaControl push API.

Reverse-engineered against an AQM1208 running firmware 1.1.8 — see
docs/WEBSOCKET-API.md in the ha-ashly-aqm repo for the full reference and
the methodology.

Topics are strings (case-sensitive). The device silently accepts joins for
unknown topic names and never emits to them, so the constants below are
deliberately exhaustive: subscribing to all of them is the only way to
receive the full state-change firehose.
"""

from __future__ import annotations

from typing import Final

# ── Topics ─────────────────────────────────────────────────────────────

#: Audio meter stream (~5 Hz). Public — emits to unauthenticated clients too.
CHANNEL_METERS: Final = "Channel Meters"

#: System-wide state: front panel, identify, log, time, sync.
SYSTEM: Final = "System"

#: DSP working settings: chain mute, mixer parameters, DVCA, GPO, templates,
#: channel rename, DSP blocks.
WORKING_SETTINGS: Final = "WorkingSettings"

#: Preset CRUD + the 3-phase recall protocol + "dirty" signal.
PRESET: Final = "Preset"

#: Event scheduler CRUD + scheduler-state signals (block/enable).
EVENTS: Final = "Events"

#: Mic preamp gain changes. Not in Portal's join list — exclusive to this topic.
MIC_PREAMP: Final = "MicPreamp"

#: Phantom power changes. Not in Portal's join list — exclusive to this topic.
PHANTOM_POWER: Final = "PhantomPower"

#: Periodic network link-state heartbeat (~7 s). Public.
NETWORK: Final = "Network"

#: Firmware update progress. Not exercised in reverse engineering; expected
#: to carry chunk-upload + post-flash status when a real update is in flight.
FIRMWARE: Final = "Firmware"

#: User / role / permission CRUD events.
SECURITY: Final = "Security"


#: All known topics. Subscribing to every entry is the default behaviour
#: of :class:`aquacontrol.AquaControlClient` so consumers receive every
#: emitted event without having to enumerate.
ALL_TOPICS: Final = (
    CHANNEL_METERS,
    SYSTEM,
    WORKING_SETTINGS,
    PRESET,
    EVENTS,
    MIC_PREAMP,
    PHANTOM_POWER,
    NETWORK,
    FIRMWARE,
    SECURITY,
)


# ── Ambient ("noise") event identifiers ────────────────────────────────
#
# These events fire on a timer regardless of any state change and
# typically dominate the event stream by count. Consumers usually want
# to filter them out — :attr:`aquacontrol.Event.is_ambient` returns True
# when the event matches one of these pairs.

_AMBIENT_PAIRS: Final = frozenset(
    {
        (SYSTEM, "System Info Values"),     # 1 Hz CPU/RAM heartbeat
        (SYSTEM, "DateTime"),                # ~10 s device wall-clock
        (NETWORK, "detected updated network parameters"),  # ~7 s link state
    }
)


# ── Meter event identifier ─────────────────────────────────────────────

_METER_PAIRS: Final = frozenset({(CHANNEL_METERS, "Channel Meters")})


def is_ambient(topic: str, name: str) -> bool:
    """Return True if (topic, name) is a periodic heartbeat-style emission.

    Excludes :data:`CHANNEL_METERS` — meters are high-frequency but they
    carry signal data consumers may actually want. Use :func:`is_meter`
    to identify those separately.
    """
    return (topic, name) in _AMBIENT_PAIRS


def is_meter(topic: str, name: str) -> bool:
    """Return True if (topic, name) is a Channel Meters frame."""
    return (topic, name) in _METER_PAIRS
