"""Device actions for the Ashly Audio integration.

Exposes one action — `recall_preset` — so users can pick it from the
automation UI's "Then do…" device-action picker instead of editing YAML
to invoke the `ashly.recall_preset` service directly. The action takes a
single string field, `preset`, which can be a preset name (exact match)
or a 1-based numeric index — same semantics as the service.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_TYPE,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from .const import DOMAIN
from .services import SERVICE_RECALL_PRESET

ACTION_TYPES = {"recall_preset"}
CONF_PRESET = "preset"

ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(ACTION_TYPES),
        vol.Required(CONF_PRESET): vol.All(str, vol.Length(min=1)),
    }
)


async def async_get_actions(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    """List available device actions for an Ashly device."""
    return [
        {
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: "recall_preset",
        }
    ]


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """Describe the extra fields the recall_preset action takes in the UI."""
    return {
        "extra_fields": vol.Schema(
            {vol.Required(CONF_PRESET, description={"suggested_value": ""}): str}
        )
    }


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Translate the device action to a service call on `ashly.recall_preset`."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECALL_PRESET,
        {
            "device_id": config[CONF_DEVICE_ID],
            CONF_PRESET: config[CONF_PRESET],
        },
        blocking=True,
        context=context,
    )
