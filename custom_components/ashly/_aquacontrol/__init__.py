"""``aquacontrol`` — Python client for the Ashly AquaControl push API.

A standalone async library that wraps the (undocumented) Socket.IO push
channel exposed by Ashly AQM-series audio processors. Authenticates via
the REST login endpoint, opens an authenticated WebSocket, subscribes to
the device's 10 push topics, and delivers parsed events to consumer
callbacks.

Built to be consumed by the ``ha-ashly-aqm`` Home Assistant integration
but has no HA-specific dependencies — usable from any asyncio program.

See README.md alongside this package for the full reverse-engineered
protocol reference plus a usage tour. The companion documents
``docs/WEBSOCKET-API.md`` and ``docs/SECURITY-API.md`` in the
ha-ashly-aqm repo cover the wire protocol and security model in depth.
"""

from __future__ import annotations

from .auth import cookie_header, fetch_session_cookies
from .client import AquaControlClient, EventHandler, Unsubscribe
from .events import Event, Operation, parse_event
from .exceptions import (
    AquaControlAuthError,
    AquaControlConnectionError,
    AquaControlError,
    AquaControlProtocolError,
    AquaControlTimeoutError,
)
from .topics import (
    ALL_TOPICS,
    CHANNEL_METERS,
    EVENTS,
    FIRMWARE,
    MIC_PREAMP,
    NETWORK,
    PHANTOM_POWER,
    PRESET,
    SECURITY,
    SYSTEM,
    WORKING_SETTINGS,
    is_ambient,
    is_meter,
)

__version__ = "0.2.1"

# Note: ``StreamConnection`` (in :mod:`aquacontrol.stream`) is an internal
# implementation detail of :class:`AquaControlClient` and is intentionally
# not re-exported here. Callers should not depend on its shape — only the
# high-level client API is covered by the library's stability contract.
__all__ = [
    "ALL_TOPICS",
    "CHANNEL_METERS",
    "EVENTS",
    "FIRMWARE",
    "MIC_PREAMP",
    "NETWORK",
    "PHANTOM_POWER",
    "PRESET",
    "SECURITY",
    "SYSTEM",
    "WORKING_SETTINGS",
    "AquaControlAuthError",
    "AquaControlClient",
    "AquaControlConnectionError",
    "AquaControlError",
    "AquaControlProtocolError",
    "AquaControlTimeoutError",
    "Event",
    "EventHandler",
    "Operation",
    "Unsubscribe",
    "__version__",
    "cookie_header",
    "fetch_session_cookies",
    "is_ambient",
    "is_meter",
    "parse_event",
]
