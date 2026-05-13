"""Switch entities for the Ashly Audio integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import AshlyError, input_channel_id, output_channel_id
from .const import DOMAIN, NUM_DVCA_GROUPS, NUM_GPO, NUM_INPUTS, NUM_MIXERS, NUM_OUTPUTS
from .coordinator import AshlyConfigEntry, AshlyCoordinator
from .entity import AshlyEntity

# Serialise writes to the embedded device — reads come from the coordinator.
PARALLEL_UPDATES = 1


@dataclasses.dataclass(frozen=True, kw_only=True)
class AshlySwitchEntityDescription(SwitchEntityDescription):
    """Description for an Ashly switch entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AshlyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register switch entities."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = [AshlyPowerSwitch(coordinator)]

    for n in range(1, NUM_INPUTS + 1):
        entities.append(AshlyChainMuteSwitch(coordinator, input_channel_id(n)))
    for n in range(1, NUM_OUTPUTS + 1):
        entities.append(AshlyChainMuteSwitch(coordinator, output_channel_id(n)))

    for n in range(1, NUM_DVCA_GROUPS + 1):
        entities.append(AshlyDVCAMuteSwitch(coordinator, n))

    for m in range(1, NUM_MIXERS + 1):
        for i in range(1, NUM_INPUTS + 1):
            entities.append(AshlyCrosspointMuteSwitch(coordinator, m, i))

    # Front-panel LED enable (single switch)
    entities.append(AshlyFrontPanelLEDSwitch(coordinator))

    # Phantom power (12 mic inputs)
    for n in range(1, NUM_INPUTS + 1):
        entities.append(AshlyPhantomPowerSwitch(coordinator, n))

    # General-purpose outputs (2 pins)
    for n in range(1, NUM_GPO + 1):
        entities.append(AshlyGPOSwitch(coordinator, n))

    async_add_entities(entities)


def _wrap(err: AshlyError, op: str) -> HomeAssistantError:
    """Re-raise client exceptions as HA-friendly errors.

    `op` is included only via the translation placeholder; the user-facing
    string is sourced from translations/<lang>.json under exceptions.device_error.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="device_error",
        translation_placeholders={"error": f"{op}: {err}"},
    )


# ── Power ──────────────────────────────────────────────────────────────


class AshlyPowerSwitch(AshlyEntity, SwitchEntity):
    """Front-panel power switch — the device's primary entity."""

    # Inherit the device name (no per-entity suffix).
    _attr_name = None

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(key="power"),
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.power_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_power(True)
        except AshlyError as err:
            raise _wrap(err, "turn power on") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_power(False)
        except AshlyError as err:
            raise _wrap(err, "turn power off") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, on: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        self.coordinator.async_set_updated_data(
            dataclasses.replace(
                data,
                front_panel=dataclasses.replace(data.front_panel, power_on=on),
            )
        )


# ── Chain mute (per channel) ───────────────────────────────────────────


class AshlyChainMuteSwitch(AshlyEntity, SwitchEntity):
    """Per-channel chain mute toggle.

    `is_on` means the chain is muted, mirroring the device API.
    """

    def __init__(
        self,
        coordinator: AshlyCoordinator,
        channel_id: str,
    ) -> None:
        kind, _, number = channel_id.partition(".")
        is_input = kind == "InputChannel"
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key=f"chain_mute_{channel_id}",
                translation_key=("input_channel_mute" if is_input else "output_channel_mute"),
            ),
        )
        self._channel_id = channel_id
        self._attr_translation_placeholders = {"channel_number": number}

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        chain = data.chains.get(self._channel_id)
        return bool(chain.muted) if chain else False

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._channel_id in data.chains

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_chain_mute(self._channel_id, True)
        except AshlyError as err:
            raise _wrap(err, f"mute {self._channel_id}") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_chain_mute(self._channel_id, False)
        except AshlyError as err:
            raise _wrap(err, f"unmute {self._channel_id}") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, muted: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        chains = dict(data.chains)
        existing = chains.get(self._channel_id)
        if existing is None:
            return
        chains[self._channel_id] = dataclasses.replace(existing, muted=muted)
        self.coordinator.async_set_updated_data(dataclasses.replace(data, chains=chains))


# ── DVCA mute ──────────────────────────────────────────────────────────


class AshlyDVCAMuteSwitch(AshlyEntity, SwitchEntity):
    """Mute toggle for one virtual DCA group."""

    def __init__(self, coordinator: AshlyCoordinator, index: int) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key=f"dvca_mute_{index}",
                translation_key="dvca_mute",
            ),
        )
        self._index = index
        # Surface the device-side DCA name when the user has set one (e.g.
        # "Bar", "Patio") so entity names match the operator's mental
        # model. Fall back to "DCA <index>" when the device has the default
        # name. Reads coordinator.data populated by the first-refresh
        # that already completed before platform setup forwards entities.
        dvca_state = coordinator.data.dvca.get(index) if coordinator.data is not None else None
        name = (
            dvca_state.name
            if dvca_state and dvca_state.name and dvca_state.name != f"DCA {index}"
            else f"DCA {index}"
        )
        self._attr_translation_placeholders = {"name": name}

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        state = data.dvca.get(self._index)
        return bool(state.muted) if state else False

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._index in data.dvca

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_dvca_mute(self._index, True)
        except AshlyError as err:
            raise _wrap(err, f"mute DCA {self._index}") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_dvca_mute(self._index, False)
        except AshlyError as err:
            raise _wrap(err, f"unmute DCA {self._index}") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, muted: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        dvca = dict(data.dvca)
        existing = dvca.get(self._index)
        if existing is None:
            return
        dvca[self._index] = dataclasses.replace(existing, muted=muted)
        self.coordinator.async_set_updated_data(dataclasses.replace(data, dvca=dvca))


# ── Crosspoint source mute (mixer x input) ─────────────────────────────


class AshlyCrosspointMuteSwitch(AshlyEntity, SwitchEntity):
    """Per-mixer per-input source mute.

    The device has 8 mixers x 12 inputs = 96 of these per AQM1208.
    Disabled by default to keep the entity registry sane out of the box.
    """

    def __init__(
        self,
        coordinator: AshlyCoordinator,
        mixer_index: int,
        input_index: int,
    ) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key=f"xp_mute_m{mixer_index}_i{input_index}",
                translation_key="crosspoint_mute",
                entity_registry_enabled_default=False,
            ),
        )
        self._mixer = mixer_index
        self._input = input_index
        self._key: tuple[int, int] = (mixer_index, input_index)
        self._attr_translation_placeholders = {
            "mixer_number": str(mixer_index),
            "input_number": str(input_index),
        }

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        state = data.crosspoints.get(self._key)
        return bool(state.muted) if state else False

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._key in data.crosspoints

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_crosspoint_mute(self._mixer, self._input, True)
        except AshlyError as err:
            raise _wrap(err, f"mute crosspoint {self._mixer}x{self._input}") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_crosspoint_mute(self._mixer, self._input, False)
        except AshlyError as err:
            raise _wrap(err, f"unmute crosspoint {self._mixer}x{self._input}") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, muted: bool) -> None:
        # Route through the coordinator's debouncer so N crosspoint changes
        # within a 50 ms window collapse into one entity-state fan-out.
        self.coordinator.queue_crosspoint_patch(self._key, muted=muted)


# ── Front-panel LED enable (single switch) ─────────────────────────────


class AshlyFrontPanelLEDSwitch(AshlyEntity, SwitchEntity):
    """Toggle the device's front-panel status LEDs."""

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key="front_panel_leds",
                translation_key="front_panel_leds",
                entity_category=EntityCategory.CONFIG,
            ),
        )

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return data is not None and data.front_panel.leds_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_front_panel_leds(True)
        except AshlyError as err:
            raise _wrap(err, "enable front-panel LEDs") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_front_panel_leds(False)
        except AshlyError as err:
            raise _wrap(err, "disable front-panel LEDs") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, enabled: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        self.coordinator.async_set_updated_data(
            dataclasses.replace(
                data,
                front_panel=dataclasses.replace(data.front_panel, leds_enabled=enabled),
            )
        )


# ── Phantom power (per mic input) ──────────────────────────────────────


class AshlyPhantomPowerSwitch(AshlyEntity, SwitchEntity):
    """Toggle +48V phantom power on a mic/line input."""

    def __init__(self, coordinator: AshlyCoordinator, input_number: int) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key=f"phantom_power_{input_number}",
                translation_key="phantom_power",
                entity_category=EntityCategory.CONFIG,
            ),
        )
        self._input = input_number
        self._attr_translation_placeholders = {"channel_number": str(input_number)}

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        return bool(data.phantom_power.get(self._input, False))

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._input in data.phantom_power

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_phantom_power(self._input, True)
        except AshlyError as err:
            raise _wrap(err, f"enable phantom power on input {self._input}") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_phantom_power(self._input, False)
        except AshlyError as err:
            raise _wrap(err, f"disable phantom power on input {self._input}") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, enabled: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        phantom = dict(data.phantom_power)
        phantom[self._input] = enabled
        self.coordinator.async_set_updated_data(dataclasses.replace(data, phantom_power=phantom))


# ── General-purpose outputs (rear panel GPO pins) ──────────────────────


class AshlyGPOSwitch(AshlyEntity, SwitchEntity):
    """Drive a rear-panel GPO pin high or low."""

    def __init__(self, coordinator: AshlyCoordinator, pin_number: int) -> None:
        super().__init__(
            coordinator,
            AshlySwitchEntityDescription(
                key=f"gpo_{pin_number}",
                translation_key="gpo",
            ),
        )
        self._pin = pin_number
        self._attr_translation_placeholders = {"pin_number": str(pin_number)}

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        if data is None:
            return False
        return bool(data.gpo.get(self._pin, False))

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._pin in data.gpo

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_gpo(self._pin, True)
        except AshlyError as err:
            raise _wrap(err, f"drive GPO {self._pin} high") from err
        self._push_optimistic(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_set_gpo(self._pin, False)
        except AshlyError as err:
            raise _wrap(err, f"drive GPO {self._pin} low") from err
        self._push_optimistic(False)

    @callback
    def _push_optimistic(self, high: bool) -> None:
        data = self.coordinator.data
        if data is None:
            return
        gpo = dict(data.gpo)
        gpo[self._pin] = high
        self.coordinator.async_set_updated_data(dataclasses.replace(data, gpo=gpo))
