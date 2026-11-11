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
    VAL_BOILER_SETBACK,
    VAL_BOILER_SETBACK_ON,
    VAL_ENABLED,
    VAL_EXTREME_COOL,
    VAL_EXTREME_HEAT,
    VAL_FAN_CAP,
    VAL_GUARD,
    VAL_MAX_LEAD,
    VAL_NOTIFY,
    VAL_OVERSHOOT,
    VAL_RATE_COOL,
    VAL_RATE_HEAT,
    VAL_ROOM_CONTROL,
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
        self.fail = set()  # servicii climate care „pică" (timeout de integrare)

    async def async_call(self, domain, service, data, blocking=False, **kwargs):
        self.calls.append((domain, service, dict(data)))
        if domain != "climate":
            return
        if service in self.fail:
            raise RuntimeError("integrare căzută")
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
        VAL_ROOM_CONTROL: False,  # off implicit în teste; pornit explicit unde se testează
        VAL_SEASON: season,
        VAL_FAN_CAP: "low",
        VAL_TARGET: 22.0,
        VAL_MAX_LEAD: 180.0,
        VAL_TOLERANCE: 0.7,
        VAL_RATE_HEAT: 1.5,
        VAL_RATE_COOL: 2.0,
        VAL_OVERSHOOT: 0.5,
        VAL_EXTREME_COOL: 12.0,
        VAL_EXTREME_HEAT: 30.0,
        # Oprit implicit în teste (comportamentul clasic: centrala pe off la final);
        # pornit explicit unde se testează temperatura de așteptare.
        VAL_BOILER_SETBACK_ON: False,
        VAL_BOILER_SETBACK: 18.0,
    }
    c.state_name = STATE_IDLE
    c._active = None
    c._commanded = {}
    c._resolved_season = season
    c._manual_occ = None
    c._force_key = None
    c._stopped = set()
    c._failed = set()
    c._gen = 0
    c._dead = False
    c._cmd_lock = asyncio.Lock()
    c._tick_lock = asyncio.Lock()
    c._orphan_check = False
    c._persisted = None
    c._recommand = False
    c._deficit_since = None
    c._fight_notified = False
    c._skips = set()
    c._rates = {}
    c._lags = {}
    c._await_verify = set()
    c._orphan_entities = []
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
    c._request_tick = lambda: None
    # Ceasul gărzii urmează timpul FALS al testelor (altfel rezultatul depinde de
    # data reală la care rulează suita).
    c._test_ts = 0.0
    c._ts = lambda: c._test_ts

    async def _noop_save():
        return None

    c._save_store = _noop_save
    c._save_learning = _noop_save

    hass.states.set("sensor.indoor", str(indoor))
    hass.states.set("sensor.outdoor", str(outdoor))
    hass.states.set("climate.ac1", "off",
                    {"fan_modes": ["low", "medium", "high", "auto"], "min_temp": 12, "max_temp": 30})
    hass.states.set("climate.ac2", "off",
                    {"fan_modes": ["quiet", "1", "2", "3"], "min_temp": 12, "max_temp": 30})
    if boiler:
        hass.states.set(boiler, "off", {})  # centrala nu are fan_modes
    return c


async def tick(c, now):
    """Un tick al coordinatorului, fără partea de Store/build."""
    c._test_ts = now.timestamp()
    occs = c._occurrences(now)
    await c._advance(now, c._pick_current(occs, now), schedule.next_occurrence(occs, now))


# ----------------------------------------------------------------- map_fan_mode
def test_map_fan_mode_ladders():
    assert map_fan_mode("low", ["Low", "Medium", "High"]) == "Low"
    assert map_fan_mode("low", ["quiet", "1", "2"]) == "quiet"
    assert map_fan_mode("high", ["quiet", "1", "2"]) == "2"  # fallback: ultima treaptă
    assert map_fan_mode("medium", ["silent", "turbo"]) == "silent"  # fallback: prima
    assert map_fan_mode("auto", ["low", "high"]) is None  # auto inexistent → nu comanda
    assert map_fan_mode("low", None) is None  # fără fan_modes (centrala)
    # Pe unități cu trepte 1–5, „maxim" e 5 — nu 4 (bug raportat pe teren).
    assert map_fan_mode("high", ["1", "2", "3", "4", "5"]) == "5"
    assert map_fan_mode("high", ["1", "2", "3", "4"]) == "4"


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


# ----------------------------------------------- termostat pe senzorul de sală
def test_room_control_extreme_setpoint_and_hysteresis():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True

        # Precondiționare: AC-urile primesc minimul unității (12°), NU ținta —
        # altfel termostatul lor intern se saturează de la propriul aer rece.
        await tick(c, _at(16, 30))
        assert c.state_name == STATE_PRECONDITIONING
        temps = {call[2]["entity_id"]: call[2]["temperature"]
                 for call in hass.services.of("set_temperature")}
        assert temps == {"climate.ac1": 12.0, "climate.ac2": 12.0}
        assert c._active["acs_on"] is True

        # Sala (senzorul real) a coborât sub țintă − toleranță → AC-urile se opresc.
        hass.states.set("sensor.indoor", "20.5")  # țintă precond 21.5, tol 0.7
        await tick(c, _at(16, 40))
        assert hass.services.of("turn_off")
        assert c._commanded["climate.ac1"] == {"hvac_mode": "off"}
        assert c._active["acs_on"] is False

        # În eveniment sala urcă peste țintă + toleranță → repornesc, tot pe 12°.
        hass.states.set("sensor.indoor", "23.0")  # țintă 22, tol 0.7
        await tick(c, _at(18, 5))
        assert c.state_name == STATE_EVENT
        assert c._active["acs_on"] is True
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 12.0

        # Se satisface din nou în timpul evenimentului → se opresc iar.
        hass.states.set("sensor.indoor", "21.2")  # sub 22 − 0.7
        await tick(c, _at(18, 10))
        assert c._active["acs_on"] is False
        assert c._commanded["climate.ac2"] == {"hvac_mode": "off"}

    asyncio.run(main())


def test_extreme_setpoint_configured_not_unit_attrs():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        # Unitate care NU publică min_temp/max_temp → valoarea configurată (12°).
        hass.states.set("climate.ac1", "off", {"fan_modes": ["low"]})
        hass.states._data["climate.ac1"].attributes.pop("min_temp", None)
        assert c._extreme_setpoint("climate.ac1", "cool") == 12.0
        # Unitate cu minim mai SUS decât configuratul → limitat la minimul unității
        # (o comandă sub minim ar fi respinsă de integrare).
        hass.states.set("climate.ac2", "off", {"min_temp": 16})
        assert c._extreme_setpoint("climate.ac2", "cool") == 16.0
        # Încălzire: limitat la maximul unității.
        assert c._extreme_setpoint("climate.ac2", "heat") == 30.0
        hass.states.set("climate.ac2", "off", {"max_temp": 28})
        assert c._extreme_setpoint("climate.ac2", "heat") == 28.0

    asyncio.run(main())


def test_recommand_after_reload_mid_run():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(18, 5))
        assert c.state_name == STATE_EVENT

        # Simulăm un reload/update: comenzile vechi există, dar flag-ul cere retrimitere.
        hass.services.calls.clear()
        c._recommand = True
        await tick(c, _at(18, 6))
        # Comenzile au fost retrimise cu setările NOI (setpoint extrem 12°).
        assert hass.services.of("set_hvac_mode")
        assert hass.services.of("set_temperature")[0][2]["temperature"] == 12.0
        assert c._recommand is False

        # Fără flag, tick-urile normale NU retrimit comenzi complete.
        hass.services.calls.clear()
        await tick(c, _at(18, 7))
        assert not hass.services.of("set_hvac_mode")

    asyncio.run(main())


def test_room_control_off_keeps_target_setpoint():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        assert c.values[VAL_ROOM_CONTROL] is False
        await tick(c, _at(16, 30))
        # Comportamentul clasic: setpoint = țintă − supra-țintă (21.5).
        assert hass.services.of("set_temperature")[0][2]["temperature"] == 21.5

    asyncio.run(main())


def test_room_control_leaves_boiler_on_target():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)  # iarnă, boiler-only
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(16, 0))
        # Centrala are termostat de cameră real → primește ținta normală (22.5).
        assert hass.services.of("set_temperature")[0][2] == {
            "entity_id": "climate.boiler", "temperature": 22.5
        }
        # Și histerezisul nu o atinge: sala „prea caldă" nu oprește centrala.
        hass.states.set("sensor.indoor", "23.5")
        n_off = len(hass.services.of("turn_off"))
        await tick(c, _at(16, 5))
        assert len(hass.services.of("turn_off")) == n_off

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
        # Se închide DOAR ce a comandat rularea (aici: centrala; AC-urile n-au fost atinse).
        off_calls = hass.services.of("turn_off")
        assert off_calls and off_calls[-1][2]["entity_id"] == ["climate.boiler"]
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


# =============================================================== regresii v0.6.0
class frozen_now:
    """Îngheață `dt_util.now()` pentru acțiunile care citesc ceasul real."""

    def __init__(self, now):
        self.now = now

    def __enter__(self):
        from homeassistant.util import dt as dt_util

        self._mod, self._orig = dt_util, dt_util.now
        dt_util.now = lambda *a, **k: self.now

    def __exit__(self, *exc):
        self._mod.now = self._orig


def _wire_refresh(c, now):
    async def _refresh():
        await tick(c, now)

    c.async_refresh = _refresh


KEY = "ev1:2026-11-05"


def test_stop_stays_stopped_until_resumed():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_PRECONDITIONING

        await c.async_stop()
        await c._cooldown_task
        assert c.state_name == STATE_IDLE and KEY in c._stopped

        # Înainte: următorul tick repornea totul. Acum rămâne oprit — inclusiv evenimentul.
        n = len(hass.services.calls)
        await tick(c, _at(16, 1))
        await tick(c, _at(18, 5))
        assert c.state_name == STATE_IDLE
        assert len(hass.services.calls) == n

        # Reactivare din agendă (același comutator ca la „sărit").
        c.toggle_skip(KEY)
        assert KEY not in c._stopped and KEY not in c._skips
        await tick(c, _at(18, 6))
        assert c.state_name == STATE_EVENT

    asyncio.run(main())


def test_boost_now_far_event_is_manual_run_near_event_preconditions():
    async def main():
        # Eveniment peste 2 zile → NU precondiționare de zile; rulare manuală de 60 min.
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        far = dt.datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
        _wire_refresh(c, far)
        with frozen_now(far):
            await c.async_boost_now()
        assert c.state_name == STATE_EVENT
        assert c._active["manual"] is True
        assert c._active["event_end"] == far + dt.timedelta(minutes=60)
        await tick(c, far + dt.timedelta(minutes=61))
        assert c.state_name == STATE_COOLDOWN
        await c._cooldown_task

        # Eveniment în mai puțin de „Avans maxim" → precondiționare spre el, chiar dacă
        # avansul calculat nu a sosit încă (22° deja → avans 0).
        hass = FakeHass()
        c = make_coordinator(hass, indoor=22.0)
        near = _at(16, 30)
        await tick(c, near)
        assert c.state_name == STATE_IDLE
        _wire_refresh(c, near)
        with frozen_now(near):
            await c.async_boost_now()
        assert c.state_name == STATE_PRECONDITIONING
        assert c._active["key"] == KEY

    asyncio.run(main())


def test_stop_during_command_sequence_leaves_everything_off():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=15.0)  # boost din start: centrală + 2 AC-uri
        c._cmd_gap = 0.05
        run = asyncio.get_running_loop().create_task(tick(c, _at(15, 30)))
        await asyncio.sleep(0.02)  # în mijlocul primei unități
        await c.async_stop()
        await run
        if c._cooldown_task is not None:
            await c._cooldown_task
        assert c.state_name == STATE_IDLE
        for eid in ("climate.boiler", "climate.ac1", "climate.ac2"):
            assert hass.states.get(eid).state == "off", eid
        # Ultima comandă trimisă e all-off, nu o pornire rătăcită.
        assert hass.services.calls[-1][1] == "turn_off"

    asyncio.run(main())


def test_room_sensor_lost_falls_back_to_normal_setpoint():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(18, 1))
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 12.0

        hass.states.set("sensor.indoor", "unavailable")
        await tick(c, _at(18, 2))  # o scăpare scurtă nu schimbă nimic
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 12.0
        await tick(c, _at(18, 6))  # > 3 min fără senzor → țintă normală
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 22.0
        assert c._active["sensor_fallback"] is True
        assert any(kind == "sensor" for kind, _, _ in c.notifications)

        hass.states.set("sensor.indoor", "25.0")
        await tick(c, _at(18, 7))  # senzorul revine → înapoi pe setpoint extrem
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 12.0
        assert c._active["sensor_fallback"] is False

    asyncio.run(main())


def test_failed_turn_off_is_retried_even_with_guard_off():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        c.values[VAL_GUARD] = False
        c.policy = GuardPolicy(grace_s=0.0)
        await tick(c, _at(18, 1))

        hass.services.fail = {"turn_off", "set_hvac_mode"}
        hass.states.set("sensor.indoor", "21.0")  # satisfăcut → histerezisul oprește
        await tick(c, _at(18, 2))
        assert hass.states.get("climate.ac1").state == "cool"  # oprirea a eșuat
        assert c._commanded["climate.ac1"] == {"hvac_mode": "off"}
        assert "climate.ac1" in c._failed

        hass.services.fail = set()
        await tick(c, _at(18, 3))  # reconcilierea reîncearcă, deși garda e oprită
        assert hass.states.get("climate.ac1").state == "off"
        assert hass.states.get("climate.ac2").state == "off"
        assert not c._failed

    asyncio.run(main())


def test_skip_next_toggles_same_occurrence():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass)
        with frozen_now(_at(12, 0)):
            assert c._build_data()["skip_next_armed"] is False
            c.skip_next()
            assert c._skips == {KEY}
            assert c._build_data()["skip_next_armed"] is True
            c.skip_next()  # a doua apăsare ANULEAZĂ, nu sare și evenimentul de după
            assert c._skips == set()
            assert c._build_data()["skip_next_armed"] is False

    asyncio.run(main())


def test_live_setting_change_recommands_current_run():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=21.0, boiler=None)  # AC-urile încălzesc
        await tick(c, _at(18, 5))
        assert c.state_name == STATE_EVENT
        assert hass.services.of("set_fan_mode")[-2][2]["fan_mode"] == "low"

        c.set_value(VAL_FAN_CAP, "medium")
        c.set_value(VAL_TARGET, 23.0)
        assert c._recommand is True
        await tick(c, _at(18, 6))
        assert hass.services.of("set_fan_mode")[-2][2]["fan_mode"] == "medium"
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 23.0

        # Restaurarea de la pornire (valoare veche inexistentă) NU e o schimbare.
        c._recommand = False
        c._active["tainted"] = False
        del c.values[VAL_GUARD]
        c.set_value(VAL_GUARD, True)
        assert c._recommand is False and not c._active.get("tainted")

    asyncio.run(main())


def test_learning_measures_time_to_target_not_whole_window():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(16, 0))
        # Ținta e atinsă după 60 min (rată reală 3 °C/h), apoi sala doar stă la țintă.
        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(17, 0))
        hass.states.set("sensor.indoor", "22.4")
        await tick(c, _at(18, 0))
        assert c.state_name == STATE_EVENT
        # Înainte: 3.4° / 120 min = 1.7 °C/h (diluat de timpul de staționare).
        assert c._samples[-1]["rate"] == 3.0
        assert c._records[-1]["reached_min"] == 60.0

    asyncio.run(main())


def test_escalation_teaches_boiler_rate_so_next_lead_is_longer():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        c.values[VAL_MAX_LEAD] = 360.0  # loc ca avansul să crească în loc de boost din start
        lead_before = c._plan(c._occurrences(_at(14, 0))[0], _at(14, 0))["lead"]
        await tick(c, _at(15, 45))
        assert c._active["plan"]["mode"] == MODE_HEAT_BOILER

        # Inerția primelor minute NU escaladează (înainte: boost după câteva minute).
        await tick(c, _at(15, 50))
        assert c._active["escalated"] is False

        # Prima mișcare a sălii abia după 20 min → timpul mort al centralei.
        hass.states.set("sensor.indoor", "19.2")
        await tick(c, _at(16, 5))
        assert c._active["lag_at"] == _at(16, 5)

        # După 45 min centrala a urcat doar 0.3° → escaladare + rata ei reală e învățată.
        hass.states.set("sensor.indoor", "19.3")
        await tick(c, _at(16, 30))
        assert c._active["escalated"] is True
        assert c._active["plan"]["mode"] == MODE_HEAT_BOOST
        learned = c._rates[MODE_HEAT_BOILER]["b3"]
        # De la prima mișcare: 0.1° în 25 min = 0.24 °C/h, amestecat (EMA 30%) cu rata
        # planificată 1.5 → 1.12. Inerția (20 min) se învață separat.
        assert round(learned, 2) == 1.12
        assert c._samples[-1]["rate"] == 0.24
        assert c._lags[MODE_HEAT_BOILER] == 20.0

        # Restul rulării (boost) se învață separat, pe modul boost.
        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(18, 0))
        assert c._samples[-1]["mode"] == MODE_HEAT_BOOST
        assert c._samples[-1]["rate"] == 1.8  # 2.7° în 90 min
        assert MODE_HEAT_BOOST not in c._lags  # inerția boost-ului nu se deduce de aici
        assert c._rates[MODE_HEAT_BOILER]["b3"] == learned  # neatins de porțiunea de boost

        # Data viitoare, aceeași situație → avans mai mare (pornește mai devreme).
        await tick(c, _at(19, 30))
        await c._cooldown_task
        hass.states.set("sensor.indoor", "19.0")
        nxt = _at(14, 0) + dt.timedelta(days=7)
        assert c._plan(c._occurrences(nxt)[0], nxt)["lead"] > lead_before

    asyncio.run(main())


def test_hysteresis_restart_at_half_tolerance_and_no_blind_restart_at_event():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(16, 30))
        hass.states.set("sensor.indoor", "20.5")
        await tick(c, _at(16, 40))
        assert c._active["acs_on"] is False

        # Începutul evenimentului cu sala sub țintă: AC-urile NU repornesc orbește.
        hass.services.calls.clear()
        hass.states.set("sensor.indoor", "21.8")
        await tick(c, _at(18, 0))
        assert c.state_name == STATE_EVENT
        assert c._active["acs_on"] is False
        assert not hass.services.of("set_hvac_mode")

        # Repornire la țintă + toleranță/2 (22.35), sub pragul de deficit (22.7).
        hass.states.set("sensor.indoor", "22.4")
        await tick(c, _at(18, 1))
        assert c._active["acs_on"] is True
        assert c._deficit_since is None

    asyncio.run(main())


def test_auto_season_in_ambiguous_band_follows_need():
    async def main():
        hass = FakeHass()
        # Toamnă: ultima dată a fost „vară", afară 18° (între praguri), sala rece.
        c = make_coordinator(hass, season="auto", indoor=20.0, outdoor=18.0)
        c._resolved_season = SEASON_SUMMER
        plan = c._plan(c._occurrences(_at(12, 0))[0], _at(12, 0))
        assert plan["season"] == SEASON_WINTER and plan["mode"] == MODE_HEAT_BOILER
        # În afara zonei ambigue decide exteriorul, ca înainte.
        hass.states.set("sensor.outdoor", "30.0")
        assert c._plan(c._occurrences(_at(12, 0))[0], _at(12, 0))["season"] == SEASON_SUMMER

    asyncio.run(main())


def test_cached_cooldown_state_does_not_stick_and_startup_waits():
    async def main():
        hass = FakeHass()
        hass.data = {"climateflow": {"cache_test": {"state": STATE_COOLDOWN}}}
        hass.is_running = False
        c = make_coordinator(hass, indoor=19.0)
        # HA încă pornește → nu intrăm într-o rulare pe senzori indisponibili.
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_IDLE and not hass.services.calls
        hass.is_running = True
        await tick(c, _at(16, 0))
        assert c.state_name == STATE_PRECONDITIONING

    asyncio.run(main())


def test_window_error_rejects_silent_invalid_events():
    assert schedule.window_error("18:00", "20:00") is None
    assert schedule.window_error("20:00", "18:00")  # sfârșit înaintea începutului
    assert schedule.window_error("22:00", "01:00")  # peste miezul nopții
    assert schedule.window_error("18", "20:00")
    assert schedule.window_error("18:00", "20:00", "2026-13-40")
    assert schedule.window_error("18:00", "20:00", "2026-08-22") is None


# =============================================================== v0.7.0
def test_boiler_goes_to_setback_instead_of_off():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=15.0)  # boost: centrală + AC-uri
        c.values[VAL_BOILER_SETBACK_ON] = True
        c.values[VAL_BOILER_SETBACK] = 18.5
        await tick(c, _at(15, 30))
        hass.states.set("sensor.indoor", "22.0")
        await tick(c, _at(18, 0))
        hass.services.calls.clear()

        await tick(c, _at(19, 30))  # finalul evenimentului
        await c._cooldown_task
        assert c.state_name == STATE_IDLE
        # AC-urile se sting; centrala NU — coboară pe temperatura de așteptare.
        assert set(hass.services.of("turn_off")[-1][2]["entity_id"]) == {
            "climate.ac1", "climate.ac2"
        }
        boiler = hass.states.get("climate.boiler")
        assert boiler.state == "heat" and boiler.attributes["temperature"] == 18.5

    asyncio.run(main())


def test_stop_in_winter_also_uses_setback_and_summer_leaves_boiler_alone():
    async def main():
        # „Oprește tot" iarna: aerele off (toate), centrala pe așteptare.
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        c.values[VAL_BOILER_SETBACK_ON] = True
        await tick(c, _at(16, 0))
        await c.async_stop()
        await c._cooldown_task
        boiler = hass.states.get("climate.boiler")
        assert boiler.state == "heat" and boiler.attributes["temperature"] == 18.0
        assert set(hass.services.of("turn_off")[-1][2]["entity_id"]) == {
            "climate.ac1", "climate.ac2"
        }

        # Vara centrala n-a fost comandată → finalul evenimentului nu o atinge deloc.
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_BOILER_SETBACK_ON] = True
        hass.states.set("climate.boiler", "heat", {"temperature": 45.0})  # ex. apă caldă
        await tick(c, _at(18, 5))
        await tick(c, _at(19, 30))
        await c._cooldown_task
        boiler = hass.states.get("climate.boiler")
        assert boiler.state == "heat" and boiler.attributes["temperature"] == 45.0
        assert all(
            call[2]["entity_id"] != "climate.boiler"
            and "climate.boiler" not in call[2]["entity_id"]
            for call in hass.services.calls
        )

    asyncio.run(main())


def test_schedule_edit_mid_run_is_followed_without_recommanding():
    async def main():
        hass = FakeHass()
        events = [dict(EVENTS[0])]
        c = make_coordinator(hass, indoor=21.0, events=events)
        await tick(c, _at(18, 5))
        assert c.state_name == STATE_EVENT

        # Salvare din panou fără reload: ora de final mutată → nicio comandă nouă.
        hass.services.calls.clear()
        events[0]["end"] = "20:00"
        await tick(c, _at(18, 6))
        assert c._active["event_end"] == _at(20, 0)
        assert not hass.services.of("set_hvac_mode")

        # Ținta evenimentului schimbată → se retrimite DOAR atunci.
        events[0]["target"] = 23.0
        await tick(c, _at(18, 7))
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 23.0

    asyncio.run(main())


def test_frozen_room_sensor_counts_as_lost():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(18, 1))
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 12.0

        # Senzorul „arată" 27°, dar n-a mai raportat nimic de 3 ore (baterie moartă).
        hass.states.get("sensor.indoor").last_reported = _at(15, 0)
        await tick(c, _at(18, 2))
        await tick(c, _at(18, 6))
        assert c._active["sensor_fallback"] is True
        assert hass.services.of("set_temperature")[-1][2]["temperature"] == 22.0

    asyncio.run(main())


def test_unit_clamping_setpoint_is_adopted_not_fought():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, season=SEASON_SUMMER, indoor=27.0, outdoor=32.0)
        c.values[VAL_ROOM_CONTROL] = True
        await tick(c, _at(18, 1))
        # Unitatea acceptă comanda de 12°, dar se oprește la minimul ei real: 16°.
        hass.states.set("climate.ac1", "cool", {"temperature": 16.0})

        hass.services.calls.clear()
        for minute in range(3, 12):
            await tick(c, _at(18, minute))
        assert c._commanded["climate.ac1"]["temperature"] == 16.0
        assert not hass.services.of("set_temperature")  # nicio re-asertare
        assert not any(kind == "fighting" for kind, _, _ in c.notifications)

        # O telecomandă care schimbă DUPĂ aceea e în continuare corectată.
        hass.states.set("climate.ac1", "cool", {"temperature": 24.0})
        await tick(c, _at(18, 13))
        assert hass.services.of("set_temperature")[-1][2] == {
            "entity_id": "climate.ac1", "temperature": 16.0
        }

    asyncio.run(main())


def test_learned_lag_extends_lead_and_delays_escalation():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        occ = c._occurrences(_at(12, 0))[0]
        assert c._plan(occ, _at(12, 0))["lead"] == 138.0
        c._lags = {MODE_HEAT_BOILER: 25.0}
        assert c._plan(occ, _at(12, 0))["lead"] == 163.0  # 25 min inerție + 138

        # Cu inerția cunoscută, lipsa de progres din primele 20 min NU escaladează.
        await tick(c, _at(15, 17))
        assert c.state_name == STATE_PRECONDITIONING
        await tick(c, _at(15, 37))
        assert c._active["escalated"] is False

    asyncio.run(main())


def test_escalation_uses_observed_rate_when_building_is_faster_than_planned():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        c._rates = {MODE_HEAT_BOILER: {"b3": 1.5}}
        await tick(c, _at(15, 42))
        hass.states.set("sensor.indoor", "19.2")
        await tick(c, _at(16, 10))  # prima mișcare (inerție lungă: 28 min)
        # După rata PLANIFICATĂ ar fi în urmă (2.0° / 1.5 = 80 min > 74 rămase)…
        hass.states.set("sensor.indoor", "20.0")
        await tick(c, _at(16, 46))
        # …dar sala urcă acum cu 1.33 °C/h… tot insuficient → escaladează corect.
        assert c._active["escalated"] is True

        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        await tick(c, _at(15, 42))
        hass.states.set("sensor.indoor", "19.2")
        await tick(c, _at(16, 10))
        hass.states.set("sensor.indoor", "20.6")  # 2.33 °C/h observat
        await tick(c, _at(16, 46))
        # 1.4° rămase la 2.33 °C/h = 36 min < 74 → NU escaladează, deși la rata
        # planificată (1.5) ar fi părut la limită.
        assert c._active["escalated"] is False

    asyncio.run(main())


def test_slow_run_without_backup_still_teaches_rate():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        c.ac_units = []  # doar centrală: nu există boost care să „salveze" rularea
        await tick(c, _at(15, 42))
        # Sala abia s-a mișcat (0.2° în 138 min), iar senzorul e viu (a raportat).
        hass.states.set("sensor.indoor", "19.2")
        hass.states.get("sensor.indoor").last_reported = _at(17, 50)
        await tick(c, _at(18, 0))
        assert c._records[-1]["on_time"] is False
        # Înainte: eșantion aruncat (Δ < 0.3°) → aceeași pornire târzie data viitoare.
        assert c._rates[MODE_HEAT_BOILER]["b3"] < 1.5
        await tick(c, _at(19, 30))
        await c._cooldown_task
        nxt = _at(12, 0) + dt.timedelta(days=7)
        hass.states.set("sensor.indoor", "19.0")
        assert c._plan(c._occurrences(nxt)[0], nxt)["lead"] > 138.0

        # Senzor care NU a raportat nimic în rulare → nu învățăm din el.
        hass = FakeHass()
        c = make_coordinator(hass, indoor=19.0)
        c.ac_units = []
        await tick(c, _at(15, 42))
        hass.states.get("sensor.indoor").last_reported = _at(12, 0)
        await tick(c, _at(18, 0))
        assert c._rates == {}

    asyncio.run(main())


def test_repeated_start_boost_suggests_raising_max_lead():
    async def main():
        hass = FakeHass()
        c = make_coordinator(hass, indoor=15.0)  # 7° de recuperat: boost din start
        with frozen_now(_at(20, 0)):
            for day in (0, 7, 14):
                start = _at(15, 30) + dt.timedelta(days=day)
                hass.states.set("sensor.indoor", "15.0")
                with frozen_now(start):
                    await tick(c, start)
                    assert c._active["plan"]["mode"] == MODE_HEAT_BOOST
                    await c.async_stop()
                    await c._cooldown_task
                c._stopped.clear()
            last = _at(20, 0) + dt.timedelta(days=14)
        with frozen_now(last):
            sug = c.history()["suggestion"]
            assert sug["count"] == 3 and sug["max_lead"] == 180.0
            assert sug["needed_min"] == 322  # 7° / 1.5 °C/h × 1.15
            assert any(kind == "max_lead" for kind, _, _ in c.notifications)
            # Avansul maxim crescut destul → sugestia dispare.
            c.values[VAL_MAX_LEAD] = 330.0
            assert c.history()["suggestion"] is None

    asyncio.run(main())
