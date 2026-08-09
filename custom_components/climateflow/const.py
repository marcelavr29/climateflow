"""Constante pentru integrarea ClimateFlow."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "climateflow"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.BUTTON,
]

# --- Setări generale (entry.data) ---------------------------------------------
CONF_INDOOR_SENSOR = "indoor_sensor"  # senzor temperatură interioară
CONF_OUTDOOR_SENSOR = "outdoor_sensor"  # senzor temperatură exterioară
CONF_AC_UNITS = "ac_units"  # entități climate — aerele condiționate
CONF_BOILER = "boiler"  # entitate climate — centrala termică (opțional)
CONF_NOTIFY_SERVICE = "notify_service"  # opțional: serviciu notify.* pentru push

# --- Programul evenimentelor (entry.options) -----------------------------------
# entry.options[CONF_EVENTS] = evenimente recurente săptămânal:
#   {id, name, days: [0-6, luni=0], start "HH:MM", end "HH:MM",
#    target: °C|None (None → number.default_target), week_filter, enabled}
# entry.options[CONF_ONE_OFFS] = evenimente unice (nunți etc.):
#   {id, name, date "YYYY-MM-DD", start, end, target}
CONF_EVENTS = "events"
CONF_ONE_OFFS = "one_offs"

CONF_ID = "id"
CONF_NAME = "name"
CONF_DAYS = "days"
CONF_START = "start"
CONF_END = "end"
CONF_TARGET = "target"
CONF_WEEK_FILTER = "week_filter"
CONF_ENABLED = "enabled"
CONF_DATE = "date"

# Filtrul de săptămână: regula „duminica pică pe luna următoare".
WF_ALWAYS = "always"
WF_ONLY_TRANSITION = "only_transition"  # DOAR în săptămâna de tranziție de lună
WF_EXCEPT_TRANSITION = "except_transition"  # EXCEPTÂND săptămâna de tranziție

# Programul preinstalat al clădirii — sistemul funcționează din prima, fără
# configurare manuală. Editabil ulterior din panoul ClimateFlow.
DEFAULT_EVENTS: list[dict] = [
    {"id": "tue_pm", "name": "Marți seara", "days": [1], "start": "18:00",
     "end": "20:00", "target": None, "week_filter": WF_ALWAYS, "enabled": True},
    {"id": "sun_am", "name": "Duminică dimineața", "days": [6], "start": "09:30",
     "end": "12:00", "target": None, "week_filter": WF_ALWAYS, "enabled": True},
    {"id": "sun_pm", "name": "Duminică seara", "days": [6], "start": "18:00",
     "end": "20:00", "target": None, "week_filter": WF_ALWAYS, "enabled": True},
    {"id": "thu_pm", "name": "Joi seara", "days": [3], "start": "18:00",
     "end": "19:30", "target": None, "week_filter": WF_EXCEPT_TRANSITION, "enabled": True},
    {"id": "fri_pm", "name": "Vineri seara", "days": [4], "start": "18:00",
     "end": "20:00", "target": None, "week_filter": WF_ONLY_TRANSITION, "enabled": True},
]

# --- Chei valori reglabile live (coordinator.values) --------------------------
VAL_ENABLED = "enabled"  # master: sistemul comandă / nu comandă nimic
VAL_GUARD = "guard"  # garda anti-modificare (telecomenzi)
VAL_ROOM_CONTROL = "room_control"  # termostat pe senzorul de sală (setpoint extrem pe AC-uri)
VAL_NOTIFY = "notify"  # notificări HA
VAL_SEASON = "season"  # vară / iarnă / auto
VAL_FAN_CAP = "fan_cap"  # treapta maximă de ventilator în timpul evenimentului
VAL_TARGET = "default_target"  # ținta implicită de temperatură (°C)
VAL_MAX_LEAD = "max_lead"  # avans maxim de pornire (min)
VAL_TOLERANCE = "tolerance"  # toleranță confort (°C)
VAL_RATE_HEAT = "default_rate_heat"  # rată implicită încălzire (°C/h)
VAL_RATE_COOL = "default_rate_cool"  # rată implicită răcire (°C/h)
VAL_OVERSHOOT = "overshoot"  # supra-țintă la precondiționare (°C)
VAL_EXTREME_COOL = "extreme_cool"  # setpoint extrem răcire (termostat pe sală)
VAL_EXTREME_HEAT = "extreme_heat"  # setpoint extrem încălzire

DEFAULT_EXTREME_COOL_C = 12.0
DEFAULT_EXTREME_HEAT_C = 30.0
DEFAULT_TARGET_C = 22.0
DEFAULT_MAX_LEAD_MIN = 180.0
DEFAULT_TOLERANCE_C = 0.7
DEFAULT_RATE_HEAT = 1.5
DEFAULT_RATE_COOL = 2.0
DEFAULT_OVERSHOOT_C = 0.5

# --- Sezoane / moduri ----------------------------------------------------------
SEASON_SUMMER = "summer"
SEASON_WINTER = "winter"
SEASON_AUTO = "auto"
SEASON_OPTIONS = [SEASON_SUMMER, SEASON_WINTER, SEASON_AUTO]

# Praguri pentru sezonul automat (histerezis pe temperatura exterioară,
# evaluat la începutul precondiționării, nu continuu).
AUTO_WINTER_BELOW_C = 15.0
AUTO_SUMMER_ABOVE_C = 22.0

# Moduri de funcționare — cheile sub care se învață ratele termice.
MODE_COOL = "cool"
MODE_HEAT_BOILER = "heat_boiler"
MODE_HEAT_BOOST = "heat_boiler_boost"

# --- Plafonul de ventilator ----------------------------------------------------
FAN_CAP_OPTIONS = ["quiet", "low", "medium", "high", "auto"]
DEFAULT_FAN_CAP = "low"

# --- Stările sistemului --------------------------------------------------------
STATE_IDLE = "idle"
STATE_PRECONDITIONING = "preconditioning"
STATE_EVENT = "event"
STATE_COOLDOWN = "cooldown"

# --- Servicii ------------------------------------------------------------------
SERVICE_SKIP_NEXT = "skip_next"
SERVICE_ADD_ONE_OFF = "add_one_off"
SERVICE_BOOST_NOW = "boost_now"
SERVICE_STOP = "stop"
