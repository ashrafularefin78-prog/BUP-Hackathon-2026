"""Request-side strict JSON-Schema contract tests.

Mirrors the response-contract approach in test_judge_acceptance.py:

1. STRICT SCHEMA GATE — every public sample request validates against
   schemas/request.schema.json (Draft 2020-12, closed objects).

2. EQUIVALENCE — the schema and the service's Pydantic ingress
   (app.schemas.OptimizeRequest) accept/reject the same structural shapes:
   24 unique hours 0-23 (pigeonhole `contains` constraints in the schema),
   non-empty notes, numeric domains, battery invariants. Non-finite values
   (NaN/Infinity — not valid JSON per RFC 8259) are layered: the schema's
   numeric bounds reject nan and -inf, and the service ingress rejects all
   non-finite floats (including +inf, which no bound catches) at HTTP 400.

3. NEGATIVE CONTROLS — deliberately broken requests must fail the schema,
   proving the contract is not vacuously satisfied.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

from app.schemas import OptimizeRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_SCHEMA_PATH = REPO_ROOT / "schemas" / "request.schema.json"

VALID_REQUEST = {
    "scenario_id": "CONTRACT-PROBE",
    "operator_notes": ["Solar output will drop to about 20% from 1 PM to 3 PM."],
    "hours": [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 40.0, "tariff_bdt_per_kwh": 6.0}
        for h in range(24)
    ],
    "battery": {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 100.0,
        "minimum_energy_kwh": 20.0,
        "max_charge_kwh_per_hour": 50.0,
        "max_discharge_kwh_per_hour": 60.0,
    },
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def request_schema() -> dict:
    schema = json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return schema


@pytest.fixture(scope="module")
def request_validator(request_schema) -> jsonschema.protocols.Validator:
    return jsonschema.validators.validator_for(request_schema)(request_schema)


@pytest.fixture(scope="module")
def client() -> TestClient:
    import os

    previous = os.environ.get("LLM_PROVIDER")
    os.environ["LLM_PROVIDER"] = "mock"
    try:
        from app.main import app

        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = previous


# ---------------------------------------------------------------------------
# Gate 1: strict schema validation of every public sample request
# ---------------------------------------------------------------------------


def test_request_schema_matches_pack_schema_notes(sample_pack):
    """The contract file must mirror the pack's own request-side requirements."""
    notes = sample_pack["_meta"]["schema_notes"]
    schema = json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert set(notes["input_required_fields"]) <= set(schema["required"])
    assert set(notes["hour_required_fields"]) <= set(
        schema["$defs"]["hourInput"]["required"]
    )
    assert set(notes["battery_required_fields"]) <= set(
        schema["$defs"]["batteryInput"]["required"]
    )
    assert set(schema["properties"]) == set(notes["input_required_fields"])
    assert set(schema["$defs"]["hourInput"]["properties"]) == set(
        notes["hour_required_fields"]
    )
    assert set(schema["$defs"]["batteryInput"]["properties"]) == set(
        notes["battery_required_fields"]
    )


def test_every_public_request_passes_strict_schema(request_validator, sample_cases):
    for case in sample_cases:
        # Raises with a full report on any violation.
        jsonschema.validate(instance=case["input"], schema=request_validator.schema)


def test_sample_requests_are_non_empty_and_schema_really_bites(request_validator):
    """Meta-check that the schema is load-bearing: an empty-hours probe fails."""
    broken = deepcopy(VALID_REQUEST)
    broken["hours"] = broken["hours"][:23]  # drop one hour
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=broken, schema=request_validator.schema)


# ---------------------------------------------------------------------------
# Gate 2: equivalence with the service ingress (Pydantic OptimizeRequest)
# ---------------------------------------------------------------------------


def test_schema_and_pydantic_accept_the_canonical_request(request_validator):
    jsonschema.validate(instance=VALID_REQUEST, schema=request_validator.schema)
    OptimizeRequest.model_validate(VALID_REQUEST)  # raises on rejection


def test_hours_do_not_need_to_be_sorted_for_either_gate(request_validator):
    shuffled = deepcopy(VALID_REQUEST)
    shuffled["hours"] = list(reversed(shuffled["hours"]))
    jsonschema.validate(instance=shuffled, schema=request_validator.schema)
    OptimizeRequest.model_validate(shuffled)


def test_pigeonhole_rejects_duplicate_and_missing_hours(request_validator):
    """24 slots + every hour required once => duplicates and gaps both fail."""
    duplicate = deepcopy(VALID_REQUEST)
    duplicate["hours"][5]["hour"] = 4  # two hour-4 entries, no hour-5
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=duplicate, schema=request_validator.schema)

    missing = deepcopy(VALID_REQUEST)
    missing["hours"] = missing["hours"][:-1]  # 23 entries: hour 23 absent
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing, schema=request_validator.schema)


@pytest.mark.parametrize(
    "mutation",
    [
        {"hours.0.hour": 24},
        {"hours.0.hour": -1},
        {"hours.0.hour": "13"},  # string hour
        {"hours.0.demand_kwh": -1.0},
        {"hours.0.solar_kwh": -0.5},
        {"hours.0.tariff_bdt_per_kwh": -0.1},
        {"battery.capacity_kwh": 0},
        {"battery.initial_energy_kwh": -1},
        {"battery.minimum_energy_kwh": -3},
        {"battery.max_charge_kwh_per_hour": -0.1},
        {"battery.max_discharge_kwh_per_hour": -0.1},
        {"scenario_id": ""},
        {"operator_notes": []},
        {"operator_notes": ["note one", "", "note three"]},
        {"operator_notes": ["a", "b", "c", "d"]},  # more than 3 notes
    ],
    ids=lambda m: next(iter(m)),
)
def test_schema_and_pydantic_reject_the_same_mutations(
    request_validator, mutation
):
    """Every listed mutation must fail BOTH the JSON Schema and Pydantic ingress."""
    def apply(spec: dict, target: dict) -> None:
        keys = list(spec)[0].split(".")
        node = target
        for key in keys[:-1]:
            node = node[int(key)] if key.isdigit() else node[key]
        node[keys[-1]] = list(spec.values())[0]

    schema_victim = deepcopy(VALID_REQUEST)
    apply(mutation, schema_victim)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=schema_victim, schema=request_validator.schema)

    pydantic_victim = deepcopy(VALID_REQUEST)
    apply(mutation, pydantic_victim)
    with pytest.raises(ValueError):  # pydantic.ValidationError subclasses ValueError
        OptimizeRequest.model_validate(pydantic_victim)


def test_extra_fields_rejected_by_both_gates(request_validator):
    extra_top = deepcopy(VALID_REQUEST)
    extra_top["bonus"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=extra_top, schema=request_validator.schema)
    with pytest.raises(ValueError):
        OptimizeRequest.model_validate(extra_top)

    extra_nested = deepcopy(VALID_REQUEST)
    extra_nested["battery"]["vendor"] = "acme"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=extra_nested, schema=request_validator.schema)
    with pytest.raises(ValueError):
        OptimizeRequest.model_validate(extra_nested)


def test_minus_infinity_parsed_value_violates_the_schema(request_validator):
    """Numeric bounds bite where comparisons can: -inf violates exclusiveMinimum."""
    victim = deepcopy(VALID_REQUEST)
    victim["battery"]["capacity_kwh"] = float("-inf")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=victim, schema=request_validator.schema)


@pytest.mark.parametrize(
    "nonfinite",
    [float("nan"), float("inf")],
    ids=["nan", "inf"],
)
def test_nan_and_plus_inf_are_ingress_only_blind_spots(request_validator, nonfinite):
    """Comparison-based bounds cannot see nan/+inf: the schema accepts them,
    but the service's Pydantic finite check rejects them. (End-to-end HTTP 400
    for these values is proven by the raw-literal test below — in-process
    clients refuse to even serialize non-finite floats.)"""
    victim = deepcopy(VALID_REQUEST)
    victim["battery"]["capacity_kwh"] = nonfinite
    jsonschema.validate(instance=victim, schema=request_validator.schema)  # accepted
    with pytest.raises(ValueError):  # Pydantic ingress: finite check
        OptimizeRequest.model_validate(victim)


@pytest.mark.parametrize(
    "literal",
    ["NaN", "Infinity", "-Infinity"],
    ids=["nan", "inf", "-inf"],
)
def test_nonfinite_json_literals_rejected_over_http(client, literal):
    """NaN/Infinity are not valid JSON (RFC 8259). A lenient parser may accept
    the raw literal; the service ingress still rejects it (HTTP 400) — this is
    the layer that catches +inf, which no schema bound can exclude."""
    raw = json.dumps(VALID_REQUEST).replace(
        '"capacity_kwh": 200.0', f'"capacity_kwh": {literal}'
    )
    resp = client.post(
        "/optimize-energy",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_live_service_accepts_schema_valid_request(client, request_validator):
    """The contract and the wire format agree: a schema-valid request ships."""
    resp = client.post("/optimize-energy", json=VALID_REQUEST)
    assert resp.status_code == 200, resp.text[:300]


# ---------------------------------------------------------------------------
# Gate 3: negative controls (schema must reject broken requests)
# ---------------------------------------------------------------------------


def test_negative_missing_required_top_level_field(request_validator):
    body = deepcopy(VALID_REQUEST)
    del body["battery"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)


def test_negative_missing_battery_field(request_validator):
    body = deepcopy(VALID_REQUEST)
    del body["battery"]["max_charge_kwh_per_hour"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)


def test_negative_missing_hour_field(request_validator):
    body = deepcopy(VALID_REQUEST)
    del body["hours"][0]["tariff_bdt_per_kwh"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)


def test_negative_wrong_type_scenario_id(request_validator):
    body = deepcopy(VALID_REQUEST)
    body["scenario_id"] = 42
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)


def test_negative_note_is_not_a_string(request_validator):
    body = deepcopy(VALID_REQUEST)
    body["operator_notes"] = [42]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)


def test_negative_unknown_battery_enum_shape(request_validator):
    """A structurally wrong hour entry (hour as object) fails the pigeonhole too."""
    body = deepcopy(VALID_REQUEST)
    body["hours"][0]["hour"] = {"value": 0}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=body, schema=request_validator.schema)

