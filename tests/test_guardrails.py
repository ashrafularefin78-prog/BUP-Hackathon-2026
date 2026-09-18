"""Guardrail unit tests: every rejection path, salvage, and safe demotion."""

from __future__ import annotations

from app import guardrails


def valid_entry(**overrides):
    entry = {
        "note_index": 0,
        "applies": True,
        "directive_type": "no_charge_window",
        "structured_adjustment": {"hours": [14, 15]},
        "explanation": "Charging blocked.",
    }
    entry.update(overrides)
    return entry


def test_valid_entry_passes():
    entries, reasons = guardrails.validate_and_repair([valid_entry()], 1)
    assert entries is not None
    assert entries[0]["directive_type"] == "no_charge_window"
    assert entries[0]["applies"] is True
    assert reasons == []


def test_all_directive_types_pass():
    cases = [
        {"directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [13, 14], "factor": 0.2}},
        {"directive_type": "minimum_battery_reserve",
         "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100}},
        {"directive_type": "no_charge_window", "structured_adjustment": {"hours": [2, 3, 4]}},
        {"directive_type": "no_discharge_window", "structured_adjustment": {"hours": [18, 19]}},
        {"directive_type": "max_grid_window",
         "structured_adjustment": {"hours": [19, 20], "max_grid_kwh": 155}},
    ]
    for i, override in enumerate(cases):
        entries, _ = guardrails.validate_and_repair([valid_entry(**override)], 1)
        assert entries is not None
        assert entries[0]["structured_adjustment"] == override["structured_adjustment"]


def test_rejects_unsupported_directive_type():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="ban_ice_cream")], 1
    )
    assert entries[0]["directive_type"] == "no_op"
    assert entries[0]["applies"] is False
    assert entries[0]["structured_adjustment"] is None


def test_no_op_requires_false_applies_and_null_adjustment():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="no_op", structured_adjustment=None, applies=False)], 1
    )
    assert entries[0]["directive_type"] == "no_op"

    # applies=true with no_op is demoted
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="no_op", structured_adjustment=None, applies=True)], 1
    )
    assert entries[0]["directive_type"] == "no_op"

    # no_op with a non-null adjustment is demoted
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="no_op", structured_adjustment={"hours": [1]})], 1
    )
    assert entries[0]["directive_type"] == "no_op"


def test_rejects_non_noop_with_applies_false():
    entries, _ = guardrails.validate_and_repair([valid_entry(applies=False)], 1)
    assert entries[0]["directive_type"] == "no_op"


def test_rejects_out_of_range_hours():
    for hours in ([24], [-1], [5, 5], [0.5], ["3"], []):
        entries, _ = guardrails.validate_and_repair(
            [valid_entry(structured_adjustment={"hours": hours})], 1
        )
        assert entries[0]["directive_type"] == "no_op", f"hours={hours} should be rejected"


def test_unsorted_hours_are_salvaged_not_demoted():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(structured_adjustment={"hours": [7, 3]})], 1
    )
    assert entries[0]["directive_type"] == "no_charge_window"
    assert entries[0]["structured_adjustment"]["hours"] == [3, 7]


def test_rejects_factor_out_of_range():
    for factor in (1.5, -0.1):
        entries, _ = guardrails.validate_and_repair(
            [valid_entry(directive_type="solar_reduction",
                         structured_adjustment={"hours": [12], "factor": factor})], 1
        )
        assert entries[0]["directive_type"] == "no_op"


def test_rejects_negative_or_nonfinite_values():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="minimum_battery_reserve",
                     structured_adjustment={"hours": [18], "minimum_energy_kwh": -5})], 1
    )
    assert entries[0]["directive_type"] == "no_op"

    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="max_grid_window",
                     structured_adjustment={"hours": [18], "max_grid_kwh": "abc"})], 1
    )
    assert entries[0]["directive_type"] == "no_op"


def test_rejects_wrong_adjustment_keys():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(structured_adjustment={"hours": [14], "minutes": 30})], 1
    )
    assert entries[0]["directive_type"] == "no_op"


def test_out_of_range_note_index_is_corrected_positionally():
    """A bad LLM index label never leaks through: salvage forces the positional index."""
    entries, _ = guardrails.validate_and_repair([valid_entry(note_index=5)], 1)
    assert entries[0]["note_index"] == 0
    assert entries[0]["directive_type"] == "no_charge_window"

    # Duplicate labels are also not a permutation -> positional mapping.
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(note_index=0), valid_entry(note_index=0,
         structured_adjustment={"hours": [2, 3, 4]})], 2
    )
    assert [e["note_index"] for e in entries] == [0, 1]
    assert entries[0]["structured_adjustment"] == {"hours": [14, 15]}
    assert entries[1]["structured_adjustment"] == {"hours": [2, 3, 4]}


def test_arity_mismatch_maps_positionally_and_demotes_missing():
    """Too few entries: map positionally, demote the gap — never discard the list."""
    entries, reasons = guardrails.validate_and_repair([valid_entry()], 2)
    assert entries is not None
    assert entries[0]["directive_type"] == "no_charge_window"
    assert entries[1]["directive_type"] == "no_op"
    assert reasons  # demotion is logged/visible


def test_salvages_numeric_string_factor():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(directive_type="solar_reduction",
                     structured_adjustment={"hours": [12, 13], "factor": "0.25"})], 1
    )
    assert entries is not None
    assert entries[0]["directive_type"] == "solar_reduction"
    assert entries[0]["structured_adjustment"]["factor"] == 0.25


def test_salvages_unsorted_hours():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(structured_adjustment={"hours": [14, 12]})], 1
    )
    assert entries is not None
    assert entries[0]["structured_adjustment"]["hours"] == [12, 14]


def test_missing_note_is_demoted_to_no_op():
    entries, _ = guardrails.validate_and_repair([valid_entry(note_index=0)], 2)
    assert entries is not None
    assert entries[0]["directive_type"] == "no_charge_window"
    assert entries[1]["directive_type"] == "no_op"


def test_entries_normalized_in_note_index_order():
    entries, _ = guardrails.validate_and_repair(
        [valid_entry(note_index=0), valid_entry(note_index=1)], 2
    )
    assert entries is not None
    assert [e["note_index"] for e in entries] == [0, 1]


def test_make_no_op_shape():
    entry = guardrails.make_no_op(2)
    assert entry == {
        "note_index": 2,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": entry["explanation"],
    }
