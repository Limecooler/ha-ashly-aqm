"""Tests for the device_action module."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.ashly.const import DOMAIN


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_config_entry, mock_client, patched_session):
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


async def test_async_get_actions_returns_recall_preset(hass: HomeAssistant, loaded_entry):
    """An Ashly device exposes a recall_preset action."""
    from custom_components.ashly.device_action import async_get_actions

    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    actions = await async_get_actions(hass, device.id)
    assert len(actions) == 1
    assert actions[0] == {
        CONF_DOMAIN: DOMAIN,
        CONF_DEVICE_ID: device.id,
        CONF_TYPE: "recall_preset",
    }


async def test_async_get_action_capabilities_returns_preset_field(hass: HomeAssistant):
    """The capabilities describe a single 'preset' extra field."""
    from custom_components.ashly.device_action import async_get_action_capabilities

    caps = await async_get_action_capabilities(hass, {})
    assert "extra_fields" in caps


async def test_async_call_action_invokes_service(hass: HomeAssistant, loaded_entry, mock_client):
    """The action handler translates to a recall_preset service call."""
    from custom_components.ashly.device_action import async_call_action_from_config

    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    await async_call_action_from_config(
        hass,
        {
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device.id,
            CONF_TYPE: "recall_preset",
            "preset": "Preset 1",
        },
        variables=None,
        context=None,
    )
    mock_client.async_recall_preset.assert_awaited_once_with("Preset 1")
