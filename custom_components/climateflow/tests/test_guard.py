"""Teste pentru politica gărzii anti-modificare (deviere, grace, rate-limit, backoff)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from guard import GuardPolicy, deviations  # noqa: E402


CMD = {"hvac_mode": "heat", "temperature": 22.5, "fan_mode": "low"}


# ------------------------------------------------------------------- deviations
def test_no_deviation_when_matching():
    actual = {"hvac_mode": "heat", "temperature": 22.5, "fan_mode": "low"}
    assert deviations(CMD, actual) == {}


def test_temperature_within_tolerance_ok():
    actual = {"hvac_mode": "heat", "temperature": 22.0, "fan_mode": "low"}
    assert deviations(CMD, actual) == {}  # 0.5 ≤ toleranța


def test_temperature_beyond_tolerance_flagged():
    actual = {"hvac_mode": "heat", "temperature": 25.0, "fan_mode": "low"}
    assert deviations(CMD, actual) == {"temperature": 22.5}


def test_hvac_and_fan_deviations():
    actual = {"hvac_mode": "off", "temperature": 22.5, "fan_mode": "high"}
    assert deviations(CMD, actual) == {"hvac_mode": "heat", "fan_mode": "low"}


def test_unavailable_is_not_deviation():
    actual = {"hvac_mode": "unavailable", "temperature": None, "fan_mode": None}
    assert deviations(CMD, actual) == {}


def test_uncommanded_attributes_not_compared():
    # Centrala fără fan: comanda nu are fan_mode → fanul real e ignorat.
    cmd = {"hvac_mode": "heat", "temperature": 22.5}
    actual = {"hvac_mode": "heat", "temperature": 22.5, "fan_mode": "whatever"}
    assert deviations(cmd, actual) == {}


# ------------------------------------------------------------------ GuardPolicy
def test_grace_after_own_command():
    p = GuardPolicy(grace_s=45)
    p.note_command("climate.a", 100.0)
    assert p.should_check("climate.a", 130.0) is False  # în grace (30s < 45s)
    assert p.should_check("climate.a", 150.0) is True  # grace expirat
    assert p.should_check("climate.b", 100.0) is True  # altă entitate — neafectată


def test_rate_limit_then_backoff():
    p = GuardPolicy(max_reasserts=3, window_s=300, backoff_s=300)
    assert p.note_reassert("climate.a", 10.0) is True
    assert p.note_reassert("climate.a", 20.0) is True
    assert p.note_reassert("climate.a", 30.0) is True
    # a 4-a în aceeași fereastră → refuzată + backoff
    assert p.note_reassert("climate.a", 40.0) is False
    assert p.in_backoff("climate.a", 100.0) is True
    assert p.is_fighting("climate.a", 100.0) is True
    assert p.should_check("climate.a", 100.0) is False
    # după backoff (40 + 300) garda reia
    assert p.in_backoff("climate.a", 341.0) is False
    assert p.should_check("climate.a", 341.0) is True


def test_rate_limit_window_slides():
    p = GuardPolicy(max_reasserts=3, window_s=300, backoff_s=300)
    p.note_reassert("climate.a", 10.0)
    p.note_reassert("climate.a", 20.0)
    p.note_reassert("climate.a", 30.0)
    # peste 300s, fereastra veche a expirat — re-asertarea e permisă din nou
    assert p.note_reassert("climate.a", 400.0) is True


def test_reset_clears_history():
    p = GuardPolicy(max_reasserts=1, window_s=300, backoff_s=300)
    p.note_reassert("climate.a", 10.0)
    assert p.note_reassert("climate.a", 20.0) is False  # backoff
    p.reset()
    assert p.should_check("climate.a", 21.0) is True
    assert p.note_reassert("climate.a", 22.0) is True
