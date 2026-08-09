"""Senzori: următorul eveniment, ETA precondiționare, stare, temperaturi, învățare."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import ClimateFlowConfigEntry
from .const import (
    STATE_COOLDOWN,
    STATE_EVENT,
    STATE_IDLE,
    STATE_PRECONDITIONING,
)
from .coordinator import ClimateFlowCoordinator
from .entity import ClimateFlowEntity


def _timestamp(value: object) -> object:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


@dataclass(frozen=True, kw_only=True)
class ClimateFlowSensorDef:
    key: str
    name: str
    value_fn: Callable[[dict], object]
    attrs_fn: Callable[[dict], dict] = field(default=lambda d: {})
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    icon: str | None = None
    suggested_precision: int | None = None
    category: EntityCategory | None = None
    options: list[str] | None = None


_DEFS: list[ClimateFlowSensorDef] = [
    ClimateFlowSensorDef(
        key="next_event",
        name="Următorul eveniment",
        value_fn=lambda d: _timestamp(d.get("next_event")),
        attrs_fn=lambda d: {
            "name": d.get("next_event_name"),
            "end": d.get("next_event_end"),
            "target": d.get("next_event_target"),
            "skip_armed": d.get("skip_next_armed"),
        },
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:calendar-clock",
    ),
    ClimateFlowSensorDef(
        key="precondition_start",
        name="Pornire precondiționare",
        value_fn=lambda d: _timestamp(d.get("precondition_start")),
        attrs_fn=lambda d: {"lead_minutes": d.get("lead_minutes"), "mode": d.get("mode")},
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-start",
    ),
    ClimateFlowSensorDef(
        key="state",
        name="Stare sistem",
        value_fn=lambda d: d.get("state"),
        attrs_fn=lambda d: {"season": d.get("season"), "mode": d.get("mode")},
        device_class=SensorDeviceClass.ENUM,
        options=[STATE_IDLE, STATE_PRECONDITIONING, STATE_EVENT, STATE_COOLDOWN],
        icon="mdi:state-machine",
    ),
    ClimateFlowSensorDef(
        key="indoor_temp",
        name="Temperatură interior",
        value_fn=lambda d: d.get("indoor_temp"),
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_precision=1,
    ),
    ClimateFlowSensorDef(
        key="outdoor_temp",
        name="Temperatură exterior",
        value_fn=lambda d: d.get("outdoor_temp"),
        unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_precision=1,
    ),
    ClimateFlowSensorDef(
        key="learned_rate_current",
        name="Rată termică (condiții curente)",
        value_fn=lambda d: d.get("learned_rate_current"),
        unit="°C/h",
        icon="mdi:chart-line",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_precision=2,
        category=EntityCategory.DIAGNOSTIC,
    ),
    ClimateFlowSensorDef(
        key="sample_count",
        name="Eșantioane învățate",
        value_fn=lambda d: d.get("sample_count"),
        attrs_fn=lambda d: {"rates": d.get("rates")},
        icon="mdi:school",
        state_class=SensorStateClass.TOTAL_INCREASING,
        category=EntityCategory.DIAGNOSTIC,
    ),
    ClimateFlowSensorDef(
        key="on_time_pct",
        name="Punctualitate (30 zile)",
        value_fn=lambda d: d.get("on_time_pct"),
        unit="%",
        icon="mdi:target",
        suggested_precision=0,
        category=EntityCategory.DIAGNOSTIC,
    ),
    ClimateFlowSensorDef(
        key="lead_minutes",
        name="Avans calculat",
        value_fn=lambda d: d.get("lead_minutes"),
        unit=UnitOfTime.MINUTES,
        icon="mdi:timer-sand",
        suggested_precision=0,
        category=EntityCategory.DIAGNOSTIC,
    ),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateFlowConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ClimateFlowSensor(coordinator, d) for d in _DEFS)


class ClimateFlowSensor(
    ClimateFlowEntity, CoordinatorEntity[ClimateFlowCoordinator], SensorEntity
):
    """Senzor derivat din datele coordinatorului."""

    def __init__(
        self, coordinator: ClimateFlowCoordinator, definition: ClimateFlowSensorDef
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ClimateFlowEntity.__init__(self, coordinator)
        self._def = definition
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{definition.key}"
        self._attr_name = definition.name
        self._attr_native_unit_of_measurement = definition.unit
        self._attr_device_class = definition.device_class
        self._attr_state_class = definition.state_class
        self._attr_icon = definition.icon
        self._attr_entity_category = definition.category
        if definition.options is not None:
            self._attr_options = definition.options
        if definition.suggested_precision is not None:
            self._attr_suggested_display_precision = definition.suggested_precision

    @property
    def native_value(self) -> object:
        if self.coordinator.data is None:
            return None
        return self._def.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return self._def.attrs_fn(self.coordinator.data)
