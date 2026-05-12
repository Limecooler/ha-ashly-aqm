"""Services exposed by the Ashly Audio integration.

Currently provides one service: ``ashly.recall_preset``. The recall is
done via the cookie-authenticated ``POST /v1.0-beta/preset/recall/{name}``
endpoint, which does NOT require the SimpleControl user account that
Ashly's Simple Control Integration Guide describes — that's only needed
for the parallel ``/simplecontrol/*`` endpoint tree, which this
integration does not use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .client import AshlyError
from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import AshlyConfigEntry

SERVICE_RECALL_PRESET = "recall_preset"

# A single device_id or list of them.
RECALL_PRESET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.Any(str, [str]),
        vol.Required("preset"): vol.All(str, vol.Length(min=1)),
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register integration-level services. Idempotent."""
    if hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET):
        return

    async def _recall_preset(call: ServiceCall) -> None:
        preset = str(call.data["preset"]).strip()
        device_ids = call.data["device_id"]
        if isinstance(device_ids, str):
            device_ids = [device_ids]

        device_reg = dr.async_get(hass)
        entries: list[AshlyConfigEntry] = []
        for device_id in device_ids:
            device = device_reg.async_get(device_id)
            if device is None:
                raise ServiceValidationError(f"Unknown device id: {device_id}")
            # Find the Ashly config entry that owns this device.
            entry_id = next(
                (
                    eid
                    for eid in device.config_entries
                    if (e := hass.config_entries.async_get_entry(eid)) is not None
                    and e.domain == DOMAIN
                ),
                None,
            )
            if entry_id is None:
                raise ServiceValidationError(f"Device {device_id} is not an Ashly device")
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is None or not hasattr(entry, "runtime_data"):
                raise ServiceValidationError(f"Ashly entry for device {device_id} is not loaded")
            entries.append(entry)

        # Resolve preset by exact name first; then by numeric position in
        # the coordinator's preset list as a convenience for templates.
        for entry in entries:
            coordinator = entry.runtime_data.coordinator
            client = entry.runtime_data.client
            data = coordinator.data
            preset_names = [p.name for p in data.presets] if data is not None else []

            resolved = _resolve_preset_name(preset, preset_names)
            if resolved is None:
                raise ServiceValidationError(
                    f"Preset {preset!r} not found on {client.host}; "
                    f"available: {', '.join(preset_names) or '(none)'}"
                )
            try:
                await client.async_recall_preset(resolved)
            except AshlyError as err:
                raise HomeAssistantError(
                    f"Failed to recall preset {resolved!r} on {client.host}: {err}"
                ) from err
            # Refresh the coordinator so `last_recalled_preset` and any
            # state that changed (mutes, levels, mixer assignments)
            # propagates to HA promptly.
            await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_RECALL_PRESET, _recall_preset, schema=RECALL_PRESET_SCHEMA
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove the integration-level services. Only called on the last unload."""
    if hass.services.has_service(DOMAIN, SERVICE_RECALL_PRESET):
        hass.services.async_remove(DOMAIN, SERVICE_RECALL_PRESET)


def _resolve_preset_name(input_: str, available: list[str]) -> str | None:
    """Map a user-supplied preset reference to a real preset name.

    Accepts:
      - the preset's exact name (case-sensitive)
      - a 1-based numeric index into the preset list (e.g. "1" → first)

    Returns None if the input doesn't match either.
    """
    if input_ in available:
        return input_
    # 1-based numeric index — useful for automations templated from the
    # `preset_count` sensor's attributes list.
    if input_.isdigit():
        idx = int(input_) - 1
        if 0 <= idx < len(available):
            return available[idx]
    return None
