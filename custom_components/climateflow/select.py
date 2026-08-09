"""Entități select: sezonul și plafonul de ventilator din timpul evenimentului."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ClimateFlowConfigEntry
from .const import (
    DEFAULT_FAN_CAP,
    FAN_CAP_OPTIONS,
    SEASON_AUTO,
    SEASON_OPTIONS,
    VAL_FAN_CAP,
    VAL_SEASON,
)
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
            ClimateFlowSelect(
                coordinator,
                value_key=VAL_SEASON,
                name="Sezon",
                options=SEASON_OPTIONS,
                default=SEASON_AUTO,
                icon="mdi:sun-snowflake-variant",
            ),
            ClimateFlowSelect(
                coordinator,
                value_key=VAL_FAN_CAP,
                name="Ventilator maxim în eveniment",
                options=FAN_CAP_OPTIONS,
                default=DEFAULT_FAN_CAP,
                icon="mdi:fan-chevron-down",
                category=EntityCategory.CONFIG,
            ),
        ]
    )


class ClimateFlowSelect(ClimateFlowEntity, RestoreEntity, SelectEntity):
    """Select persistent care alimentează coordinatorul."""

    def __init__(
        self,
        coordinator: ClimateFlowCoordinator,
        *,
        value_key: str,
        name: str,
        options: list[str],
        default: str,
        icon: str | None = None,
        category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._value_key = value_key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{value_key}"
        self._attr_name = name
        self._attr_options = options
        self._attr_current_option = default
        self._attr_icon = icon
        self._attr_entity_category = category

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in self._attr_options:
            self._attr_current_option = last_state.state
        self.coordinator.set_value(self._value_key, self._attr_current_option)

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.coordinator.set_value(self._value_key, option)
        self.async_write_ha_state()
