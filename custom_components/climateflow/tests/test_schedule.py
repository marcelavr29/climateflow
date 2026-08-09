"""Teste pentru modulul pur de program (regula săptămânii de tranziție + ocurențe)."""

import datetime as dt
import os
import sys

# Rulabil cu `pytest` din rădăcina proiectului fără instalare.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from schedule import (  # noqa: E402
    consume_past_skips,
    current_occurrence,
    expand_occurrences,
    is_month_transition,
    next_occurrence,
    occurrence_key,
    sunday_of_week,
)

UTC = dt.timezone.utc

# Programul real al clădirii.
EVENTS = [
    {"id": "tue", "name": "Marți", "days": [1], "start": "18:00", "end": "20:00",
     "week_filter": "always", "enabled": True},
    {"id": "sun_am", "name": "Duminică dimineața", "days": [6], "start": "09:30",
     "end": "12:00", "week_filter": "always", "enabled": True},
    {"id": "sun_pm", "name": "Duminică seara", "days": [6], "start": "18:00",
     "end": "20:00", "week_filter": "always", "enabled": True},
    {"id": "thu", "name": "Joi", "days": [3], "start": "18:00", "end": "19:30",
     "week_filter": "except_transition", "enabled": True},
    {"id": "fri", "name": "Vineri", "days": [4], "start": "18:00", "end": "20:00",
     "week_filter": "only_transition", "enabled": True},
]


def _at(y, m, d, hh=8, mm=0):
    return dt.datetime(y, m, d, hh, mm, tzinfo=UTC)


# ------------------------------------------------------------- regula de tranziție
def test_sunday_of_week():
    assert sunday_of_week(dt.date(2026, 10, 29)) == dt.date(2026, 11, 1)  # joi
    assert sunday_of_week(dt.date(2026, 11, 1)) == dt.date(2026, 11, 1)  # duminica însăși
    assert sunday_of_week(dt.date(2026, 10, 26)) == dt.date(2026, 11, 1)  # luni


def test_transition_thursday_before_november_sunday():
    # Joi 29 oct 2026 → duminica ei e 1 nov ⇒ săptămână de tranziție.
    assert is_month_transition(dt.date(2026, 10, 29)) is True
    assert is_month_transition(dt.date(2026, 10, 30)) is True  # și vinerea


def test_normal_thursday_not_transition():
    # Joi 5 nov 2026 → duminica ei e 8 nov, aceeași lună.
    assert is_month_transition(dt.date(2026, 11, 5)) is False


def test_sunday_never_transition():
    # Duminica de 1 nov e propria ei duminică — nu e „tranziție”.
    assert is_month_transition(dt.date(2026, 11, 1)) is False


def test_first_of_month_thursday_not_transition():
    # Joi 1 oct 2026 → duminica ei e 4 oct, aceeași lună (regula se uită DOAR
    # la duminica următoare, nu la „săptămâna traversează granița").
    assert is_month_transition(dt.date(2026, 10, 1)) is False


def test_sunday_on_last_day_of_month_not_transition():
    # Joi 28 aug 2025 → duminica ei e 31 aug (ultima zi a lunii) — aceeași lună,
    # chiar dacă lunea următoare e 1 sept.
    assert is_month_transition(dt.date(2025, 8, 28)) is False


def test_year_wrap_transition():
    # Joi 31 dec 2026 → duminica ei e 3 ian 2027 ⇒ tranziție (comparare pe (an, lună)).
    assert is_month_transition(dt.date(2026, 12, 31)) is True


# ------------------------------------------------------------------- expandarea
def test_transition_week_swaps_thursday_for_friday():
    # Săptămâna 26 oct – 1 nov 2026: joia sare, vinerea se ține.
    now = _at(2026, 10, 26)  # luni
    occs = expand_occurrences(EVENTS, [], set(), now, horizon_days=6)
    ids = [(o["event_id"], o["date"].isoformat()) for o in occs]
    assert ("thu", "2026-10-29") not in ids
    assert ("fri", "2026-10-30") in ids
    assert ("tue", "2026-10-27") in ids
    assert ("sun_am", "2026-11-01") in ids
    assert ("sun_pm", "2026-11-01") in ids


def test_normal_week_has_thursday_no_friday():
    now = _at(2026, 11, 2)  # luni, săptămâna 2–8 nov
    occs = expand_occurrences(EVENTS, [], set(), now, horizon_days=6)
    ids = [(o["event_id"], o["date"].isoformat()) for o in occs]
    assert ("thu", "2026-11-05") in ids
    assert ("fri", "2026-11-06") not in ids


def test_occurrences_sorted_and_future_only():
    now = _at(2026, 11, 2)
    occs = expand_occurrences(EVENTS, [], set(), now, horizon_days=13)
    starts = [o["start"] for o in occs]
    assert starts == sorted(starts)
    assert all(o["end"] > now for o in occs)


def test_in_progress_occurrence_included():
    # Marți 3 nov 2026, 18:30 — evenimentul e în desfășurare (restartul HA îl regăsește).
    now = _at(2026, 11, 3, 18, 30)
    occs = expand_occurrences(EVENTS, [], set(), now)
    current = current_occurrence(occs, now)
    assert current is not None and current["event_id"] == "tue"
    upcoming = next_occurrence(occs, now)
    assert upcoming is not None and upcoming["start"] > now


def test_skip_marker_excludes_occurrence():
    now = _at(2026, 11, 2)
    skips = {occurrence_key("thu", dt.date(2026, 11, 5))}
    occs = expand_occurrences(EVENTS, [], skips, now, horizon_days=6)
    ids = [(o["event_id"], o["date"].isoformat()) for o in occs]
    assert ("thu", "2026-11-05") not in ids
    assert ("tue", "2026-11-03") in ids  # doar ocurența țintită e sărită


def test_disabled_event_excluded():
    events = [{**EVENTS[0], "enabled": False}]
    occs = expand_occurrences(events, [], set(), _at(2026, 11, 2))
    assert occs == []


def test_one_off_included_with_target():
    one_offs = [{"id": "wed1", "name": "Nuntă", "date": "2026-11-07",
                 "start": "16:00", "end": "20:00", "target": 23.5}]
    now = _at(2026, 11, 2)
    occs = expand_occurrences(EVENTS, one_offs, set(), now, horizon_days=6)
    match = [o for o in occs if o["event_id"] == "wed1"]
    assert len(match) == 1
    assert match[0]["target"] == 23.5
    assert match[0]["one_off"] is True


def test_one_off_outside_horizon_excluded():
    one_offs = [{"id": "x", "name": "Departe", "date": "2026-12-25",
                 "start": "10:00", "end": "12:00", "target": None}]
    occs = expand_occurrences([], one_offs, set(), _at(2026, 11, 2), horizon_days=6)
    assert occs == []


def test_invalid_times_ignored():
    events = [{"id": "bad", "name": "Rău", "days": [1], "start": "27:00",
               "end": "20:00", "week_filter": "always", "enabled": True},
              {"id": "inv", "name": "Invers", "days": [1], "start": "20:00",
               "end": "18:00", "week_filter": "always", "enabled": True}]
    occs = expand_occurrences(events, [], set(), _at(2026, 11, 2))
    assert occs == []


def test_consume_past_skips():
    now = _at(2026, 11, 4)
    skips = {
        occurrence_key("thu", dt.date(2026, 10, 22)),  # trecut → eliminat
        occurrence_key("thu", dt.date(2026, 11, 5)),  # viitor → păstrat
        "gunoaie-fără-dată",  # invalid → eliminat
    }
    kept = consume_past_skips(skips, now)
    assert kept == {occurrence_key("thu", dt.date(2026, 11, 5))}
