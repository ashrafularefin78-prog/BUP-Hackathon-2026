"""Paraphrase-variant tests for the interpretation layer.

These pressure-test semantic (not keyword) extraction: the same directive
reworded with different units, synonyms, and time formats must resolve to the
same structured result. They run against the offline interpreter; when a hosted
provider is configured they are skipped (the judge evaluates hidden paraphrases
directly against the live service).
"""

from __future__ import annotations

import os

import pytest

from app.config import get_settings
from app.guardrails import validate_and_repair
from app.llm import mock_interpret

pytestmark = pytest.mark.skipif(
    os.getenv("LLM_PROVIDER", "mock") != "mock",
    reason="paraphrase suite targets the offline interpreter; hosted providers are judged live",
)


def interpret(note: str, capacity: float = 200.0) -> dict:
    entries, _ = validate_and_repair(mock_interpret([note], capacity), 1)
    assert entries is not None
    return entries[0]


@pytest.mark.parametrize(
    "note,expected",
    [
        # solar_reduction, factor = fraction REMAINING
        ("Solar output will drop to about 20% from 1 PM to 3 PM.",
         ("solar_reduction", [13, 14], 0.2)),
        ("PV production will drop to about 20% between 13:00 and 15:00.",
         ("solar_reduction", [13, 14], 0.2)),
        ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.",
         ("solar_reduction", [13, 14], 0.2)),
        ("Panel washing from one until three will leave roughly one-fifth of normal solar output.",
         ("solar_reduction", [13, 14], 0.2)),
        ("Cloud cover will leave about half of the forecast solar output from 10 AM until noon.",
         ("solar_reduction", [10, 11], 0.5)),
        # no_charge_window
        ("Do not charge the battery between 2 PM and 4 PM.",
         ("no_charge_window", [14, 15], None)),
        ("The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance.",
         ("no_charge_window", [2, 3, 4], None)),
        ("The charging circuit will be unavailable from 2 PM until 4 PM.",
         ("no_charge_window", [14, 15], None)),
        # no_discharge_window
        ("For protection testing, the battery must not discharge from 6 PM until 8 PM.",
         ("no_discharge_window", [18, 19], None)),
        ("Do not discharge the battery from 5 PM until 7 PM during relay testing.",
         ("no_discharge_window", [17, 18], None)),
        # minimum_battery_reserve: absolute and %-of-capacity
        ("Keep at least 120 kWh in reserve from 6 PM until 9 PM.",
         ("minimum_battery_reserve", [18, 19, 20], 120.0)),
        ("Keep at least 50% of the battery capacity stored from 6 PM until 9 PM for emergencies.",
         ("minimum_battery_reserve", [18, 19, 20], 100.0)),  # 50% of 200 kWh
        # max_grid_window
        ("From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour.",
         ("max_grid_window", [18, 19, 20], 155.0)),
        ("The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM.",
         ("max_grid_window", [19, 20], 180.0)),
        # no_op distractors
        ("The cafeteria menu changes tomorrow.", ("no_op", None, None)),
        ("The library is extending book-return hours next week.", ("no_op", None, None)),
        ("The sports office moved next month's registration deadline.", ("no_op", None, None)),
    ],
)
def test_paraphrases_map_to_same_directive(note, expected):
    entry = interpret(note)
    etype, ehours, eval_ = expected
    assert entry["directive_type"] == etype
    if etype == "no_op":
        assert entry["applies"] is False
        assert entry["structured_adjustment"] is None
        return
    assert entry["applies"] is True
    assert entry["structured_adjustment"]["hours"] == ehours
    if eval_ is not None:
        key = {"solar_reduction": "factor",
               "minimum_battery_reserve": "minimum_energy_kwh",
               "max_grid_window": "max_grid_kwh"}[etype]
        assert entry["structured_adjustment"][key] == pytest.approx(eval_, abs=1e-6)


def test_ambiguous_note_is_safe_no_op():
    entry = interpret("The weather is nice today.")
    assert entry["directive_type"] == "no_op"


def test_battery_capacity_conversion_uses_scenario_capacity():
    entry = interpret("Keep at least 50% of the battery capacity from 6 PM until 9 PM.",
                      capacity=240.0)
    assert entry["directive_type"] == "minimum_battery_reserve"
    assert entry["structured_adjustment"]["minimum_energy_kwh"] == pytest.approx(120.0)
