"""Adversarial note-generation suite — regression hook.

Scores all 50 hidden-style operator notes (tests/fixtures/adversarial_notes.json)
through the full interpretation path (interpreter + production guardrails) and
asserts exact type / hours / value ground truth. Runs against the configured
provider; the offline run exercises the deterministic mock.

The same fixture is scored over live HTTP by scripts/run_adversarial_suite.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import get_settings
from app.guardrails import validate_and_repair
from app.llm import build_interpreter

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "adversarial_notes.json").read_text(encoding="utf-8")
)
CAPACITY = 200.0

VALUE_KEY = {
    "solar_reduction": "factor",
    "minimum_battery_reserve": "minimum_energy_kwh",
    "max_grid_window": "max_grid_kwh",
}


def _interpret(note: str) -> dict:
    raw = build_interpreter(get_settings()).interpret([note], CAPACITY)
    entries, _ = validate_and_repair(raw, 1)
    assert entries is not None
    return entries[0]


def _normalize_wrap(hours: list[int]) -> list[int]:
    """Rotate a midnight-crossing window so its minimum comes first.

    The guardrails force ascending hours, so a note like '10 PM until 2 AM'
    surfaces as [0, 22, 23]; the fixture's [22, 23, 0] denotes the same set.
    """
    i = hours.index(min(hours))
    return hours[i:] + hours[:i]


@pytest.mark.parametrize(
    "item", FIXTURE["notes"], ids=lambda i: i["id"]
)
def test_adversarial_note_matches_ground_truth(item):
    entry = _interpret(item["note"])
    expected = item["expected"]

    if expected is None:
        assert entry["directive_type"] == "no_op"
        assert entry["applies"] is False
        assert entry["structured_adjustment"] is None
        return

    assert entry["applies"] is True
    assert entry["directive_type"] == expected["directive_type"], item["id"]
    adj = entry["structured_adjustment"]
    assert adj["hours"] == _normalize_wrap(expected["hours"]), item["id"]

    key = VALUE_KEY.get(expected["directive_type"])
    if key is not None:
        assert adj[key] == pytest.approx(expected[key], abs=1e-3), item["id"]
