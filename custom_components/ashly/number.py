"""Number entities for the Ashly Audio integration."""

from __future__ import annotations

import dataclasses

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import AshlyError
from .const import (
    DVCA_LEVEL_MAX_DB,
    DVCA_LEVEL_MIN_DB,
    DVCA_LEVEL_STEP_DB,
    MIC_PREAMP_GAIN_MAX_DB,
    MIC_PREAMP_GAIN_MIN_DB,
    MIC_PREAMP_GAIN_STEP_DB,
    MIXER_LEVEL_MAX_DB,
    MIXER_LEVEL_MIN_DB,
    MIXER_LEVEL_STEP_DB,
    NUM_DVCA_GROUPS,
    NUM_INPUTS,
    NUM_MIXERS,
)
from .coordinator import AshlyConfigEntry, AshlyCoordinator
from .entity import AshlyEntity

PARALLEL_UPDATES = 1


@dataclasses.dataclass(frozen=True, kw_only=True)
class AshlyNumberEntityDescription(NumberEntityDescription):
    """Description for an Ashly number entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AshlyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register number entities."""
    coordinator = entry.runtime_data.coordinator
    entities: list[NumberEntity] = []
    for n in range(1, NUM_DVCA_GROUPS + 1):
        entities.append(AshlyDVCALevelNumber(coordinator, n))
    for m in range(1, NUM_MIXERS + 1):
        for i in range(1, NUM_INPUTS + 1):
            entities.append(AshlyCrosspointLevelNumber(coordinator, m, i))
    for n in range(1, NUM_INPUTS + 1):
        entities.append(AshlyMicPreampGainNumber(coordinator, n))
    async_add_entities(entities)


def _wrap(err: AshlyError, op: str) -> HomeAssistantError:
    return HomeAssistantError(f"Failed to {op}: {err}")


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ── DVCA level ─────────────────────────────────────────────────────────


class AshlyDVCALevelNumber(AshlyEntity, NumberEntity):
    """Level slider for one virtual DCA group (dB)."""

    _attr_native_min_value = DVCA_LEVEL_MIN_DB
    _attr_native_max_value = DVCA_LEVEL_MAX_DB
    _attr_native_step = DVCA_LEVEL_STEP_DB
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:tune-vertical"

    def __init__(self, coordinator: AshlyCoordinator, index: int) -> None:
        dvca_state = (coordinator.data.dvca.get(index)
                      if coordinator.data is not None else None)
        name = dvca_state.name if dvca_state else f"DCA {index}"
        super().__init__(
            coordinator,
            AshlyNumberEntityDescription(key=f"dvca_level_{index}"),
        )
        self._index = index
        self._attr_name = f"{name} level"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        state = data.dvca.get(self._index)
        return state.level_db if state else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and self._index in data.dvca
        )

    async def async_set_native_value(self, value: float) -> None:
        clamped = _clamp(value, self._attr_native_min_value, self._attr_native_max_value)
        try:
            await self.coordinator.client.async_set_dvca_level(self._index, clamped)
        except AshlyError as err:
            raise _wrap(err, f"set DCA {self._index} level") from err
        self._push_optimistic(clamped)

    @callback
    def _push_optimistic(self, level_db: float) -> None:
        data = self.coordinator.data
        if data is None:
            return
        dvca = dict(data.dvca)
        existing = dvca.get(self._index)
        if existing is None:
            return
        dvca[self._index] = dataclasses.replace(existing, level_db=level_db)
        self.coordinator.async_set_updated_data(
            dataclasses.replace(data, dvca=dvca)
        )


# ── Mixer crosspoint source level ──────────────────────────────────────


class AshlyCrosspointLevelNumber(AshlyEntity, NumberEntity):
    """Per-mixer per-input source level (dB).

    96 of these per AQM1208 — disabled by default.
    """

    _attr_native_min_value = MIXER_LEVEL_MIN_DB
    _attr_native_max_value = MIXER_LEVEL_MAX_DB
    _attr_native_step = MIXER_LEVEL_STEP_DB
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:tune-vertical"

    def __init__(
        self,
        coordinator: AshlyCoordinator,
        mixer_index: int,
        input_index: int,
    ) -> None:
        super().__init__(
            coordinator,
            AshlyNumberEntityDescription(
                key=f"xp_level_m{mixer_index}_i{input_index}",
                entity_registry_enabled_default=False,
            ),
        )
        self._mixer = mixer_index
        self._input = input_index
        self._key: tuple[int, int] = (mixer_index, input_index)
        self._attr_name = f"Mixer {mixer_index} input {input_index} level"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        state = data.crosspoints.get(self._key)
        return state.level_db if state else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and self._key in data.crosspoints
        )

    async def async_set_native_value(self, value: float) -> None:
        clamped = _clamp(value, self._attr_native_min_value, self._attr_native_max_value)
        try:
            await self.coordinator.client.async_set_crosspoint_level(
                self._mixer, self._input, clamped
            )
        except AshlyError as err:
            raise _wrap(err, f"set crosspoint {self._mixer}x{self._input} level") from err
        self._push_optimistic(clamped)

    @callback
    def _push_optimistic(self, level_db: float) -> None:
        data = self.coordinator.data
        if data is None:
            return
        crosspoints = dict(data.crosspoints)
        existing = crosspoints.get(self._key)
        if existing is None:
            return
        crosspoints[self._key] = dataclasses.replace(existing, level_db=level_db)
        self.coordinator.async_set_updated_data(
            dataclasses.replace(data, crosspoints=crosspoints)
        )


# ── Mic preamp gain (per mic input) ────────────────────────────────────


class AshlyMicPreampGainNumber(AshlyEntity, NumberEntity):
    """Mic-preamp input gain (0..+66 dB in 6 dB steps)."""

    _attr_native_min_value = MIC_PREAMP_GAIN_MIN_DB
    _attr_native_max_value = MIC_PREAMP_GAIN_MAX_DB
    _attr_native_step = MIC_PREAMP_GAIN_STEP_DB
    _attr_native_unit_of_measurement = "dB"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:microphone"

    def __init__(self, coordinator: AshlyCoordinator, input_number: int) -> None:
        super().__init__(
            coordinator,
            AshlyNumberEntityDescription(
                key=f"mic_preamp_{input_number}",
                entity_category=EntityCategory.CONFIG,
            ),
        )
        self._input = input_number
        self._attr_name = f"Input {input_number} preamp gain"

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        gain = data.mic_preamp_gain.get(self._input)
        return float(gain) if gain is not None else None

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and self._input in data.mic_preamp_gain
        )

    async def async_set_native_value(self, value: float) -> None:
        # Snap to the nearest 6 dB step the device accepts.
        step = MIC_PREAMP_GAIN_STEP_DB
        snapped = round(value / step) * step
        snapped = max(MIC_PREAMP_GAIN_MIN_DB, min(MIC_PREAMP_GAIN_MAX_DB, snapped))
        try:
            await self.coordinator.client.async_set_mic_preamp(self._input, snapped)
        except AshlyError as err:
            raise _wrap(err, f"set input {self._input} preamp gain") from err
        self._push_optimistic(snapped)

    @callback
    def _push_optimistic(self, gain_db: int) -> None:
        data = self.coordinator.data
        if data is None:
            return
        gains = dict(data.mic_preamp_gain)
        gains[self._input] = gain_db
        self.coordinator.async_set_updated_data(
            dataclasses.replace(data, mic_preamp_gain=gains)
        )
