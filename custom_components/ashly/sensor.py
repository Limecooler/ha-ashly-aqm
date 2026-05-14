"""Sensor entities for the Ashly Audio integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import METER_INPUT_RANGE_DB, NUM_INPUTS
from .coordinator import AshlyConfigEntry, AshlyCoordinator
from .entity import AshlyEntity
from .meter import METER_FLOOR_DB, AshlyMeterClient, raw_to_db

PARALLEL_UPDATES = 0  # all reads come from the coordinator's poll


@dataclasses.dataclass(frozen=True, kw_only=True)
class AshlySensorEntityDescription(SensorEntityDescription):
    """Description for an Ashly sensor entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AshlyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register sensor entities."""
    coordinator = entry.runtime_data.coordinator
    meter_client: AshlyMeterClient = entry.runtime_data.meter_client
    entities: list[SensorEntity] = [
        AshlyFirmwareSensor(coordinator),
        AshlyPresetCountSensor(coordinator),
        AshlyLastRecalledPresetSensor(coordinator),
        AshlyIPAddressSensor(coordinator),
    ]
    # 12 input + 12 mixer-input meters. All disabled by default — they
    # update at ~1 Hz and would otherwise clutter the recorder.
    for n in range(1, NUM_INPUTS + 1):
        entities.append(AshlyChannelMeterSensor(coordinator, meter_client, kind="input", channel=n))
        entities.append(AshlyChannelMeterSensor(coordinator, meter_client, kind="mixer", channel=n))
    async_add_entities(entities)


class AshlyFirmwareSensor(AshlyEntity, RestoreEntity, SensorEntity):
    """Reports the device's firmware (software) revision.

    Restores the prior firmware string across HA restarts so the entity
    isn't 'unavailable' for ~30 s after restart while the coordinator
    completes its first poll.
    """

    _restored: str | None = None

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySensorEntityDescription(
                key="firmware_version",
                translation_key="firmware_version",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            ),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            self._restored = last.state

    @property
    def native_value(self) -> str | None:
        info = self.coordinator.system_info
        if info is not None:
            return info.firmware_version
        return self._restored


class AshlyPresetCountSensor(AshlyEntity, RestoreEntity, SensorEntity):
    """Number of stored presets on the device.

    Useful as an automation trigger (e.g. notify when presets change).
    The full preset list is exposed as an attribute. Restores its last
    value across HA restarts.
    """

    _restored: int | None = None

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySensorEntityDescription(
                key="preset_count",
                translation_key="preset_count",
                entity_category=EntityCategory.DIAGNOSTIC,
                entity_registry_enabled_default=False,
            ),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            try:
                self._restored = int(last.state)
            except (TypeError, ValueError):
                self._restored = None

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is not None:
            return len(data.presets)
        return self._restored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        return {"presets": [{"id": p.id, "name": p.name} for p in data.presets]}


class AshlyLastRecalledPresetSensor(AshlyEntity, RestoreEntity, SensorEntity):
    """Name of the most-recently-recalled preset (or `None`).

    `modified` (True if working state has drifted since the recall) is
    exposed as an attribute. Restores its last value across HA restarts.
    """

    _restored: str | None = None

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySensorEntityDescription(
                key="last_recalled_preset",
                translation_key="last_recalled_preset",
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in ("unknown", "unavailable"):
            self._restored = last.state

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is not None:
            return data.last_recalled_preset.name
        return self._restored

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        if data is None:
            return {}
        return {"modified": data.last_recalled_preset.modified}


class AshlyIPAddressSensor(AshlyEntity, SensorEntity):
    """The device's IP address (or hostname) as configured in HA.

    Surfaced as a diagnostic sensor so the IP shows up immediately below the
    device-info area without needing to click into the configuration URL.
    Tracks the entry's stored host so it auto-updates if the user
    reconfigures the integration after a DHCP-induced IP change.
    """

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySensorEntityDescription(
                key="ip_address",
                translation_key="ip_address",
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    @property
    def native_value(self) -> str:
        return self.coordinator.client.host

    @property
    def available(self) -> bool:
        # The IP comes from the config entry, not from a coordinator poll,
        # so this stays available even while the device is unreachable —
        # useful for "the IP I should be reaching" troubleshooting.
        return True


# ── Live signal meters (per input / mixer-input channel) ───────────────


class AshlyChannelMeterSensor(AshlyEntity, SensorEntity):
    """Live signal-level sensor for one channel.

    `kind="input"` exposes the raw mic/line input post-preamp (metermap
    positions 0..11); `kind="mixer"` exposes the post-DSP mixer-input
    level (positions 12..23). Updates at ~1 Hz from a background
    socket.io stream.

    Intentionally has no `device_class` (SOUND_PRESSURE is for acoustic
    SPL, not line-level dBu) and no `state_class` (these are noisy live
    meters — recording them as long-term statistics has no analytical
    value and floods the recorder DB).
    """

    _attr_native_unit_of_measurement = "dB"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: AshlyCoordinator,
        meter_client: AshlyMeterClient,
        *,
        kind: str,
        channel: int,
    ) -> None:
        if kind == "input":
            meter_index = channel - 1  # positions 0..11
            translation_key = "input_meter"
        elif kind == "mixer":
            meter_index = NUM_INPUTS + channel - 1  # positions 12..23
            translation_key = "mixer_input_meter"
        else:
            raise ValueError(f"Unknown meter kind: {kind!r}")
        super().__init__(
            coordinator,
            AshlySensorEntityDescription(
                key=f"meter_{kind}_{channel}",
                translation_key=translation_key,
                entity_category=EntityCategory.DIAGNOSTIC,
                # Disabled by default: 24 noisy push-based sensors per
                # device is too much for the default UI. Users enable
                # the channels they care about.
                entity_registry_enabled_default=False,
            ),
        )
        self._meter_client = meter_client
        self._meter_index = meter_index
        self._attr_translation_placeholders = {"channel_number": str(channel)}
        # Seed with the floor so the entity has a value immediately.
        self._cached_db: float = METER_FLOOR_DB

    @property
    def native_value(self) -> float | None:
        return self._cached_db

    @property
    def available(self) -> bool:
        # Don't flap on every reconnect; the client emits its own
        # `disconnected` log line and the value goes stale gracefully.
        # The base class still gates availability on the coordinator's
        # last-update-success state.
        return super().available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Seed once from whatever's already buffered.
        records = self._meter_client.latest_records
        if self._meter_index < len(records):
            self._cached_db = raw_to_db(records[self._meter_index])
        # HA tracks the remove-listener callback for us via
        # `async_on_remove`, so we don't have to manage it manually.
        self.async_on_remove(self._meter_client.add_listener(self._on_meter_update))

    @callback
    def _on_meter_update(self, records: list[int]) -> None:
        if self._meter_index >= len(records):
            return
        new_db = raw_to_db(records[self._meter_index])
        # Clamp to the documented range.
        lo, hi = METER_INPUT_RANGE_DB
        new_db = max(lo, min(hi, new_db))
        if new_db != self._cached_db:
            self._cached_db = new_db
            self.async_write_ha_state()
