"""Teste pentru modulul pur de învățare termică (benzi + EMA + avans)."""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from thermal import (  # noqa: E402
    EMA_ALPHA,
    band_key,
    effective_rate,
    lead_minutes,
    observed_rate,
    required_rate,
    update_rate,
)


# ---------------------------------------------------------------------- benzi
def test_band_edges_heat():
    assert band_key(-10.0, "heat_boiler") == "b0"
    assert band_key(-5.0, "heat_boiler") == "b1"  # capătul de jos e inclusiv
    assert band_key(-0.1, "heat_boiler") == "b1"
    assert band_key(0.0, "heat_boiler") == "b2"
    assert band_key(9.9, "heat_boiler") == "b3"
    assert band_key(25.0, "heat_boiler") == "b4"


def test_band_edges_cool():
    assert band_key(20.0, "cool") == "c0"
    assert band_key(25.0, "cool") == "c1"
    assert band_key(34.9, "cool") == "c2"
    assert band_key(40.0, "cool") == "c3"


# --------------------------------------------------------------- observed_rate
def test_observed_rate_basic():
    # 15.1 → 20.0 în 96 min ⇒ ~3.06 °C/h
    assert observed_rate(15.1, 20.0, 96) == pytest.approx(3.06, abs=0.01)


def test_observed_rate_cooling_uses_absolute_delta():
    assert observed_rate(28.0, 24.0, 120) == pytest.approx(2.0)


def test_observed_rate_rejects_short_run():
    assert observed_rate(15.0, 20.0, 10) is None


def test_observed_rate_rejects_tiny_delta():
    assert observed_rate(20.0, 20.2, 60) is None


def test_observed_rate_rejects_absurd_rate():
    # 12 °C într-o oră = glitch de senzor, nu fizică.
    assert observed_rate(10.0, 22.0, 60) is None


def test_observed_rate_rejects_missing_temps():
    assert observed_rate(None, 20.0, 60) is None
    assert observed_rate(15.0, None, 60) is None


# ------------------------------------------------------------------------ EMA
def test_update_rate_seeds_first_sample():
    rates = update_rate({}, "heat_boiler", "b1", 2.0)
    assert rates["heat_boiler"]["b1"] == pytest.approx(2.0)


def test_update_rate_converges_monotonically():
    rates = {"heat_boiler": {"b1": 2.0}}
    prev = 2.0
    for _ in range(5):
        rates = update_rate(rates, "heat_boiler", "b1", 1.0)
        val = rates["heat_boiler"]["b1"]
        assert 1.0 < val < prev  # se apropie de 1.0 la fiecare pas
        prev = val
    # primul pas: 2.0 + 0.3·(1.0 − 2.0) = 1.7
    assert update_rate({"h": {"b": 2.0}}, "h", "b", 1.0, EMA_ALPHA)["h"]["b"] == pytest.approx(1.7)


def test_update_rate_does_not_mutate_input():
    rates = {"heat_boiler": {"b1": 2.0}}
    update_rate(rates, "heat_boiler", "b1", 1.0)
    assert rates["heat_boiler"]["b1"] == 2.0


# -------------------------------------------------------------- effective_rate
def test_effective_rate_exact_band():
    rates = {"heat_boiler": {"b1": 1.2}}
    assert effective_rate(rates, "heat_boiler", "b1", 9.9) == pytest.approx(1.2)


def test_effective_rate_neighbor_fallback():
    # b1 lipsește → cea mai apropiată bandă cu date (b2).
    rates = {"heat_boiler": {"b2": 1.8, "b4": 3.0}}
    assert effective_rate(rates, "heat_boiler", "b1", 9.9) == pytest.approx(1.8)


def test_effective_rate_default_when_empty():
    assert effective_rate({}, "heat_boiler", "b1", 1.5) == pytest.approx(1.5)
    assert effective_rate({"cool": {"c1": 2.0}}, "heat_boiler", "b1", 1.5) == pytest.approx(1.5)


# ---------------------------------------------------------------- lead_minutes
def test_lead_zero_when_already_at_target():
    assert lead_minutes(0.0, 1.5, 180) == 0.0
    assert lead_minutes(-2.0, 1.5, 180) == 0.0


def test_lead_max_when_rate_unknown():
    assert lead_minutes(5.0, 0.0, 180) == 180.0


def test_lead_normal_with_margin():
    # 3 °C la 1.5 °C/h × marja 1.15 ⇒ ceil(138) = 138 min
    assert lead_minutes(3.0, 1.5, 180) == pytest.approx(138.0)


def test_lead_clamped_to_max():
    # Val de frig: 8 °C la 1 °C/h ⇒ 552 min → plafonat la 180.
    assert lead_minutes(8.0, 1.0, 180) == 180.0


def test_lead_clamped_to_min():
    assert lead_minutes(0.05, 10.0, 180) == 10.0


# --------------------------------------------------------------- required_rate
def test_required_rate():
    assert required_rate(4.0, 120.0) == pytest.approx(2.0)
    assert required_rate(0.0, 120.0) == 0.0
    assert required_rate(-1.0, 120.0) == 0.0
    assert math.isinf(required_rate(4.0, 0.0))  # fără timp ⇒ imposibil
