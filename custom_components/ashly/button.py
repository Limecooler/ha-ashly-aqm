"""Button entities for the Ashly Audio integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import AshlyError
from .const import DOMAIN
from .coordinator import AshlyConfigEntry, AshlyCoordinator
from .entity import AshlyEntity

PARALLEL_UPDATES = 1


@dataclasses.dataclass(frozen=True, kw_only=True)
class AshlyButtonEntityDescription(ButtonEntityDescription):
    """Description for an Ashly button entity."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AshlyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Register button entities."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([AshlyIdentifyButton(coordinator)])


class AshlyIdentifyButton(AshlyEntity, ButtonEntity):
    """Trigger the device's identify (front-panel LED blink)."""

    def __init__(self, coordinator: AshlyCoordinator) -> None:
        super().__init__(
            coordinator,
            AshlyButtonEntityDescription(
                key="identify",
                device_class=ButtonDeviceClass.IDENTIFY,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
        )

    async def async_press(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_identify()
        except AshlyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_error",
                translation_placeholders={"error": f"identify: {err}"},
            ) from err
