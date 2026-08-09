"""Înregistrarea panoului ClimateFlow în bara laterală (frontend custom)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "climateflow"  # /climateflow în bara laterală
_STATIC_URL = "/climateflow_frontend/climateflow-panel.js"
_REGISTERED = "panel_registered"


def _js_hash(js_path: Path) -> str:
    """Cache-busting automat: `?v=` derivat din conținutul JS-ului.

    URL-ul modulului se schimbă exact când se schimbă fișierul → browserul reîncarcă
    singur după update, fără hard refresh (lecția release-urilor ZoneFlow 0.9–0.10).
    """
    try:
        return hashlib.md5(js_path.read_bytes()).hexdigest()[:10]
    except OSError:
        return "0"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Servește JS-ul și adaugă panoul în sidebar — o singură dată per HA."""
    store = hass.data.setdefault(DOMAIN, {})
    if store.get(_REGISTERED):
        return

    js_path = Path(__file__).parent / "frontend" / "climateflow-panel.js"
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_STATIC_URL, str(js_path), False)]
        )
    except (RuntimeError, ValueError) as err:
        # Calea poate fi deja înregistrată (ex. la un reload în aceeași sesiune).
        _LOGGER.debug("Calea statică ClimateFlow deja înregistrată: %s", err)

    version = await hass.async_add_executor_job(_js_hash, js_path)
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="climateflow-panel",
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{_STATIC_URL}?v={version}",
        sidebar_title="ClimateFlow",
        sidebar_icon="mdi:sun-thermometer-outline",
        require_admin=False,
        config={},
    )
    store[_REGISTERED] = True
    _LOGGER.debug("Panou ClimateFlow înregistrat (v=%s)", version)


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Scoate panoul (doar la ștergerea ultimei intrări)."""
    store = hass.data.get(DOMAIN, {})
    if store.get(_REGISTERED):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
        store[_REGISTERED] = False
