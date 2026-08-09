"""Config flow ClimateFlow.

Setup-ul inițial și options flow-ul gestionează DOAR setările generale (senzori +
actuatoare). Evenimentele se configurează din panoul „ClimateFlow” (v0.3) sau prin
websocket `climateflow/save_events` / serviciul `climateflow.add_one_off`.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AC_UNITS,
    CONF_BOILER,
    CONF_EVENTS,
    CONF_INDOOR_SENSOR,
    CONF_NOTIFY_SERVICE,
    CONF_ONE_OFFS,
    CONF_OUTDOOR_SENSOR,
    DEFAULT_EVENTS,
    DOMAIN,
)


def _general_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_INDOOR_SENSOR, default=defaults.get(CONF_INDOOR_SENSOR)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(
                CONF_OUTDOOR_SENSOR, default=defaults.get(CONF_OUTDOOR_SENSOR)
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            vol.Required(
                CONF_AC_UNITS, default=defaults.get(CONF_AC_UNITS, [])
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="climate", multiple=True)
            ),
            vol.Optional(
                CONF_BOILER,
                description={"suggested_value": defaults.get(CONF_BOILER)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
            vol.Optional(
                CONF_NOTIFY_SERVICE,
                description={"suggested_value": defaults.get(CONF_NOTIFY_SERVICE)},
            ): selector.TextSelector(),
        }
    )


class ClimateFlowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup inițial: doar setări generale. Evenimentele se adaugă ulterior."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Programul vine preinstalat — sistemul e funcțional imediat după setup.
            return self.async_create_entry(
                title="ClimateFlow",
                data=user_input,
                options={CONF_EVENTS: list(DEFAULT_EVENTS), CONF_ONE_OFFS: []},
            )
        return self.async_show_form(step_id="user", data_schema=_general_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ClimateFlowOptionsFlow()


class ClimateFlowOptionsFlow(OptionsFlow):
    """Doar setări generale; evenimentele se editează separat."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **user_input}
            )
            return self.async_create_entry(title="", data=self.config_entry.options)
        return self.async_show_form(
            step_id="init", data_schema=_general_schema(dict(self.config_entry.data))
        )
