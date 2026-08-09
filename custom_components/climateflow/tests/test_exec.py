"""Teste pentru state machine-ul coordinatorului (cu un `hass` fals).

Acoperă: intrarea în precondiționare, boost-ul de iarnă, trecerea în eveniment
(cu plafonarea ventilatorului + capturarea eșantionului de învățare), cooldown-ul,
oprirea la master off, recuperarea după restart și re-asertarea gărzii.
"""

import asyncio
import datetime as dt
import os
import sys
from types import SimpleNamespace

# Rulabil din rădăcina repo-ului fără instalare.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from custom_components.climateflow import schedule  # noqa: E402
from custom_components.climateflow.const import (  # noqa: E402
    MODE_HEAT_BOILER,
    MODE_HEAT_BOOST,
    SEASON_SUMMER,
    SEASON_WINTER,
    STATE_COOLDOWN,
    STATE_EVENT,
    STATE_IDLE,
    STATE_PRECONDITIONING,
    VAL_ENABLED,
    VAL_FAN_CAP,
    VAL_GUARD,
    VAL_MAX_LEAD,
    VAL_NOTIFY,
    VAL_OVERSHOOT,
    VAL_RATE_COOL,
    VAL_RATE_HEAT,
    VAL_SEASON,
    VAL_TARGET,
    VAL_TOLERANCE,
)
from custom_components.climateflow.coordinator import (  # noqa: E402
    ClimateFlowCoordinator,
    map_fan_mode,
)
from custom_components.climateflow.guard import GuardPolicy  # noqa: E402

UTC = dt.timezone.utc

# Joi 5 nov 2026, 18:00–19:30 (săptămână normală).
EVENTS = [
    {"id": "ev1", "name": "Eveniment", "days": [3], "start": "18:00", "end": "19:30",
     "week_filter": "always", "enabled": True, "target": None},
]


def _at(hh, mm=0):
    return dt.datetime(2026, 11, 5, hh, mm, tzinfo=UTC)


class FakeState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self):
        self._data = {}

    def get(self, entity_id):
        return self._data.get(entity_id)

    def set(self, entity_id, state, attributes=None):
        existing = self._data.get(entity_id)
        attrs = dict(existing.attributes) if existing else {}
        if attributes:
            attrs.update(attributes)
        self._data[entity_id] = FakeState(state, attrs)


class FakeServices:
    """Aplică efectul comenzilor climate pe stările false + înregistrează apelurile."""

    def __init__(self, states):
        self.states = states
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, **kwargs):
        self.calls.append((domain, service, dict(data)))
        if domain != "climate":
            return
        ids = data.get("entity_id", [])
        if isinstance(ids, str):
            ids = [ids]
        for eid in ids:
            if service == "set_hvac_mode":
                self.states.set(eid, data["hvac_mode"])
            elif service == "set_temperature":
                self.states.set(eid, self.states.get(eid).state if self.states.get(eid) else "heat",
                                {"temperature": data["temperature"]})
            elif service == "set_fan_mode":
                self.states.set(eid, self.states.get(eid).state if self.states.get(eid) else "heat",
                                {"fan_mode": data["fan_mode"]})
            elif service == "turn_off":
                self.states.set(eid, "off")

    def of(self, service):
        return [c for c in self.calls if c[1] == service]


class FakeHass:
    def __init__(self):
        self.states = FakeStates()
        self.services = FakeServices(self.states)

    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def make_coordinator(hass, *, season=SEASON_WINTER, indoor=19.0, outdoor=5.0,
                     boiler="climate.boiler", events=EVENTS):
    c = object.__new__(ClimateFlowCoordinator)
    c.hass = hass
    c.entry = SimpleNamespace(
        data={}, entry_id="test", options={"events": events, "one_offs": []}
    )
    c.indoor_sensor = "sensor.indoor"
    c.outdoor_sensor = "sensor.outdoor"
    c.ac_units = ["climate.ac1", "climate.ac2"]
    c.boiler = boiler
    c._cache = {}
    c.values = {
        VAL_ENABLED: True,
        VAL_GUARD: True,
        VAL_NOTIFY: False,
        VAL_SEASON: season,
        VAL_FAN_CAP: "low",
        VAL_TARGET: 22.0,
        VAL_MAX_LEAD: 180.0,
        VAL_TOLERANCE: 0.7,
        VAL_RATE_HEAT: 1.5,
        VAL_RATE_COOL: 2.0,
        VAL_OVERSHOOT: 0.5,
    }
    c.state_name = STATE_IDLE
    c._active = None
    c._commanded = {}
    c._resolved_season = season
    c._manual_occ = None
    c._deficit_since = None
    c._fight_notified = False
    c._skips = set()
    c._rates = {}
    c._samples = []
    c._records = []
    c._total_samples = 0
    c.policy = GuardPolicy()
    c._pending_reasserts = {}
    c._unsub_climate = None
    c._unsub_sensors = None
    c._cooldown_task = None
    # Timeouts scurte pentru teste.
    c._svc_timeout = 1.0
    c._retry_sleep = 0.001
    c._cmd_gap = 0.0
    # Stub-uri pentru infrastructura HA.
    c.notifications = []
    c._notify = lambda title, msg, kind="info": c.notifications.append((kind, title, msg))
    c.recompute = lambda: None

    async def _noop_save():
        return None

    c._save_store = _noop_save
    c._save_learning = _noop_save

    hass.states.set("sensor.indoor", str(indoor))
    hass.states.set("sensor.outdoor", str(outdoor))
    hass.states.set("climate.ac1", "off", {"fan_modes": ["low", "medium", "high", "auto"]})
    hass.states.set("climate.ac2", "off", {"fan_modes": ["quiet", "1", "2", "3"]})
    if boiler:
        hass.states.set(boiler, "off", {})  # centrala nu are fan_modes
    return c


async def tick(c, now):
    """Un tick al coordinatorului, fără partea de Store/build."""
    occs = c._occurrences(now)
    await c._advance(
        now, schedule.current_occurrence(occs, now), schedule.next_occurrence(occs, now)
    )


# ----------------------------------------------------------------- map_fan_mode
def test_map_fan_mode_ladders():
    assert map_fan_mode("low", ["Low", "Medium", "High"]) == "Low"
    assert map_fan_mode("low", ["quiet", "1", "2"]) == "quiet"
    assert map_fan_mode("high", ["quiet", "1", "2"]) == "2"  # fallback: ultima treaptă
    assert map_fan_mode("medium", ["silent", "turbo"]) == "silent"  # fallback: prima
    assert map_fan_mode("auto", ["low", "high"]) is None  # auto inexistent → nu comanda
    assert map_fan_mode("low", None) is None  # fără fan_modes (centrala)


# ------------------------------------------------------------- precondiționare
def test_idle_before_window_precondition_inside():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass)  # indoor 19 → needed 3°C la 1.5°C/h ⇒ ~138 min
        await tick(c, _at(14, 0))
        assert c.state_name == STATE_IDLE

        await tick(c, _at(16, 0))  # 18:00 − 138 min = 15:42 ⇒ suntem în fereastră
        assert c.state_name == STATE_PRECONDITIONING
        assert c._active["plan"]["mode"] == MODE_HEAT_BOILER
        # Doar centrala a fost comandată (heat + supra-țintă 22.5), nu AC-urile.
        hvac_calls = hass.services.of("set_hvac_mode")
        assert [call[2]["entity_id"] for call in hvac_calls] == ["climate.boiler"]
        temp_calls = hass.services.of("set_temperature")
        assert temp_calls[0][2]["temperature"] == 22.5

    asyncio.run(main())


def test_winter_boost_when_boiler_too_slow():
    async def main():
        hass = FakeHass()
        # indoor 15 → needed 7°C; rata necesară în 180 min = 2.33 > 1.5 ⇒ boost din start.
        c = make_coordinator(hass, indoor=15.0)
        await tick(c, _at(15, 30))
        assert c.state_name == STATE_PRECONDITIONING
        assert c._active["plan"]["mode"] == MODE_HEAT_BOOST
        commanded = {call[2]["entity_id"] for call in hass.services.of("set_hvac_mode")}
        assert commanded == {"climate.boiler", "climate.ac1", "climate.ac2"}

    asyncio.run(main())


def test_summer_cools_with_acs_only():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        await tick(c, _at(16, 30))
        assert c.state_name == STATE_PRECONDITIONING
        commanded = {call[2]["entity_id"] for call in hass.services.of("set_hvac_mode")}
        assert commanded == {"climate.ac1", "climate.ac2"}  # centrala neatinsă
        assert all(call[2]["hvac_mode"] == "cool" for call in hass.services.of("set_hvac_mode"))
        # Răcire: supra-țintă în jos (21.5).
        assert hass.services.of("set_temperature")[0][2]["temperature"] == 21.5

    asyncio.run(main())


# ------------------------------------------------------ eveniment + învățare
def test_event_entry_caps_fan_and_learns():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_PRECONDITIONING

        # Sala a ajuns la țintă până la începutul evenimentului.
        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(18, 0))
        assert c.state_name == STATE_EVENT

        # Eșantion capturat: 19→22 în 120 min ⇒ 1.5 °C/h, banda b3 (exterior 5°C).
        assert len(c._samples) == 1
        assert c._total_samples == 1  # contorul cumulativ (TOTAL_INCREASING)
        assert c._samples[0]["rate"] == 1.5
        assert c._rates[MODE_HEAT_BOILER]["b3"] == 1.5
        assert c._records[-1]["on_time"] is True

        # Statisticile agregate pentru panou.
        stats = c.history()["stats"]
        assert stats["total_samples"] == 1
        assert stats["events_30d"] == 1
        assert stats["avg_lead"] == 120.0
        assert stats["boost_count_30d"] == 0
        assert c._build_data()["transition_week"] in (True, False)

        # La eveniment temperatura comandată e ținta exactă (nu supra-ținta).
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 22.0

    asyncio.run(main())


def test_event_fan_capped_on_acs():
    async def main():
        hass = FakeHass()
        # Fără centrală ⇒ iarna încălzesc AC-urile (mod boost) — au fan_modes.
        c = make_coordinator(hass, indoor=19.0, boiler=None)
        await tick(c, _at(16, 0))
        fan_precond = {call[2]["fan_mode"] for call in hass.services.of("set_fan_mode")}
        assert fan_precond == {"high", "3"}  # precondiționare: viteză maximă

        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(18, 0))
        fan_event = [call[2]["fan_mode"] for call in hass.services.of("set_fan_mode")]
        # Ultimele comenzi (evenimentul) sunt pe plafonul „low" mapat per unitate.
        assert fan_event[-2:] == ["low", "quiet"]

    asyncio.run(main())


# ------------------------------------------------------------------- cooldown
def test_event_end_turns_everything_off():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(18, 0))

        await tick(c, _at(19, 30))  # evenimentul s-a încheiat
        assert c.state_name == STATE_COOLDOWN
        await c._cooldown_task
        assert c.state_name == STATE_IDLE
        off_calls = hass.services.of("turn_off")
        assert off_calls and set(off_calls[-1][2]["entity_id"]) == {
            "climate.ac1", "climate.ac2", "climate.boiler"
        }
        assert c._commanded == {}

    asyncio.run(main())


def test_master_off_forces_cooldown():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_PRECONDITIONING

        c.values[VAL_ENABLED] = False
        await tick(c, _at(16, 1))
        assert c.state_name == STATE_COOLDOWN
        await c._cooldown_task
        assert c.state_name == STATE_IDLE
        assert hass.services.of("turn_off")

        # Cu masterul oprit nu se mai comandă nimic.
        n_calls = len(hass.services.calls)
        await tick(c, _at(17, 0))
        assert c.state_name == STATE_IDLE
        assert len(hass.services.calls) == n_calls

    asyncio.run(main())


def test_restart_mid_event_recovers_without_sample():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=21.0)  # coordinator „proaspăt" (restart HA)
        await tick(c, _at(18, 30))  # în plin eveniment
        assert c.state_name == STATE_EVENT
        assert c._samples == []  # fără precondiționare ⇒ fără eșantion
        assert hass.services.of("set_hvac_mode")  # dar actuatoarele sunt comandate

    asyncio.run(main())


# ----------------------------------------------------------------------- gardă
def test_guard_reasserts_only_deviated_attributes():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_PRECONDITIONING

        # Cineva oprește centrala de la telecomandă (temperatura rămâne corectă).
        hass.states.set("climate.boiler", "off")
        before = len(hass.services.of("set_temperature"))
        await c._reconcile(_at(16, 5))
        hvac_calls = hass.services.of("set_hvac_mode")
        assert hvac_calls[-1][2] == {"entity_id": "climate.boiler", "hvac_mode": "heat"}
        # Temperatura NU a fost retrimisă (nu deviase).
        assert len(hass.services.of("set_temperature")) == before

    asyncio.run(main())


def test_guard_rate_limit_backs_off_and_notifies():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))

        # Telecomanda insistă: după 3 re-asertări garda intră în backoff + notifică.
        for i in range(5):
            hass.services.calls.clear()
            hass.states.set("climate.boiler", "off")
            await c._reconcile(_at(16, 10 + i))
        assert not hass.services.of("set_hvac_mode")  # a 4-a/5-a: nimic retrimis
        assert any(kind == "fighting" for kind, _, _ in c.notifications)

    asyncio.run(main())


def test_agenda_includes_skipped_and_run_block():
    async def main():
        from homeassistant.util import dt as dt_util

        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)

        # Următoarea joi reală (agenda pornește de la dt_util.now()).
        now = dt_util.now()
        d = now.date()
        while d.weekday() != 3 or dt.datetime.combine(
            d, dt.time(19, 30), tzinfo=now.tzinfo
        ) <= now:
            d += dt.timedelta(days=1)
        key = f"ev1:{d.isoformat()}"

        # Agenda arată și ocurențele sărite (anotate), pe care _occurrences le exclude.
        c.toggle_skip(key)
        assert key in c._skips
        agenda = c.agenda()
        skipped = [a for a in agenda if a["key"] == key]
        assert skipped and skipped[0]["skipped"] is True
        assert skipped[0]["target"] == 22.0  # ținta implicită completată
        assert all(o["key"] != key for o in c._occurrences(now))

        # Toggle înapoi → reapare ca activă.
        c.toggle_skip(key)
        assert key not in c._skips
        assert any(o["key"] == key for o in c._occurrences(now))

        # Blocul `run` din _build_data: prezent doar în stările gestionate.
        assert c._build_data()["run"] is None
        await tick(c, _at(16, 0))
        run = c._build_data()["run"]
        assert run is not None
        assert run["start_temp"] == 19.0
        assert run["event_start"].startswith("2026-11-05T18:00")

    asyncio.run(main())


def test_guard_disabled_does_not_touch():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        c.values[VAL_GUARD] = False
        hass.states.set("climate.boiler", "off")
        n = len(hass.services.calls)
        await c._reconcile(_at(16, 5))
        assert len(hass.services.calls) == n

    asyncio.run(main())
