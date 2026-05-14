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

    Indicates a likely firmware change or a network MITM. Consumers may
    treat this as a transient connection-class error or a permanent setup
    failure depending on context.
    """
