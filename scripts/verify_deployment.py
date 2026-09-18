#!/usr/bin/env python
"""Verify a deployed GridWise service from OUTSIDE your dev environment.

Run against the public URL judges will use:

    python scripts/verify_deployment.py --base-url https://gridwise-api.onrender.com

Checks:    1. GET  /health            -> 200 {"status": "ok"} within 60 s
    2. POST /optimize-energy   -> 200 for all 10 public samples
    3. Recomputed cost == reference optimum (tolerance 0.01 BDT)
    4. Response conforms to schemas/response.schema.json (strict)
    4b. Each request conforms to schemas/request.schema.json (strict) —
        proving our own inputs are contract-clean before they are sent
    5. Latency: per-request, mean, and p95 against the rubric bands
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = REPO_ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
SCHEMA_PATH = REPO_ROOT / "schemas" / "response.schema.json"
REQUEST_SCHEMA_PATH = REPO_ROOT / "schemas" / "request.schema.json"


def band(ms: float) -> str:
    if ms <= 5000:
        return "3/3 latency credit (p95 <= 5s band)"
    if ms <= 15000:
        return "2/3 (5-15s)"
    if ms <= 30000:
        return "1/3 (15-30s)"
    return "0/3 (>30s = failure)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="public base URL, no trailing slash")
    parser.add_argument("--single", help="optional single scenario id")
    args = parser.parse_args()

    with open(PACK_PATH, "r", encoding="utf-8") as f:
        pack = json.load(f)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    request_schema = json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    request_validator = jsonschema.validators.validator_for(request_schema)(request_schema)

    cases = pack["cases"]
    if args.single:
        cases = [c for c in cases if c["id"].upper() == args.single.upper()]

    failures: list[str] = []
    latencies_ms: list[float] = []

    print(f"target: {args.base_url}")

    # --- Gate 1: health ---
    try:
        started = time.perf_counter()
        health = httpx.get(f"{args.base_url}/health", timeout=60.0)
        health_ms = (time.perf_counter() - started) * 1000
        body = health.json()
        ok = health.status_code == 200 and body.get("status") == "ok"
        print(f"[{'PASS' if ok else 'FAIL'}] GET /health -> {health.status_code} {health.text} ({health_ms:.0f} ms)")
        if not ok:
            failures.append("health endpoint failed")
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] GET /health unreachable: {exc}")
        return 1

    # --- Gates 2-5: samples ---
    with httpx.Client(timeout=35.0) as client:
        for case in cases:
            scenario_id = case["id"]
            try:
                jsonschema.validate(instance=case["input"], schema=request_validator.schema)
            except jsonschema.ValidationError as exc:
                failures.append(f"{scenario_id}: request contract violation: {exc.message[:100]}")
                print(f"[FAIL] {scenario_id}: request violates request.schema.json: {exc.message[:100]}")
                continue
            try:
                started = time.perf_counter()
                resp = client.post(f"{args.base_url}/optimize-energy", json=case["input"])
                latencies_ms.append((time.perf_counter() - started) * 1000)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{scenario_id}: request error: {exc}")
                print(f"[FAIL] {scenario_id}: request error: {exc}")
                continue

            if resp.status_code != 200:
                failures.append(f"{scenario_id}: HTTP {resp.status_code}")
                print(f"[FAIL] {scenario_id}: HTTP {resp.status_code} {resp.text[:120]}")
                continue

            body = resp.json()
            ref_cost = case["expected_output"]["total_cost_bdt"]
            cost_delta = abs(body.get("total_cost_bdt", 1e9) - ref_cost)
            cost_ok = cost_delta <= 0.01

            try:
                jsonschema.validate(instance=body, schema=validator.schema)
                schema_ok = True
                schema_note = "schema OK"
            except jsonschema.ValidationError as exc:
                schema_ok = False
                schema_note = f"schema violation: {exc.message[:100]}"

            ok = cost_ok and schema_ok
            status = "PASS" if ok else "FAIL"
            print(
                f"[{status}] {scenario_id}: cost {body.get('total_cost_bdt')} "
                f"(ref {ref_cost}, delta {cost_delta:.4f}) | {schema_note} | {latencies_ms[-1]:.0f} ms"
            )
            if not cost_ok:
                failures.append(f"{scenario_id}: cost delta {cost_delta}")
            if not schema_ok:
                failures.append(f"{scenario_id}: {schema_note}")

    # --- Latency summary ---
    if latencies_ms:
        sorted_ms = sorted(latencies_ms)
        p95 = sorted_ms[min(len(sorted_ms) - 1, int(round(0.95 * len(sorted_ms))) - 1)]
        print(
            f"\nlatency: mean {statistics.mean(latencies_ms):.0f} ms, "
            f"max {max(latencies_ms):.0f} ms, p95 {p95:.0f} ms -> {band(p95)}"
        )

    if failures:
        print(f"\nRESULT: FAIL ({len(failures)} problem(s))")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nRESULT: PASS — service is judge-ready at this URL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
