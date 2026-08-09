"""Entități number reglabile live: țintă, avans maxim, toleranță, rate, overshoot."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClimateFlowConfigEntry
from .const import (
    DEFAULT_EXTREME_COOL_C,
    DEFAULT_EXTREME_HEAT_C,
    DEFAULT_MAX_LEAD_MIN,
    DEFAULT_OVERSHOOT_C,
    DEFAULT_RATE_COOL,
    DEFAULT_RATE_HEAT,
    DEFAULT_TARGET_C,
    DEFAULT_TOLERANCE_C,
    VAL_EXTREME_COOL,
    VAL_EXTREME_HEAT,
    VAL_MAX_LEAD,
    VAL_OVERSHOOT,
    VAL_RATE_COOL,
    VAL_RATE_HEAT,
    VAL_TARGET,
    VAL_TOLERANCE,
)
from .coordinator import ClimateFlowCoordinator
from .entity import ClimateFlowEntity


@dataclass(frozen=True, kw_only=True)
class ClimateFlowNumberDef:
    value_key: str
    name: str
    minimum: float
    maximum: float
    step: float
    default: float
    icon: str
    unit: str | None = None


_NUMBERS = [
    ClimateFlowNumberDef(
        value_key=VAL_TARGET,
        name="Țintă temperatură",
        minimum=15,
        maximum=28,
        step=0.5,
        default=DEFAULT_TARGET_C,
        icon="mdi:thermometer-check",
        unit=UnitOfTemperature.CELSIUS,
    ),
    ClimateFlowNumberDef(
        value_key=VAL_MAX_LEAD,
        name="Avans maxim pornire",
        minimum=30,
        maximum=360,
        step=10,
        default=DEFAULT_MAX_LEAD_MIN,
        icon="mdi:clock-fast",
        unit=UnitOfTime.MINUTES,
    ),
    ClimateFlowNumberDef(
        value_key=VAL_TOLERANCE,
        name="Toleranță confort",
        minimum=0.2,
        maximum=2,
        step=0.1,
        default=DEFAULT_TOLERANCE_C,
        icon="mdi:thermometer-lines",
        unit=UnitOfTemperature.CELSIUS,
    ),
    ClimateFlowNumberDef(
        value_key=VAL_RATE_HEAT,
        name="Rată implicită încălzire",
        minimum=0.2,
        maximum=10,
        step=0.1,
        default=DEFAULT_RATE_HEAT,
        icon="mdi:fire",
        unit="°C/h",
    ),
    ClimateFlowNumberDef(
        value_key=VAL_RATE_COOL,
        name="Rată implicită răcire",
        minimum=0.2,
        maximum=10,
        step=0.1,
        default=DEFAULT_RATE_COOL,
        icon="mdi:snowflake",
        unit="°C/h",
    ),
    ClimateFlowNumberDef(
        value_key=VAL_OVERSHOOT,
        name="Supra-țintă precondiționare",
        minimum=0,
        maximum=2,
        step=0.1,
        default=DEFAULT_OVERSHOOT_C,
        icon="mdi:thermometer-plus",
        unit=UnitOfTemperature.CELSIUS,
    ),
    ClimateFlowNumberDef(
        value_key=VAL_EXTREME_COOL,
        name="Setpoint extrem răcire",
        minimum=10,
        maximum=20,
        step=0.5,
        default=DEFAULT_EXTREME_COOL_C,
        icon="mdi:snowflake-thermometer",
        unit=UnitOfTemperature.CELSIUS,
    ),
    ClimateFlowNumberDef(
        value_key=VAL_EXTREME_HEAT,
        name="Setpoint extrem încălzire",
        minimum=25,
        maximum=32,
        step=0.5,
        default=DEFAULT_EXTREME_HEAT_C,
        icon="mdi:fire",
        unit=UnitOfTemperature.CELSIUS,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ClimateFlowNumber(coordinator, d) for d in _NUMBERS)


class ClimateFlowNumber(ClimateFlowEntity, RestoreNumber, NumberEntity):
    """Parametru reglabil persistent care alimentează coordinatorul."""

    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: ClimateFlowCoordinator, definition: ClimateFlowNumberDef
    ) -> None:
        super().__init__(coordinator)
        self._def = definition
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{definition.value_key}"
        self._attr_name = definition.name
        self._attr_native_min_value = definition.minimum
        self._attr_native_max_value = definition.maximum
        self._attr_native_step = definition.step
        self._attr_icon = definition.icon
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_native_value = definition.default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_number_data()
        if restored is not None and restored.native_value is not None:
            self._attr_native_value = restored.native_value
        self.coordinator.set_value(self._def.value_key, self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.coordinator.set_value(self._def.value_key, value)
        self.async_write_ha_state()
