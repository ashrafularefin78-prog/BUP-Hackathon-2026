"""Judge-side acceptance tests.

Two independent gates are applied to every public sample case:

1. STRICT JSON-SCHEMA VALIDATION — the raw response JSON is validated against
   schemas/response.schema.json (Draft 2020-12, closed objects, per-type
   structured_adjustment shapes), as a judge would check a submitted contract.

2. INDEPENDENT REPLAY — the returned hourly_plan is replayed by a FRESH
   validator instance against ORGANIZER GROUND-TRUTH directives (parsed
   independently from the pack below, not from the service's own reported
   interpretations), mirroring the Participant Guide rule that "the judge
   independently replays the plan using the true hidden directive, not only
   the team-reported interpretation."

3. NEGATIVE CONTROLS — deliberately broken responses must fail the schema,
   proving the contract is not vacuously satisfied.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app.replay import replay_plan
from app.schemas import BatteryInput, HourInput

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "schemas" / "response.schema.json"

TOL = 0.01

# ---------------------------------------------------------------------------
# Organizer ground truth, parsed independently of app.guardrails / app.llm.
# (directive_type, hours, numeric value or None)
# ---------------------------------------------------------------------------

GROUND_TRUTH = {
    "SAMPLE-01": [
        ("solar_reduction", [12, 13], 0.25),
        ("no_op", None, None),
    ],
    "SAMPLE-02": [
        ("no_charge_window", [2, 3, 4], None),
    ],
    "SAMPLE-03": [
        ("minimum_battery_reserve", [18, 19, 20], 100.0),
    ],
    "SAMPLE-04": [
        ("no_discharge_window", [18, 19], None),
    ],
    "SAMPLE-05": [
        ("max_grid_window", [18, 19, 20], 155.0),
    ],
    "SAMPLE-06": [
        ("solar_reduction", [10, 11], 0.5),
        ("no_charge_window", [14, 15], None),
        ("no_op", None, None),
    ],
    "SAMPLE-07": [
        ("minimum_battery_reserve", [18, 19, 20, 21], 90.0),
        ("max_grid_window", [19, 20], 180.0),
    ],
    "SAMPLE-08": [
        ("no_charge_window", [11, 12], None),
        ("no_discharge_window", [17, 18], None),
    ],
    "SAMPLE-09": [
        ("solar_reduction", [11, 12, 13], 0.2),
        ("no_op", None, None),
    ],
    "SAMPLE-10": [
        ("minimum_battery_reserve", [18, 19, 20, 21], 80.0),
        ("max_grid_window", [19, 20, 21], 190.0),
        ("no_op", None, None),
    ],
}

_VALUE_KEY = {
    "solar_reduction": "factor",
    "minimum_battery_reserve": "minimum_energy_kwh",
    "max_grid_window": "max_grid_kwh",
}


def ground_truth_directives(case: dict) -> list[dict]:
    """Compile the pack's expected interpretation into replay-ready directive dicts."""
    directives: list[dict] = []
    for directive_type, hours, value in GROUND_TRUTH[case["id"]]:
        if directive_type == "no_op":
            continue
        adjustment: dict = {"hours": hours}
        if value is not None:
            adjustment[_VALUE_KEY[directive_type]] = value
        directives.append(
            {"applies": True, "directive_type": directive_type, "structured_adjustment": adjustment}
        )
    return directives


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def validator(schema) -> jsonschema.protocols.Validator:
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


@pytest.fixture(scope="module")
def responses_by_scenario(client, sample_cases) -> dict[str, dict]:
    """POST each sample once; reuse the raw JSON bodies across all gates."""
    out: dict[str, dict] = {}
    for case in sample_cases:
        resp = client.post("/optimize-energy", json=case["input"])
        assert resp.status_code == 200, f"{case['id']}: HTTP {resp.status_code}: {resp.text[:200]}"
        out[case["id"]] = resp.json()
    return out


# ---------------------------------------------------------------------------
# Gate 1: strict JSON-schema validation of every response
# ---------------------------------------------------------------------------


def test_response_schema_matches_pack_schema_notes(sample_pack):
    """The contract file must mirror the pack's own schema_notes requirements."""
    notes = sample_pack["_meta"]["schema_notes"]
    required_top = set(notes["output_required_fields"])
    required_entry = set(notes["directive_interpretation_required_fields"])
    required_hour = set(notes["hourly_plan_required_fields"])
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert required_top <= set(schema["required"])
    assert required_entry <= set(schema["$defs"]["directiveInterpretation"]["required"])
    assert required_hour <= set(schema["$defs"]["hourlyPlanEntry"]["required"])
    assert set(schema["properties"]) == required_top  # closed: no extra top-level fields


def test_every_public_response_passes_strict_schema(validator, sample_cases, responses_by_scenario):
    for case in sample_cases:
        body = responses_by_scenario[case["id"]]
        # Raises with a full report on any violation.
        jsonschema.validate(instance=body, schema=validator.schema)


def test_hourly_plan_hours_are_exactly_0_to_23(responses_by_scenario):
    for scenario_id, body in responses_by_scenario.items():
        hours = [entry["hour"] for entry in body["hourly_plan"]]
        assert hours == list(range(24)), f"{scenario_id}: hourly_plan hours {hours}"


def test_interpretation_entries_are_ordered_and_complete(sample_cases, responses_by_scenario):
    for case in sample_cases:
        entries = responses_by_scenario[case["id"]]["directive_interpretation"]
        assert [e["note_index"] for e in entries] == list(range(len(entries))), case["id"]


# ---------------------------------------------------------------------------
# Gate 2: independent replay against organizer ground truth
# ---------------------------------------------------------------------------


def test_every_plan_replays_valid_under_ground_truth_directives(sample_cases, responses_by_scenario):
    failures: list[str] = []
    for case in sample_cases:
        scenario_id = case["id"]
        body = responses_by_scenario[scenario_id]

        hours = [HourInput(**h) for h in case["input"]["hours"]]
        battery = BatteryInput(**case["input"]["battery"])

        valid, violations = replay_plan(
            hours,
            battery,
            ground_truth_directives(case),
            body["hourly_plan"],
            body["total_grid_kwh"],
            body["total_cost_bdt"],
            body["peak_grid_kwh"],
        )
        if not valid:
            failures.append(f"{scenario_id}: {violations}")

    assert not failures, "\n".join(failures)


def test_replayed_cost_matches_reference_optimum(sample_cases, responses_by_scenario):
    failures: list[str] = []
    for case in sample_cases:
        scenario_id = case["id"]
        body = responses_by_scenario[scenario_id]
        reference_cost = case["expected_output"]["total_cost_bdt"]
        if abs(body["total_cost_bdt"] - reference_cost) > TOL:
            failures.append(
                f"{scenario_id}: replayed cost {body['total_cost_bdt']} != optimum {reference_cost}"
            )
    assert not failures, "\n".join(failures)


def test_ground_truth_solar_is_actually_applied(sample_cases, responses_by_scenario):
    """Directly verify solar_reduction ground truth against the plan (11.2 check)."""
    for case in sample_cases:
        directives = ground_truth_directives(case)
        reductions = [d for d in directives if d["directive_type"] == "solar_reduction"]
        if not reductions:
            continue
        body = responses_by_scenario[case["id"]]
        by_hour = {e["hour"]: e for e in body["hourly_plan"]}
        for d in reductions:
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                base = next(x for x in case["input"]["hours"] if x["hour"] == h)
                cap = base["solar_kwh"] * adj["factor"]
                used = by_hour[h]["solar_used_kwh"]
                assert used <= cap + TOL, (
                    f"{case['id']} hour {h}: solar_used {used} > effective {cap}"
                )


# ---------------------------------------------------------------------------
# Gate 3: negative controls — the schema must reject broken responses
# ---------------------------------------------------------------------------


def _first_non_noop_entry(sample_cases, responses_by_scenario) -> tuple[str, dict]:
    for case in sample_cases:
        for entry in responses_by_scenario[case["id"]]["directive_interpretation"]:
            if entry["directive_type"] != "no_op":
                return case["id"], entry
    raise AssertionError("no non-no_op interpretation found in pack responses")


def test_negative_extra_top_level_field_rejected(validator, sample_cases, responses_by_scenario):
    body = json.loads(json.dumps(responses_by_scenario[sample_cases[0]["id"]]))
    body["unexpected_bonus"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_wrong_adjustment_shape_rejected(validator, sample_cases, responses_by_scenario):
    scenario_id, entry = _first_non_noop_entry(sample_cases, responses_by_scenario)
    body = json.loads(json.dumps(responses_by_scenario[scenario_id]))
    target = next(
        e for e in body["directive_interpretation"] if e["note_index"] == entry["note_index"]
    )
    target["structured_adjustment"]["bogus_field"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_no_op_with_adjustment_rejected(validator, sample_cases, responses_by_scenario):
    scenario_id = next(
        cid for cid, body in responses_by_scenario.items()
        if any(e["directive_type"] == "no_op" for e in body["directive_interpretation"])
    )
    body = json.loads(json.dumps(responses_by_scenario[scenario_id]))
    target = next(
        e for e in body["directive_interpretation"] if e["directive_type"] == "no_op"
    )
    target["structured_adjustment"] = {"hours": [1]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_out_of_range_hour_rejected(validator, sample_cases, responses_by_scenario):
    body = json.loads(json.dumps(responses_by_scenario[sample_cases[0]["id"]]))
    body["hourly_plan"][0]["hour"] = 24
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_negative_grid_value_rejected(validator, sample_cases, responses_by_scenario):
    body = json.loads(json.dumps(responses_by_scenario[sample_cases[0]["id"]]))
    body["hourly_plan"][0]["grid_kwh"] = -5.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_bad_battery_action_rejected(validator, sample_cases, responses_by_scenario):
    body = json.loads(json.dumps(responses_by_scenario[sample_cases[0]["id"]]))
    body["hourly_plan"][0]["battery_action"] = "teleport"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)


def test_negative_missing_required_field_rejected(validator, sample_cases, responses_by_scenario):
    body = json.loads(json.dumps(responses_by_scenario[sample_cases[0]["id"]]))
    del body["total_cost_bdt"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=validator.schema)
