"""Select entities for the Ashly Audio integration.

One select per output, exposing the assigned mixer ("Mixer.1"…"Mixer.8" or
"None"). Setting it routes a different mixer's bus to the output.
"""

from __future__ import annotations

import dataclasses

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import AshlyError, output_channel_id
from .const import DOMAIN, NO_MIXER, NUM_MIXERS, NUM_OUTPUTS
from .coordinator import AshlyConfigEntry, AshlyCoordinator
from .entity import AshlyEntity

PARALLEL_UPDATES = 1


@dataclasses.dataclass(frozen=True, kw_only=True)
class AshlySelectEntityDescription(SelectEntityDescription):
    """Description for an Ashly select entity."""


def _mixer_options() -> list[str]:
    return [NO_MIXER] + [f"Mixer.{n}" for n in range(1, NUM_MIXERS + 1)]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AshlyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register select entities."""
    coordinator = entry.runtime_data.coordinator
    entities = [AshlyOutputMixerSelect(coordinator, n) for n in range(1, NUM_OUTPUTS + 1)]
    async_add_entities(entities)


class AshlyOutputMixerSelect(AshlyEntity, SelectEntity):
    """Select which mixer feeds a given output channel."""

    def __init__(self, coordinator: AshlyCoordinator, output_number: int) -> None:
        channel_id = output_channel_id(output_number)
        super().__init__(
            coordinator,
            AshlySelectEntityDescription(
                key=f"output_mixer_{output_number}",
                translation_key="output_mixer",
                options=_mixer_options(),
            ),
        )
        self._output_number = output_number
        self._channel_id = channel_id
        self._attr_options = _mixer_options()
        self._attr_translation_placeholders = {"output_number": str(output_number)}

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None
        chain = data.chains.get(self._channel_id)
        if chain is None:
            return None
        return chain.mixer_id or NO_MIXER

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return super().available and data is not None and self._channel_id in data.chains

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_mixer_option",
                translation_placeholders={"option": option},
            )
        try:
            await self.coordinator.client.async_set_output_mixer(self._channel_id, option)
        except AshlyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_error",
                translation_placeholders={
                    "error": f"set output {self._output_number} mixer: {err}"
                },
            ) from err
        self._push_optimistic(option)

    @callback
    def _push_optimistic(self, option: str) -> None:
        data = self.coordinator.data
        if data is None:
            return
        chains = dict(data.chains)
        existing = chains.get(self._channel_id)
        if existing is None:
            return
        new_mixer = None if option == NO_MIXER else option
        chains[self._channel_id] = dataclasses.replace(existing, mixer_id=new_mixer)
        self.coordinator.async_set_updated_data(dataclasses.replace(data, chains=chains))
