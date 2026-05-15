"""Smoke-tests for the code snippets shown in README.md.

The README ships executable Python in several fenced blocks (quick-start,
echo filter, multi-op iteration, decorator). When the public API drifts
those snippets break silently — they're rendered as documentation but
never executed in CI. These tests do not connect to a device; they only
verify that each example's import + construction + listener-registration
path is syntactically valid and uses the public API correctly.

If you change a public name, add or rename a parameter, or remove an
exported symbol, run this file before publishing — a failure here means
the README is stale.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Quick-start: imports + handler signatures ──────────────────────────


def test_quickstart_imports_resolve() -> None:
    """The README's first import line still resolves on the public API."""
    from aquacontrol import AquaControlClient  # noqa: F401


def test_quickstart_handler_signatures_compile() -> None:
    """The four handler functions in the README's quick-start block
    parse and have the right shape."""
    from aquacontrol import Event

    async def on_mute(event: Event) -> None:
        record = event.records[0]
        _ = record["id"]
        _ = record["muted"]

    async def on_front_panel(event: Event) -> None:
        record = event.records[0]
        if "powerState" in record:
            _ = record["powerState"]
        if "frontPanelLEDEnable" in record:
            _ = record["frontPanelLEDEnable"]

    async def on_preset_lifecycle(event: Event) -> None:
        _ = f"{event.topic}/{event.name} uniqueId={event.unique_id}"


@pytest.fixture
def patch_io():
    """Replace IO with mocks so the example doesn't need a real device."""
    cookie_mock = AsyncMock(return_value={"ashly-sid": "fake"})
    with (
        patch("aquacontrol.client.fetch_session_cookies", new=cookie_mock),
        patch("aquacontrol.client.StreamConnection") as MockStream,
    ):
        instance = MagicMock()
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.emit = AsyncMock()
        instance.connected = False
        MockStream.return_value = instance
        yield


async def test_quickstart_register_listeners_pattern(patch_io) -> None:
    """README pattern: three on_* registrations against a client."""
    from aquacontrol import AquaControlClient

    async with AquaControlClient(
        host="192.168.1.100",
        username="haassistant",
        password="example",
    ) as client:
        client.on_event("Set Chain Mute", lambda _e: None)
        client.on_event("Modify system info", lambda _e: None)
        client.on_topic("Preset", lambda _e: None)


async def test_decorator_example(patch_io) -> None:
    """README decorator example: @client.listen + is_own_event."""
    from aquacontrol import AquaControlClient, Event

    async with AquaControlClient(host="x", username="u", password="p") as client:
        client.set_session_id("test-uuid")

        @client.listen(name="Set Chain Mute")
        async def handle_mute(event: Event) -> None:
            if client.is_own_event(event):
                return
            record = event.records[0]
            _ = (record["id"], record["muted"])

        # Registration succeeded.
        assert client.listener_count == 1


def test_multi_op_iteration_example() -> None:
    """README multi-op pattern: iterate event.operations for Preset Recall."""
    from aquacontrol import parse_event

    event = parse_event(
        "Preset",
        {
            "name": "Preset Recall",
            "data": [
                {
                    "api": "/workingsettings/dsp/mixer",
                    "records": [{"id": "Mixer.1"}],
                    "type": "modify",
                },
                {
                    "api": "/workingsettings/dsp/block",
                    "records": [],
                    "type": "delete",
                },
            ],
            "uniqueId": "s",
        },
    )
    hits: list[tuple[str, list]] = []
    for op in event.operations:
        if op.api == "/workingsettings/dsp/mixer":
            hits.append((op.api, op.records))
    assert hits == [("/workingsettings/dsp/mixer", [{"id": "Mixer.1"}])]


# ── API-table presence checks ───────────────────────────────────────────


def test_documented_topics_constants_exist() -> None:
    """Topic constants the README lists in its `from aquacontrol import …` block."""
    from aquacontrol import (
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
    )

    assert len(ALL_TOPICS) == 10
    for t in (
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
    ):
        assert t in ALL_TOPICS


def test_documented_exception_hierarchy_exists() -> None:
    """README's exception-hierarchy diagram references these names."""
    from aquacontrol import (
        AquaControlAuthError,
        AquaControlConnectionError,
        AquaControlError,
        AquaControlProtocolError,
        AquaControlTimeoutError,
    )

    assert issubclass(AquaControlConnectionError, AquaControlError)
    assert issubclass(AquaControlTimeoutError, AquaControlConnectionError)
    assert issubclass(AquaControlAuthError, AquaControlError)
    assert issubclass(AquaControlProtocolError, AquaControlError)


def test_documented_client_methods_exist() -> None:
    """README's API-table methods all resolve on AquaControlClient."""
    from aquacontrol import AquaControlClient

    for attr in (
        "connect",
        "disconnect",
        "on_event",
        "on_topic",
        "on_any",
        "listen",
        "join",
        "leave",
        "set_session_id",
        "is_own_event",
        "session_id",
        "connected",
        "host",
        "topics",
    ):
        assert hasattr(AquaControlClient, attr), f"missing on AquaControlClient: {attr}"


def test_documented_event_accessors_exist() -> None:
    """README references these Event accessors / properties."""
    from aquacontrol import parse_event

    event = parse_event("System", {"name": "X", "data": [], "uniqueId": "s"})
    # Properties named in the README's Event section
    for attr in (
        "topic",
        "name",
        "operations",
        "unique_id",
        "raw",
        "api",
        "records",
        "type",
        "is_ambient",
        "is_meter",
        "is_state_change",
        "is_single_operation",
        "is_from_session",
        "raw_truncated",
    ):
        assert hasattr(event, attr), f"Event missing documented accessor: {attr}"
