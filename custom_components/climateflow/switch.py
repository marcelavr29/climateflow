"""Entități switch: master, garda anti-modificare, notificări."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ClimateFlowConfigEntry
from .const import VAL_ENABLED, VAL_GUARD, VAL_NOTIFY
from .coordinator import ClimateFlowCoordinator
from .entity import ClimateFlowEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            ClimateFlowToggle(
                coordinator,
                value_key=VAL_ENABLED,
                name="Sistem activ",
                default=True,
                icon="mdi:power",
            ),
            ClimateFlowToggle(
                coordinator,
                value_key=VAL_GUARD,
                name="Gardă anti-modificare",
                default=True,
                icon="mdi:shield-lock",
                category=EntityCategory.CONFIG,
            ),
            ClimateFlowToggle(
                coordinator,
                value_key=VAL_NOTIFY,
                name="Notificări",
                default=True,
                icon="mdi:bell",
                category=EntityCategory.CONFIG,
            ),
        ]
    )


class ClimateFlowToggle(ClimateFlowEntity, RestoreEntity, SwitchEntity):
    """Comutator persistent care alimentează coordinatorul."""

    def __init__(
        self,
        coordinator: ClimateFlowCoordinator,
        *,
        value_key: str,
        name: str,
        default: bool,
        icon: str | None = None,
        category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._value_key = value_key
        self._default = default
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{value_key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = category
        self._attr_is_on = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            self._attr_is_on = last_state.state == "on"
        self.coordinator.set_value(self._value_key, self._attr_is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.coordinator.set_value(self._value_key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.coordinator.set_value(self._value_key, False)
        self.async_write_ha_state()
