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
