"""Exception hierarchy for the aquacontrol library."""

from __future__ import annotations


class AquaControlError(Exception):
    """Base class for any aquacontrol error."""


class AquaControlConnectionError(AquaControlError):
    """Could not connect to the device — DNS / TCP / WebSocket layer."""


class AquaControlAuthError(AquaControlError):
    """Authentication failed against /v1.0-beta/session/login."""


class AquaControlTimeoutError(AquaControlConnectionError):
    """A request to the device timed out.

    Inherits ``AquaControlConnectionError`` so existing connection-error
    handlers continue to cover it without changes.
    """


class AquaControlProtocolError(AquaControlError):
    """The device returned an unexpected payload shape on the wire.

    Indicates a likely firmware change or a network MITM. The
    :attr:`transient` attribute is a hint to consumers about whether
    retrying is likely to help:

    - ``transient=True`` (default): treat like a connection error —
      back off and retry. Use this when the protocol error is plausibly
      caused by a flaky network, a device reboot mid-response, or a
      single corrupted payload.
    - ``transient=False``: treat as a permanent setup failure — surface
      to the operator and stop retrying. Use this when the protocol
      mismatch is structural (e.g. the device's firmware version is
      incompatible with what the library knows how to parse).

    Home Assistant integrations should map the two cases to
    ``ConfigEntryNotReady`` and ``ConfigEntryError`` respectively.
    """

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient
