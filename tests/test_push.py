"""Tests for the AshlyPushClient adapter.

These tests stub the underlying :class:`aquacontrol.AquaControlClient`
so they can exercise the adapter's dispatch logic without opening a
real WebSocket. The router itself is covered by ``test_event_router.py``.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ashly._aquacontrol import parse_event
from custom_components.ashly._aquacontrol._testing import SAMPLE_EVENTS
from custom_components.ashly.event_router import ROUTABLE_EVENT_NAMES
from custom_components.ashly.push import AshlyPushClient, PushStats


@pytest.fixture
def fake_aquacontrol_client():
    """A drop-in replacement for AquaControlClient.

    Records the listener-registration calls so tests can assert that
    AshlyPushClient wires the right handlers, and lets tests retrieve
    a registered handler to fire synthetic events through it.
    """
    client = MagicMock()
    client.connected = False
    client.session_id = None
    client.topics = ("System", "WorkingSettings", "Preset")
    client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)

    # Capture handler registrations.
    client._registered_event: dict[str, list] = {}
    client._registered_topic: dict[str, list] = {}
    client._registered_any: list = []

    def _on_event(name, handler):
        client._registered_event.setdefault(name, []).append(handler)
        return lambda: None

    def _on_topic(topic, handler):
        client._registered_topic.setdefault(topic, []).append(handler)
        return lambda: None

    def _on_any(handler):
        client._registered_any.append(handler)
        return lambda: None

    client.on_event = MagicMock(side_effect=_on_event)
    client.on_topic = MagicMock(side_effect=_on_topic)
    client.on_any = MagicMock(side_effect=_on_any)
    return client


@pytest.fixture
def push_client(hass, mock_coordinator, fake_aquacontrol_client):
    """An AshlyPushClient with its inner AquaControlClient mocked out."""
    with patch(
        "custom_components.ashly.push.AquaControlClient",
        return_value=fake_aquacontrol_client,
    ):
        pc = AshlyPushClient(
            hass=hass,
            coordinator=mock_coordinator,
            host="192.168.1.100",
            rest_port=8000,
            ws_port=8001,
            username="haassistant",
            password="x" * 16,
            session=MagicMock(),
        )
    return pc


# ── Lifecycle ───────────────────────────────────────────────────────


async def test_async_start_registers_handlers_and_connects(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    await push_client.async_start()
    fake_aquacontrol_client.connect.assert_awaited_once()
    # on_any for the heartbeat
    assert len(fake_aquacontrol_client._registered_any) == 1
    # One on_event per routable name
    assert set(fake_aquacontrol_client._registered_event) == ROUTABLE_EVENT_NAMES
    # on_topic for Preset
    assert "Preset" in fake_aquacontrol_client._registered_topic


async def test_async_stop_disconnects_and_sets_flag(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    await push_client.async_start()
    await push_client.async_stop()
    fake_aquacontrol_client.disconnect.assert_awaited_once()
    assert push_client._stopping is True


async def test_async_stop_before_start_is_noop(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    await push_client.async_stop()
    # disconnect is still called (it's idempotent on the library side).
    fake_aquacontrol_client.disconnect.assert_awaited_once()
    assert push_client._stopping is True


async def test_async_stop_is_idempotent(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    await push_client.async_start()
    await push_client.async_stop()
    await push_client.async_stop()
    assert fake_aquacontrol_client.disconnect.await_count == 2


# ── Heartbeat ───────────────────────────────────────────────────────


async def test_heartbeat_stamps_coordinator_and_increments_stats(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator
) -> None:
    await push_client.async_start()
    heartbeat = fake_aquacontrol_client._registered_any[0]
    topic, payload = SAMPLE_EVENTS["SYSTEM_INFO_VALUES_HEARTBEAT"]
    event = parse_event(topic, payload)
    heartbeat(event)
    mock_coordinator.note_push_event.assert_called_once()
    assert push_client.stats.events_received == 1
    assert push_client.stats.events_received_by_kind["System Info Values"] == 1


async def test_heartbeat_increments_existing_kind(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    """Second event with same name increments the existing counter
    (hits the `name in kinds` branch, not the elif/else)."""
    await push_client.async_start()
    heartbeat = fake_aquacontrol_client._registered_any[0]
    topic, payload = SAMPLE_EVENTS["SYSTEM_INFO_VALUES_HEARTBEAT"]
    event = parse_event(topic, payload)
    heartbeat(event)
    heartbeat(event)
    assert push_client.stats.events_received_by_kind["System Info Values"] == 2


async def test_heartbeat_overflow_bucket_caps_unfamiliar_names(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    """Once the per-kind dict reaches its cap, further unfamiliar names
    roll into the overflow bucket — defence against a buggy device
    emitting unbounded distinct event names."""
    from custom_components.ashly.push import _EVENTS_BY_KIND_CAP

    await push_client.async_start()
    heartbeat = fake_aquacontrol_client._registered_any[0]
    # Fill the dict up to the cap with synthetic distinct names.
    kinds = push_client.stats.events_received_by_kind
    while len(kinds) < _EVENTS_BY_KIND_CAP:
        kinds[f"_synthetic_{len(kinds)}"] = 0
    # Now an event with a brand-new name should overflow into the bucket,
    # not grow the dict.
    topic, payload = SAMPLE_EVENTS["SYSTEM_DATETIME_HEARTBEAT"]
    event = parse_event(topic, payload)
    heartbeat(event)
    heartbeat(event)
    assert len(kinds) == _EVENTS_BY_KIND_CAP + 1  # +1 for the overflow bucket
    assert kinds["_other"] == 2
    assert "DateTime" not in kinds


async def test_last_event_at_property_reads_coordinator(
    push_client: AshlyPushClient, mock_coordinator
) -> None:
    """The push client's `last_event_at` is a thin pass-through to the
    coordinator's public `last_push_event_at` property."""
    mock_coordinator.last_push_event_at = 1234.5
    assert push_client.last_event_at == 1234.5


async def test_heartbeat_short_circuits_when_stopping(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator
) -> None:
    await push_client.async_start()
    heartbeat = fake_aquacontrol_client._registered_any[0]
    push_client._stopping = True
    topic, payload = SAMPLE_EVENTS["SYSTEM_INFO_VALUES_HEARTBEAT"]
    event = parse_event(topic, payload)
    heartbeat(event)
    mock_coordinator.note_push_event.assert_not_called()
    assert push_client.stats.events_received == 0


# ── Routable-event dispatch ────────────────────────────────────────


async def test_routable_event_calls_async_set_updated_data(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_set_updated_data.assert_called_once()
    # The new data should reflect the patched mute.
    args, _ = mock_coordinator.async_set_updated_data.call_args
    assert args[0].chains["InputChannel.1"].muted is True


async def test_routable_event_with_no_change_does_not_fan_out(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    # Pre-state already has muted=True so the router returns NO_CHANGE.
    prev = replace(
        mock_device_data,
        chains={
            **mock_device_data.chains,
            "InputChannel.1": replace(
                mock_device_data.chains["InputChannel.1"], muted=True
            ),
        },
    )
    await push_client.async_start()
    mock_coordinator.data = prev
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_set_updated_data.assert_not_called()
    mock_coordinator.async_request_refresh.assert_not_awaited()


async def test_routable_event_with_unknown_channel_triggers_refresh(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    """An event the router can't apply (unknown id) routes to refresh."""
    import copy

    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    p = copy.deepcopy(payload)
    p["data"][0]["records"][0]["id"] = "NonExistent.999"
    event = parse_event(topic, p)
    await handler(event)
    mock_coordinator.async_request_refresh.assert_awaited_once()
    mock_coordinator.async_set_updated_data.assert_not_called()


async def test_routable_event_dropped_before_first_poll(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator
) -> None:
    """If coordinator.data is None (first poll not yet completed), drop."""
    mock_coordinator.data = None
    await push_client.async_start()
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_set_updated_data.assert_not_called()
    mock_coordinator.async_request_refresh.assert_not_awaited()


async def test_routable_event_short_circuits_when_stopping(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    """After async_stop, the dispatcher must not write to the coordinator."""
    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    push_client._stopping = True
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_set_updated_data.assert_not_called()


# ── Preset topic — always refresh ───────────────────────────────────


async def test_preset_event_triggers_refresh(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    handler = fake_aquacontrol_client._registered_topic["Preset"][0]
    topic, payload = SAMPLE_EVENTS["PRESET_CREATE_PRESET"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_request_refresh.assert_awaited_once()


async def test_preset_event_short_circuits_when_stopping(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    push_client._stopping = True
    handler = fake_aquacontrol_client._registered_topic["Preset"][0]
    topic, payload = SAMPLE_EVENTS["PRESET_CREATE_PRESET"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_request_refresh.assert_not_awaited()


async def test_preset_event_before_first_poll_is_dropped(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator
) -> None:
    mock_coordinator.data = None
    await push_client.async_start()
    handler = fake_aquacontrol_client._registered_topic["Preset"][0]
    topic, payload = SAMPLE_EVENTS["PRESET_CREATE_PRESET"]
    event = parse_event(topic, payload)
    await handler(event)
    mock_coordinator.async_request_refresh.assert_not_awaited()


# ── Diagnostics surface ────────────────────────────────────────────


async def test_diagnostics_surface_properties(
    push_client: AshlyPushClient, fake_aquacontrol_client
) -> None:
    fake_aquacontrol_client.connected = True
    fake_aquacontrol_client.session_id = "abc-123"
    fake_aquacontrol_client.topics = ("System", "WorkingSettings")
    assert push_client.connected is True
    assert push_client.session_id == "abc-123"
    assert push_client.subscribed_topics == ("System", "WorkingSettings")
    assert isinstance(push_client.stats, PushStats)


# ── Mode safety: router raising ────────────────────────────────────


async def test_router_exception_recorded_in_last_error(
    push_client: AshlyPushClient, fake_aquacontrol_client, mock_coordinator, mock_device_data
) -> None:
    """If the router raises, the exception type is recorded into
    stats.last_error (sanitized — no host or URL embedded) and the
    stream stays alive (no propagation)."""
    await push_client.async_start()
    mock_coordinator.data = mock_device_data
    handler = fake_aquacontrol_client._registered_event["Set Chain Mute"][0]
    topic, payload = SAMPLE_EVENTS["WORKING_SETTINGS_SET_CHAIN_MUTE"]
    event = parse_event(topic, payload)
    with patch(
        "custom_components.ashly.push.route_event",
        side_effect=RuntimeError("synthetic"),
    ):
        await handler(event)
    assert push_client.stats.last_error == "RuntimeError"
    mock_coordinator.async_set_updated_data.assert_not_called()
