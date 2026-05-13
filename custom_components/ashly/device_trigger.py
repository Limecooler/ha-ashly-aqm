"""Device triggers for the Ashly Audio integration.

Currently exposes one trigger type, `preset_recalled`, which fires whenever
the `last_recalled_preset` sensor for a device changes to a non-empty
state. The implementation defers to HA's `state` trigger so users get the
same change-detection semantics as automations they could have written by
hand against the underlying entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.homeassistant.triggers import state as state_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN

if TYPE_CHECKING:
    pass

TRIGGER_TYPES = {"preset_recalled"}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)})


def _find_last_recalled_entity_id(hass: HomeAssistant, device_id: str) -> str | None:
    """Return the entity_id of the `last_recalled_preset` sensor for a device."""
    ent_reg = er.async_get(hass)
    for entry in er.async_entries_for_device(ent_reg, device_id, include_disabled_entities=False):
        if entry.platform != DOMAIN:
            continue
        if entry.unique_id.endswith("_last_recalled_preset"):
            return entry.entity_id
    return None


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """List available device triggers."""
    if _find_last_recalled_entity_id(hass, device_id) is None:
        return []
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DEVICE_ID: device_id,
            CONF_DOMAIN: DOMAIN,
            CONF_TYPE: "preset_recalled",
        }
    ]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a trigger; under the hood, watch the last_recalled sensor for a
    change to a meaningful value (non-empty, not unknown/unavailable)."""
    entity_id = _find_last_recalled_entity_id(hass, config[CONF_DEVICE_ID])
    if entity_id is None:
        # The device exists but the sensor doesn't (could be a stale device
        # left after entity removal); raise so the automation engine surfaces
        # the misconfiguration.
        raise ValueError(f"No last_recalled_preset entity for device {config[CONF_DEVICE_ID]!r}")
    state_config = {
        CONF_PLATFORM: "state",
        CONF_ENTITY_ID: [entity_id],
    }
    state_config = await state_trigger.async_validate_trigger_config(hass, state_config)
    return await state_trigger.async_attach_trigger(
        hass, state_config, action, trigger_info, platform_type="device"
    )
