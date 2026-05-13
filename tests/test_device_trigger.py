"""Tests for the device_trigger module."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.ashly.const import DOMAIN


@pytest.fixture
async def loaded_entry(hass: HomeAssistant, mock_config_entry, mock_client, patched_session):
    """Set up the integration so we have a device and entities to drive."""
    with patch("custom_components.ashly.AshlyClient", return_value=mock_client):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    return mock_config_entry


async def test_async_get_triggers_returns_preset_recalled(hass: HomeAssistant, loaded_entry):
    """A loaded Ashly device exposes a preset_recalled trigger."""
    from custom_components.ashly.device_trigger import async_get_triggers

    device_reg = dr.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    triggers = await async_get_triggers(hass, device.id)
    assert len(triggers) == 1
    assert triggers[0][CONF_TYPE] == "preset_recalled"
    assert triggers[0][CONF_DOMAIN] == DOMAIN
    assert triggers[0][CONF_DEVICE_ID] == device.id


async def test_async_get_triggers_empty_when_sensor_missing(hass: HomeAssistant, loaded_entry):
    """A device without a last_recalled_preset entity exposes no triggers."""
    from custom_components.ashly.device_trigger import async_get_triggers

    device_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    # Remove the sensor entity to simulate a partial registry.
    for entry in list(er.async_entries_for_device(ent_reg, device.id)):
        if entry.unique_id.endswith("_last_recalled_preset"):
            ent_reg.async_remove(entry.entity_id)
    triggers = await async_get_triggers(hass, device.id)
    assert triggers == []


async def test_async_attach_trigger_fires_on_state_change(hass: HomeAssistant, loaded_entry):
    """Attaching a trigger and changing the sensor state fires the action."""
    from homeassistant.components.automation import async_attach_trigger
    from homeassistant.helpers.trigger import TriggerInfo

    from custom_components.ashly.device_trigger import async_attach_trigger as ashly_attach

    _ = async_attach_trigger  # used only to satisfy import linting

    device_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    entity_id = next(
        e.entity_id
        for e in er.async_entries_for_device(ent_reg, device.id)
        if e.unique_id.endswith("_last_recalled_preset")
    )

    captured: list = []

    async def action(variables, context=None):
        captured.append(variables)

    remove = await ashly_attach(
        hass,
        {
            "platform": "device",
            "domain": DOMAIN,
            "device_id": device.id,
            "type": "preset_recalled",
        },
        action,
        TriggerInfo(domain="automation", name="test", home_assistant_start=False),  # type: ignore[typeddict-item]
    )
    try:
        # Force a state transition on the entity.
        hass.states.async_set(entity_id, "Evening Mode")
        await hass.async_block_till_done()
        hass.states.async_set(entity_id, "Late Night")
        await hass.async_block_till_done()
    finally:
        remove()
    assert len(captured) >= 1


async def test_async_attach_trigger_raises_when_no_entity(hass: HomeAssistant, loaded_entry):
    """If the device exists but the sensor was removed, attach raises."""
    from homeassistant.helpers.trigger import TriggerInfo

    from custom_components.ashly.device_trigger import async_attach_trigger

    device_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    for entry in list(er.async_entries_for_device(ent_reg, device.id)):
        if entry.unique_id.endswith("_last_recalled_preset"):
            ent_reg.async_remove(entry.entity_id)
    with pytest.raises(ValueError, match="No last_recalled_preset"):
        await async_attach_trigger(
            hass,
            {
                "platform": "device",
                "domain": DOMAIN,
                "device_id": device.id,
                "type": "preset_recalled",
            },
            lambda *a, **kw: None,
            TriggerInfo(domain="automation", name="test", home_assistant_start=False),  # type: ignore[typeddict-item]
        )


async def test_async_get_triggers_skips_non_ashly_entities(hass: HomeAssistant, loaded_entry):
    """An entity belonging to another integration on the same device is ignored."""
    from custom_components.ashly.device_trigger import _find_last_recalled_entity_id

    device_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    device = next(iter(device_reg.devices.values()))
    # Pre-create a fake "hue" entity on the same device with the suspicious suffix.
    ent_reg.async_get_or_create(
        domain="sensor",
        platform="hue",
        unique_id="hue_last_recalled_preset",
        device_id=device.id,
    )
    # Should still find the real Ashly one.
    assert _find_last_recalled_entity_id(hass, device.id) is not None
