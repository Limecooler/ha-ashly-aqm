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
    on the last unload."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.ashly.const import DOMAIN
    from custom_components.ashly.services import SERVICE_RECALL_PRESET

    entry_a = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.100",
            "port": 8000,
            "username": "admin",
            "password": "secret",
        },
        unique_id="00:14:aa:11:22:33",
        title="Living Room",
    )
    entry_b = MockConfigEntry(
        domain=DOMAIN,
        data={
            "host": "192.168.1.101",
            "port": 8000,
            "username": "admin",
            "password": "secret",
        },
        unique_id="00:14:aa:44:55:66",
        title="Kitchen",
    )
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

    # Both entries loaded; service registered once.
    assert entry_a.state is ConfigEntryState.LOADED
    assert entry_b.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)

    # Unload one — service stays.
    assert await hass.config_entries.async_unload(entry_a.entry_id)
    assert hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET)

    # Unload the second — service goes away.
    assert await hass.config_entries.async_unload(entry_b.entry_id)
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
