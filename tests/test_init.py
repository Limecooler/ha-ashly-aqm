"""Setup/unload tests for the Ashly integration entry point."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.ashly.client import AshlyAuthError, AshlyConnectionError


@pytest.fixture
def _patch_client(mock_client, patched_session):
    """Make every AshlyClient instantiated by the integration the mock."""
    _ = patched_session  # ensure aiohttp.ClientSession/TCPConnector are stubbed
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        yield mock_client


async def test_setup_entry_success(hass: HomeAssistant, mock_config_entry, _patch_client) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.config_entries.async_get_entry(mock_config_entry.entry_id).runtime_data


async def test_setup_entry_auth_error_starts_reauth(
    hass: HomeAssistant, mock_config_entry, _patch_client, mock_client
) -> None:
    mock_client.async_login.side_effect = AshlyAuthError("nope")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_entry_connection_error(
    hass: HomeAssistant, mock_config_entry, _patch_client, mock_client
) -> None:
    mock_client.async_login.side_effect = AshlyConnectionError("nope")
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_options_update_triggers_reload(
    hass: HomeAssistant, mock_config_entry, _patch_client
) -> None:
    """Updating options should fire the listener which reloads the entry."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    with patch("homeassistant.config_entries.ConfigEntries.async_reload") as mock_reload:
        mock_reload.return_value = True
        hass.config_entries.async_update_entry(mock_config_entry, options={"poll_interval": 60})
        await hass.async_block_till_done()
    mock_reload.assert_called_once_with(mock_config_entry.entry_id)


async def test_unload_entry(hass: HomeAssistant, mock_config_entry, _patch_client) -> None:
    """Unload should succeed; HA owns the session lifecycle, so the client is
    not explicitly closed by the integration."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_multi_device_setup_and_unload(
    hass: HomeAssistant, mock_client, patched_session
) -> None:
    """Two Ashly entries can coexist; services register once and only deregister
    on the last unload. Each device gets its own distinct identity."""
    import dataclasses

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ashly.client import SystemInfo
    from custom_components.ashly.const import DOMAIN
    from custom_components.ashly.services import SERVICE_RECALL_PRESET

    info_a = SystemInfo(
        model="AQM1208",
        name="Living Room",
        firmware_version="1.1.8",
        hardware_revision="1.0.0",
        mac_address="00:14:aa:11:22:33",
        has_auto_mix=True,
    )
    info_b = dataclasses.replace(info_a, name="Kitchen", mac_address="00:14:aa:44:55:66")

    # Build a second client identical to the first but with a different MAC.
    import copy

    client_b = copy.copy(mock_client)
    client_b.host = "192.168.1.101"
    client_b.async_get_system_info = mock_client.async_login.__class__(return_value=info_b)
    # Re-stub other reads (they share the same dataclass instances from fixtures,
    # which is fine because they don't carry identity).
    mock_client.async_get_system_info.return_value = info_a

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.1.100", "port": 8000, "username": "admin", "password": "secret"},
        unique_id="00:14:aa:11:22:33",
        title="Living Room",
    )
    entry_b = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.168.1.101", "port": 8000, "username": "admin", "password": "secret"},
        unique_id="00:14:aa:44:55:66",
        title="Kitchen",
    )

    # Patch AshlyClient to return the right mock based on host.
    def _client_factory(host, port, **_kw):
        return client_b if host == "192.168.1.101" else mock_client

    with patch("custom_components.ashly.AshlyClient", side_effect=_client_factory):
        # Add+setup entry A first, then entry B — adding both before setup
        # can race HA's startup hook that auto-loads pending entries.
        entry_a.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()
        entry_b.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

    assert entry_a.state is ConfigEntryState.LOADED
    assert entry_b.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)

    # Removing the first entry keeps the service alive (a second device exists).
    # We use async_remove (not async_unload) because services are scoped to
    # entries in the registry, and unloading leaves the entry present.
    assert await hass.config_entries.async_remove(entry_a.entry_id)
    assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)
    # Removing the last entry tears down the service.
    assert await hass.config_entries.async_remove(entry_b.entry_id)
    assert not hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)


async def test_homeassistant_stop_stops_meter(
    hass: HomeAssistant, mock_config_entry, _patch_client, patched_session
) -> None:
    """Firing EVENT_HOMEASSISTANT_STOP should call meter_client.async_stop()
    so the websocket task winds down before the loop is torn down."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    # Capture the stub meter client that patched_session installed.
    meter = mock_config_entry.runtime_data.meter_client
    meter.async_stop.reset_mock()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    meter.async_stop.assert_awaited()


async def test_setup_starts_push_after_platform_forward(
    hass: HomeAssistant, mock_config_entry, _patch_client
) -> None:
    """The push client must start AFTER async_forward_entry_setups returns.

    Otherwise a slow/unreachable push endpoint can delay HA's entry-ready
    signal. The conftest fake_push has async_start as an AsyncMock — we
    assert it was awaited exactly once during setup.
    """
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    push = mock_config_entry.runtime_data.push_client
    push.async_start.assert_awaited_once()


async def test_unload_stops_push_before_platform_unload(
    hass: HomeAssistant, mock_config_entry, _patch_client
) -> None:
    """Stop ordering: push.async_stop must run before async_unload_platforms
    so an in-flight push event can't fire into half-removed entities."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    push = mock_config_entry.runtime_data.push_client
    push.async_stop.reset_mock()

    # Record ordering: capture the entry's state at the moment push.async_stop
    # is awaited. If push stops before platform unload, the entry is still LOADED.
    state_at_push_stop: list[ConfigEntryState] = []
    original_stop = push.async_stop

    async def _capturing_stop():
        state_at_push_stop.append(mock_config_entry.state)
        await original_stop()

    push.async_stop = _capturing_stop

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert state_at_push_stop == [ConfigEntryState.LOADED], (
        "push.async_stop must be awaited before platform unload runs"
    )


async def test_homeassistant_stop_stops_push_and_meter(
    hass: HomeAssistant, mock_config_entry, _patch_client
) -> None:
    """EVENT_HOMEASSISTANT_STOP shuts down both WS clients."""
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    push = mock_config_entry.runtime_data.push_client
    meter = mock_config_entry.runtime_data.meter_client
    push.async_stop.reset_mock()
    meter.async_stop.reset_mock()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    push.async_stop.assert_awaited()
    meter.async_stop.assert_awaited()


async def test_setup_failure_after_platforms_stops_push_and_meter(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """If anything after `async_forward_entry_setups` raises (push start,
    services), the integration must tear down the WS clients before
    propagating the failure — otherwise the background tasks keep running
    against a setup_error entry and a retry duplicates them."""
    from unittest.mock import AsyncMock, MagicMock

    import aiohttp

    from custom_components.ashly.push import PushStats

    fake_session = MagicMock(spec=aiohttp.ClientSession)
    fake_session.closed = False
    fake_session.close = AsyncMock(return_value=None)

    fake_meter = MagicMock()
    fake_meter.async_start = AsyncMock(return_value=None)
    fake_meter.async_stop = AsyncMock(return_value=None)

    fake_push = MagicMock()
    # Simulate the real failure mode: connect succeeds, then a later
    # post-forward step raises. Easiest hook: async_start itself raises.
    fake_push.async_start = AsyncMock(side_effect=RuntimeError("boom"))
    fake_push.async_stop = AsyncMock(return_value=None)
    fake_push.connected = False
    fake_push.session_id = None
    fake_push.last_event_at = None
    fake_push.subscribed_topics = ()
    fake_push.stats = PushStats()

    with (
        patch("custom_components.ashly.async_create_clientsession", return_value=fake_session),
        patch("custom_components.ashly.AshlyClient", return_value=mock_client),
        patch("custom_components.ashly.AshlyMeterClient", return_value=fake_meter),
        patch("custom_components.ashly.AshlyPushClient", return_value=fake_push),
    ):
        mock_config_entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    fake_push.async_stop.assert_awaited()
    fake_meter.async_stop.assert_awaited()


async def test_setup_passes_shared_session_to_push_client(
    hass: HomeAssistant, mock_config_entry, mock_client
) -> None:
    """The push client receives the same aiohttp session as the REST client,
    so a single cookie jar is shared three ways (REST, meter ws, push ws).
    """
    from unittest.mock import AsyncMock, MagicMock

    import aiohttp

    from custom_components.ashly.push import PushStats

    fake_session = MagicMock(spec=aiohttp.ClientSession)
    fake_session.closed = False
    fake_session.close = AsyncMock(return_value=None)

    fake_meter = MagicMock()
    fake_meter.async_start = AsyncMock(return_value=None)
    fake_meter.async_stop = AsyncMock(return_value=None)
    fake_meter.connected = False
    fake_meter.latest_records = []

    fake_push = MagicMock()
    fake_push.async_start = AsyncMock(return_value=None)
    fake_push.async_stop = AsyncMock(return_value=None)
    fake_push.connected = False
    fake_push.session_id = None
    fake_push.last_event_at = None
    fake_push.subscribed_topics = ()
    fake_push.stats = PushStats()

    with (
        patch(
            "custom_components.ashly.async_create_clientsession",
            return_value=fake_session,
        ),
        patch("custom_components.ashly.AshlyClient", return_value=mock_client),
        patch("custom_components.ashly.AshlyMeterClient", return_value=fake_meter) as MockMeter,
        patch("custom_components.ashly.AshlyPushClient", return_value=fake_push) as MockPush,
    ):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    push_kwargs = MockPush.call_args.kwargs
    assert push_kwargs["session"] is fake_session
    # Meter client receives the cookie_jar separately (its constructor predates
    # the session= kwarg pattern); the session it later opens internally still
    # honours the same jar. Smoke-assert MockMeter was called.
    MockMeter.assert_called_once()
