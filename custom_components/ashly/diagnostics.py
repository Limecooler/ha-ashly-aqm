"""Diagnostics support for the Ashly Audio integration."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import AshlyConfigEntry

TO_REDACT = {"password", "host", "mac_address"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AshlyConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data

    return {
        "config_entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "config_entry_options": dict(entry.options),
        "system_info": async_redact_data(asdict(data.system_info), TO_REDACT),
        "front_panel": asdict(data.front_panel),
        "power_on": data.power_on,
        "channels": {cid: asdict(c) for cid, c in data.channels.items()},
        "chains": {cid: asdict(s) for cid, s in data.chains.items()},
        "dvca": {str(idx): asdict(s) for idx, s in data.dvca.items()},
        "crosspoints": {
            f"{m}.{i}": asdict(s) for (m, i), s in data.crosspoints.items()
        },
        "presets": [asdict(p) for p in data.presets],
        "phantom_power": {str(k): v for k, v in data.phantom_power.items()},
        "mic_preamp_gain": {str(k): v for k, v in data.mic_preamp_gain.items()},
        "gpo": {str(k): v for k, v in data.gpo.items()},
        "last_recalled_preset": asdict(data.last_recalled_preset),
    }
