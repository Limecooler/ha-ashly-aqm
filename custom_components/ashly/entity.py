"""Base entity for the Ashly Audio integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AshlyCoordinator


class AshlyEntity(CoordinatorEntity[AshlyCoordinator]):
    """Common base for every Ashly entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AshlyCoordinator,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        if coordinator.system_info is None:
            raise RuntimeError("Coordinator system_info not initialized")

        info = coordinator.system_info
        mac = format_mac(info.mac_address)
        self.entity_description = description
        self._attr_unique_id = f"{mac}_{description.key}"

        device_name = info.name or f"Ashly {info.model}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=device_name,
            manufacturer="Ashly Audio",
            model=info.model,
            sw_version=info.firmware_version,
            hw_version=info.hardware_revision or None,
            configuration_url=(f"http://{coordinator.client.host}:{coordinator.client.port}"),
        )
