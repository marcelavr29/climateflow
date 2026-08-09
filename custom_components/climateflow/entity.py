"""Bază comună pentru entitățile integrării (device info)."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import ClimateFlowCoordinator


class ClimateFlowEntity(Entity):
    """Atașează entitatea la un singur dispozitiv „ClimateFlow”."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: ClimateFlowCoordinator) -> None:
        self.coordinator = coordinator
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name="ClimateFlow",
            manufacturer="tbfapps",
            model="Climatizare pentru evenimente",
        )
