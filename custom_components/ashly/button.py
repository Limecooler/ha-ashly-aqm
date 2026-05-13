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
from homeassistant.core import HomeAssistant, callback
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
    """Register button entities.

    Beyond the static `Identify` button, this dynamically creates one
    `Recall <preset>` button per preset reported by the device. New presets
    are added when the device gains them; removed presets cause their
    buttons to become unavailable. Re-adding a preset with the same id
    creates a fresh entity (the `known_preset_ids` set is cleaned on
    removal so a re-add isn't silently swallowed).
    """
    coordinator = entry.runtime_data.coordinator
    async_add_entities([AshlyIdentifyButton(coordinator)])

    known_preset_ids: set[str] = set()
    preset_entities: dict[str, AshlyRecallPresetButton] = {}

    @callback
    def _refresh_preset_buttons() -> None:
        data = coordinator.data
        if data is None:
            return
        current_ids = {p.id for p in data.presets}
        new_ids = current_ids - known_preset_ids
        removed_ids = known_preset_ids - current_ids
        if new_ids:
            new_entities = [AshlyRecallPresetButton(coordinator, pid) for pid in sorted(new_ids)]
            for ent in new_entities:
                preset_entities[ent.preset_id] = ent
            async_add_entities(new_entities)
        for pid in removed_ids:
            if pid in preset_entities:
                preset_entities.pop(pid).async_mark_removed()
        # IMPORTANT: drop removed ids so a re-add with the same id gets a
        # fresh entity instead of being skipped.
        known_preset_ids.difference_update(removed_ids)
        known_preset_ids.update(new_ids)

    _refresh_preset_buttons()
    entry.async_on_unload(coordinator.async_add_listener(_refresh_preset_buttons))


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


class AshlyRecallPresetButton(AshlyEntity, ButtonEntity):
    """One button per preset on the device; pressing it recalls that preset.

    Disabled by default — a user with 30 presets does not want 30 buttons
    on by default. Enable in the entity registry for the presets you wire
    to dashboards.
    """

    def __init__(self, coordinator: AshlyCoordinator, preset_id: str) -> None:
        super().__init__(
            coordinator,
            AshlyButtonEntityDescription(
                # The MAC-prefixed unique_id is stable across renames on the
                # device because the preset id and name are the same value
                # in AquaControl.
                key=f"recall_preset_{preset_id}",
                translation_key="recall_preset",
                entity_registry_enabled_default=False,
            ),
        )
        self._preset_id = preset_id
        self._attr_translation_placeholders = {"preset": preset_id}
        self._removed = False

    @property
    def preset_id(self) -> str:
        """The preset name/id this button recalls."""
        return self._preset_id

    @property
    def available(self) -> bool:
        if self._removed or not super().available:
            return False
        data = self.coordinator.data
        if data is None:
            return False
        return any(p.id == self._preset_id for p in data.presets)

    async def async_press(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.async_recall_preset(self._preset_id)
        except AshlyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="device_error",
                translation_placeholders={"error": f"recall {self._preset_id}: {err}"},
            ) from err
        # Refresh so last_recalled_preset / state reflects the change quickly.
        await self.coordinator.async_request_refresh()

    @callback
    def async_mark_removed(self) -> None:
        """Mark the button permanently unavailable (preset removed from device)."""
        self._removed = True
        if self.hass is not None:
            self.async_write_ha_state()
