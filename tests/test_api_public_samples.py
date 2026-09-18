"""End-to-end tests over the 10 public sample cases through the API layer.

Per the pack's equivalence note, schedules are compared by recomputed cost and
validity — never byte-for-byte against the reference plan.
"""

from __future__ import annotations

import pytest

TOL = 0.01

# Ground-truth interpretation semantics from the public pack (types, hours, key
# numeric values) — used to assert the mock interpreter's extraction quality.
EXPECTED_INTERPRETATIONS = {
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

EXPECTED_TOTALS = {
    "SAMPLE-01": (2692.5, 38365.0),
    "SAMPLE-02": (2915.0, 42885.0),
    "SAMPLE-03": (2430.0, 35480.0),
    "SAMPLE-04": (2645.0, 40495.0),
    "SAMPLE-05": (2430.0, 33950.0),
    "SAMPLE-06": (2395.0, 34090.0),
    "SAMPLE-07": (2560.0, 38550.0),
    "SAMPLE-08": (2490.0, 37665.0),
    "SAMPLE-09": (2504.0, 34873.0),
    "SAMPLE-10": (2715.0, 41620.0),
}


def test_all_public_cases_end_to_end(client, sample_cases):
    failures = []
    for case in sample_cases:
        scenario_id = case["id"]
        resp = client.post("/optimize-energy", json=case["input"])
        if resp.status_code != 200:
            failures.append(f"{scenario_id}: HTTP {resp.status_code}: {resp.text[:200]}")
            continue
        body = resp.json()

        # --- Response schema ---
        for field in (
            "scenario_id", "directive_interpretation", "hourly_plan",
            "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
        ):
            if field not in body:
                failures.append(f"{scenario_id}: missing response field {field}")
        if body["scenario_id"] != scenario_id:
            failures.append(f"{scenario_id}: scenario_id echo mismatch")

        # --- Interpretation matches organizer ground truth ---
        expected = EXPECTED_INTERPRETATIONS[scenario_id]
        got = body["directive_interpretation"]
        if len(got) != len(expected):
            failures.append(f"{scenario_id}: expected {len(expected)} entries, got {len(got)}")
            continue
        for i, (etype, ehours, eval_) in enumerate(expected):
            entry = got[i]
            if entry["note_index"] != i:
                failures.append(f"{scenario_id}: note_index {entry['note_index']} != {i}")
            if entry["directive_type"] != etype:
                failures.append(
                    f"{scenario_id} note {i}: type {entry['directive_type']} != {etype}"
                )
                continue
            if etype == "no_op":
                if entry["applies"] or entry["structured_adjustment"] is not None:
                    failures.append(f"{scenario_id} note {i}: bad no_op shape")
                continue
            if not entry["applies"]:
                failures.append(f"{scenario_id} note {i}: applies false for {etype}")
            adj = entry["structured_adjustment"]
            if adj["hours"] != ehours:
                failures.append(f"{scenario_id} note {i}: hours {adj['hours']} != {ehours}")
            if eval_ is not None:
                key = {"solar_reduction": "factor",
                       "minimum_battery_reserve": "minimum_energy_kwh",
                       "max_grid_window": "max_grid_kwh"}[etype]
                if abs(adj[key] - eval_) > TOL:
                    failures.append(f"{scenario_id} note {i}: {key} {adj[key]} != {eval_}")

        # --- Schedule validity + optimal cost ---
        exp_grid, exp_cost = EXPECTED_TOTALS[scenario_id]
        if abs(body["total_grid_kwh"] - exp_grid) > TOL:
            failures.append(
                f"{scenario_id}: total_grid_kwh {body['total_grid_kwh']} != reference {exp_grid}"
            )
        if abs(body["total_cost_bdt"] - exp_cost) > TOL:
            failures.append(
                f"{scenario_id}: total_cost_bdt {body['total_cost_bdt']} != optimal {exp_cost}"
            )

    assert not failures, "\n".join(failures)


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_totals_consistent_with_hourly_plan(client, sample_cases):
    case = sample_cases[0]
    resp = client.post("/optimize-energy", json=case["input"])
    body = resp.json()
    plan = body["hourly_plan"]

    assert len(plan) == 24
    assert [p["hour"] for p in plan] == list(range(24))

    hours = case["input"]["hours"]
    total_grid = sum(p["grid_kwh"] for p in plan)
    total_cost = sum(p["grid_kwh"] * h["tariff_bdt_per_kwh"] for p, h in zip(plan, hours))
    peak = max(p["grid_kwh"] for p in plan)

    assert abs(total_grid - body["total_grid_kwh"]) <= TOL
    assert abs(total_cost - body["total_cost_bdt"]) <= TOL
    assert abs(peak - body["peak_grid_kwh"]) <= TOL


def test_battery_neutrality_in_response(client, sample_cases):
    for case in sample_cases:
        resp = client.post("/optimize-energy", json=case["input"])
        body = resp.json()
        initial = case["input"]["battery"]["initial_energy_kwh"]
        final = body["hourly_plan"][-1]["battery_energy_after_kwh"]
        assert abs(final - initial) <= TOL, f"{case['id']}: final {final} != initial {initial}"
