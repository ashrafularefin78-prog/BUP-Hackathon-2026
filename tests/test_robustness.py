"""Robustness tests: bad input, provider outage, and stability."""

from __future__ import annotations

import pytest

from app import guardrails
from app.llm import LLMTransportError


def test_malformed_json_returns_400(client):
    resp = client.post(
        "/optimize-energy",
        content=b"{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_missing_fields_return_400(client):
    resp = client.post("/optimize-energy", json={"scenario_id": "X"})
    assert resp.status_code == 400


def test_wrong_hour_count_returns_400(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["hours"] = payload["hours"][:23]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_duplicate_hours_return_400(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["hours"] = payload["hours"][:-1] + [payload["hours"][0]]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_empty_note_rejected(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["operator_notes"] = [""]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_too_many_notes_rejected(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["operator_notes"] = ["a", "b", "c", "d"]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_negative_demand_rejected(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["hours"] = [dict(h, demand_kwh=-1) if h["hour"] == 5 else h
                        for h in payload["hours"]]
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_initial_energy_above_capacity_rejected(client, sample_cases):
    case = sample_cases[0]
    payload = dict(case["input"])
    payload["battery"] = dict(payload["battery"], initial_energy_kwh=10**6)
    resp = client.post("/optimize-energy", json=payload)
    assert resp.status_code == 400


def test_provider_outage_short_circuits_to_safe_no_ops(client, monkeypatch, sample_cases):
    """Circuit-breaker: transport failure yields a valid all-no_op schedule, not a 500."""
    case = sample_cases[1]

    import app.llm as llm_mod
    import app.pipeline as pipeline_mod

    class FailingInterpreter:
        provider = "failing"

        def interpret(self, notes, battery_capacity_kwh):
            raise LLMTransportError("provider down")

    monkeypatch.setattr(llm_mod, "build_interpreter", lambda settings: FailingInterpreter())

    resp = client.post("/optimize-energy", json=case["input"])
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["directive_interpretation"]) == len(case["input"]["operator_notes"])
    assert all(e["directive_type"] == "no_op" for e in body["directive_interpretation"])
    # Schedule is still valid and returned.
    assert len(body["hourly_plan"]) == 24
    assert abs(
        body["hourly_plan"][-1]["battery_energy_after_kwh"]
        - case["input"]["battery"]["initial_energy_kwh"]
    ) <= 0.01


def test_malformed_llm_output_demotes_instead_of_crashing(client, monkeypatch, sample_cases):
    case = sample_cases[2]

    import app.llm as llm_mod

    class GarbageInterpreter:
        provider = "garbage"

        def interpret(self, notes, battery_capacity_kwh):
            return [{"hello": "world"}]  # wrong arity and wrong shape

    monkeypatch.setattr(llm_mod, "build_interpreter", lambda settings: GarbageInterpreter())

    resp = client.post("/optimize-energy", json=case["input"])
    assert resp.status_code == 200
    body = resp.json()
    assert all(e["directive_type"] == "no_op" for e in body["directive_interpretation"])


def test_repeated_requests_stable(client, sample_cases):
    case = sample_cases[5]
    first = client.post("/optimize-energy", json=case["input"]).json()
    second = client.post("/optimize-energy", json=case["input"]).json()
    assert first["total_cost_bdt"] == pytest.approx(second["total_cost_bdt"], abs=1e-6)
    assert first["hourly_plan"] == second["hourly_plan"]


def test_guardrail_full_garbage_candidates_signal_retry():
    entries, reasons = guardrails.validate_and_repair("not a list", 1)
    assert entries is None
    assert reasons
